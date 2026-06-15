"""
Django management command: consume_incidents

Long-lived Kafka consumer that syncs incident data from FastAPI (DB-2)
to Django (DB-1) by upserting the incident_snapshots table.

Subscribed topics:
    - incidents.created           → Create a new incident snapshot
    - incidents.duplicate_detected → Update occurrence_count + last_seen_at
    - incidents.analyzed          → Reserved for future analytics updates
    - incidents.updated           → Apply status/assignment changes

Architecture:
    This consumer follows the exact same pattern as consume_indexing_status:
      1. Parse JSON from Kafka message value
      2. Validate required fields and UUIDs
      3. Insert into ProcessedEvent for idempotency (IntegrityError → skip)
      4. UPSERT the IncidentSnapshot model
      5. Commit Kafka offset ONLY after DB write succeeds

    If the DB write fails, the offset is NOT committed and Kafka will
    redeliver the message on the next poll cycle.

Graceful shutdown:
    Handles SIGTERM and SIGINT by setting _running = False.
    The consumer loop exits cleanly and calls consumer.close().

Usage:
    python manage.py consume_incidents

Architecture reference:
    Phase 4 Technical Documentation — Section 5.4 (Django Kafka Consumer)
"""
from __future__ import annotations

import json
import logging
import signal
import time
import uuid

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import IntegrityError, transaction
from django.utils import timezone as django_timezone

from confluent_kafka import Consumer, KafkaError, KafkaException

from analytics.models import IncidentSnapshot
from outbox.models import ProcessedEvent

logger = logging.getLogger(__name__)

# ── Consumer configuration ────────────────────────────────────────────────────

CONSUMER_GROUP = "django-incident-snapshot-consumer"
TOPICS = [
    "incidents.created",
    "incidents.duplicate_detected",
    "incidents.analyzed",
    "incidents.updated",
]

# Seconds to wait before reconnecting after a consumer error
_RECONNECT_DELAY = 5

# Bootstrap servers — loaded from Django settings with a safe fallback
BOOTSTRAP_SERVERS = getattr(settings, "KAFKA_BOOTSTRAP_SERVERS", "kafka:9092")


# ── Management command ────────────────────────────────────────────────────────


class Command(BaseCommand):
    help = (
        "Kafka consumer: syncs incident snapshots from FastAPI (DB-2) "
        "to Django (DB-1) via Debezium outbox events."
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._running = True

    def handle(self, *args, **options) -> None:
        """Entry point — registers signal handlers and starts the consumer loop."""
        signal.signal(signal.SIGTERM, self._handle_shutdown)
        signal.signal(signal.SIGINT, self._handle_shutdown)

        logger.info(
            "incident_snapshot_consumer_starting",
            extra={
                "topics": TOPICS,
                "group_id": CONSUMER_GROUP,
                "bootstrap_servers": BOOTSTRAP_SERVERS,
            },
        )

        # Outer retry loop — reconnects on transient Kafka errors
        while self._running:
            try:
                self._run_consumer_loop()
            except (KafkaException, Exception) as exc:
                logger.error(
                    "incident_snapshot_consumer_error",
                    extra={"error": str(exc)},
                    exc_info=True,
                )
            if self._running:
                logger.info(
                    "incident_snapshot_consumer_reconnecting",
                    extra={"delay_seconds": _RECONNECT_DELAY},
                )
                time.sleep(_RECONNECT_DELAY)

        logger.info("incident_snapshot_consumer_exited")

    # ── Consumer loop ─────────────────────────────────────────────────────────

    def _run_consumer_loop(self) -> None:
        """Create a Kafka consumer, subscribe, and poll in a loop."""
        consumer = Consumer({
            "bootstrap.servers": BOOTSTRAP_SERVERS,
            "group.id": CONSUMER_GROUP,
            "auto.offset.reset": "earliest",
            "enable.auto.commit": False,
            "allow.auto.create.topics": True,
            "session.timeout.ms": 30000,
            "heartbeat.interval.ms": 10000,
            "max.poll.interval.ms": 300000,
        })
        consumer.subscribe(TOPICS)

        logger.info(
            "incident_snapshot_consumer_subscribed",
            extra={"topics": TOPICS},
        )

        try:
            while self._running:
                msg = consumer.poll(timeout=2.0)
                if msg is None:
                    continue
                if msg.error():
                    err = msg.error()
                    if err.code() == KafkaError._PARTITION_EOF:
                        continue
                    if err.code() == KafkaError.UNKNOWN_TOPIC_OR_PART:
                        # Topics don't exist until the first event is produced.
                        # Wait briefly and retry rather than crashing.
                        time.sleep(2.0)
                        continue
                    raise KafkaException(err)

                self._handle_message(msg)

                # Commit offset AFTER successful DB write
                consumer.commit(message=msg, asynchronous=False)
        finally:
            consumer.close()
            logger.info("incident_snapshot_consumer_closed")

    # ── Message handler ───────────────────────────────────────────────────────

    def _handle_message(self, msg) -> None:
        """Parse a Kafka message and dispatch to the appropriate handler."""
        # ── Step 1: Parse JSON ────────────────────────────────────────────
        try:
            payload = json.loads(msg.value().decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            logger.error(
                "incident_snapshot_json_error",
                extra={
                    "error": str(exc),
                    "topic": msg.topic(),
                    "partition": msg.partition(),
                    "offset": msg.offset(),
                },
            )
            return

        event_type = payload.get("event_type")
        event_id_str = payload.get("event_id")

        # ── Step 2: Validate event_id ─────────────────────────────────────
        if not event_id_str:
            logger.error(
                "incident_snapshot_missing_event_id",
                extra={"topic": msg.topic()},
            )
            return

        try:
            event_uuid = uuid.UUID(event_id_str)
        except ValueError:
            logger.error(
                "incident_snapshot_invalid_event_id",
                extra={"event_id": event_id_str},
            )
            return

        # ── Step 3: Route to handler ──────────────────────────────────────
        handler_map = {
            "incident.created": self._handle_created,
            "incident.duplicate_detected": self._handle_duplicate,
            "incident.analyzed": self._handle_analyzed,
            "incident.updated": self._handle_updated,
        }

        handler = handler_map.get(event_type)
        if handler is None:
            logger.debug(
                "incident_snapshot_unhandled_event_type",
                extra={"event_type": event_type},
            )
            return

        # ── Step 4: Idempotency check + DB write (atomic) ─────────────────
        try:
            with transaction.atomic():
                # Insert ProcessedEvent — IntegrityError if already processed
                try:
                    ProcessedEvent.objects.create(
                        event_id=event_uuid,
                        consumer_group=CONSUMER_GROUP,
                        topic=msg.topic(),
                    )
                except IntegrityError:
                    logger.info(
                        "incident_snapshot_duplicate_event_skipped",
                        extra={"event_id": event_id_str},
                    )
                    return

                # Execute the handler within the same atomic block
                handler(payload.get("payload", {}))

        except Exception as exc:
            logger.error(
                "incident_snapshot_db_write_failed",
                extra={
                    "event_type": event_type,
                    "event_id": event_id_str,
                    "error": str(exc),
                },
                exc_info=True,
            )
            # Re-raise so that Kafka offset is NOT committed
            # and the message will be redelivered
            raise

    # ── Event handlers ────────────────────────────────────────────────────────

    def _handle_created(self, data: dict) -> None:
        """
        Upsert a new incident snapshot row.

        Uses update_or_create to handle the edge case where a
        duplicate_detected event arrives before the created event
        (out-of-order delivery).
        """
        try:
            incident_id = uuid.UUID(data["incident_id"])
            tenant_id = uuid.UUID(data["tenant_id"])
        except (KeyError, ValueError) as exc:
            logger.error(
                "incident_snapshot_created_invalid_ids",
                extra={"error": str(exc), "data_keys": list(data.keys())},
            )
            return

        IncidentSnapshot.objects.update_or_create(
            incident_id=incident_id,
            defaults={
                "tenant_id": tenant_id,
                "status": data.get("status", "open"),
                "severity": data.get("severity", "unknown"),
                "confidence_score": data.get("confidence_score"),
                "error_type": data.get("error_type", ""),
                "error_message": data.get("error_message", ""),
                "service_name": data.get("service_name", ""),
                "environment": data.get("environment", ""),
                "crash_file": data.get("crash_file", ""),
                "crash_method": data.get("crash_method", ""),
                "root_cause": data.get("root_cause", ""),
                "suggested_fix": data.get("suggested_fix", ""),
                "assigned_user_id": (
                    uuid.UUID(data["assigned_user_id"])
                    if data.get("assigned_user_id")
                    else None
                ),
                "occurrence_count": data.get("occurrence_count", 1),
                "is_draft": data.get("is_draft", False),
                "source_version": 1,
                "first_seen_at": data.get("first_seen_at"),
                "last_seen_at": data.get("last_seen_at") or data.get("first_seen_at"),
                "created_at": data.get("created_at") or data.get("first_seen_at"),
            },
        )

        logger.info(
            "incident_snapshot_created",
            extra={"incident_id": str(incident_id)},
        )

    def _handle_duplicate(self, data: dict) -> None:
        """
        Update occurrence_count and last_seen_at on duplicate detection.

        This is a lightweight UPDATE — no full upsert needed because the
        incident row must already exist from the incident.created event.
        """
        try:
            incident_id = uuid.UUID(data["incident_id"])
        except (KeyError, ValueError) as exc:
            logger.error(
                "incident_snapshot_duplicate_invalid_id",
                extra={"error": str(exc)},
            )
            return

        updated = IncidentSnapshot.objects.filter(
            incident_id=incident_id
        ).update(
            occurrence_count=data.get("new_occurrence_count", 1),
            last_seen_at=data.get("last_seen_at"),
        )

        if updated == 0:
            logger.warning(
                "incident_snapshot_duplicate_no_row",
                extra={
                    "incident_id": str(incident_id),
                    "detail": (
                        "Duplicate event arrived before created event. "
                        "The snapshot will be created when the created "
                        "event is processed."
                    ),
                },
            )
        else:
            logger.info(
                "incident_snapshot_duplicate_updated",
                extra={
                    "incident_id": str(incident_id),
                    "new_occurrence_count": data.get("new_occurrence_count"),
                },
            )

    def _handle_analyzed(self, data: dict) -> None:
        """
        Handle incident.analyzed events.

        Currently a no-op — the analysis data is stored in DB-2 only.
        Reserved for future analytics aggregation (e.g. token usage
        dashboards, agent performance metrics per tenant).
        """
        logger.debug(
            "incident_snapshot_analyzed_noop",
            extra={
                "incident_id": data.get("incident_id"),
                "analysis_id": data.get("analysis_id"),
            },
        )

    def _handle_updated(self, data: dict) -> None:
        """
        Apply status and assignment updates to the snapshot.

        Only updates fields that are present in the event payload.
        """
        try:
            incident_id = uuid.UUID(data["incident_id"])
        except (KeyError, ValueError) as exc:
            logger.error(
                "incident_snapshot_updated_invalid_id",
                extra={"error": str(exc)},
            )
            return

        update_fields = {}

        if "status" in data:
            update_fields["status"] = data["status"]
            # Auto-set resolved_at when transitioning to resolved
            if data["status"] == "resolved":
                update_fields["resolved_at"] = django_timezone.now()

        if "assigned_user_id" in data:
            update_fields["assigned_user_id"] = (
                uuid.UUID(data["assigned_user_id"])
                if data["assigned_user_id"]
                else None
            )

        if update_fields:
            updated = IncidentSnapshot.objects.filter(
                incident_id=incident_id
            ).update(**update_fields)

            if updated == 0:
                logger.warning(
                    "incident_snapshot_updated_no_row",
                    extra={"incident_id": str(incident_id)},
                )
            else:
                logger.info(
                    "incident_snapshot_updated",
                    extra={
                        "incident_id": str(incident_id),
                        "fields_updated": list(update_fields.keys()),
                    },
                )

    # ── Shutdown handler ──────────────────────────────────────────────────────

    def _handle_shutdown(self, signum, frame) -> None:
        """Signal handler for graceful shutdown."""
        logger.info(
            "incident_snapshot_consumer_shutdown_signal",
            extra={"signal": signum},
        )
        self._running = False
