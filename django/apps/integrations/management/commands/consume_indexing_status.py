"""
django/apps/integrations/management/commands/consume_indexing_status.py

Django management command: consume_indexing_status

Long-lived Kafka consumer that listens to the ``indexing.status`` topic,
which FastAPI's ``index_code`` Celery task publishes to (via the DB-2
transactional outbox → Debezium CDC pipeline) whenever the indexing
status of a tenant's GitHub repository changes.

On each message this command:
  1. Checks the ``processed_events`` table (DB-1) for idempotency.
  2. Updates ``GitHubIntegration.indexing_status`` (and optionally
     ``last_indexed_commit``) in DB-1 via a direct queryset update —
     NOT via .save() — so the model's source_version increment and
     outbox write are intentionally skipped (prevents an infinite loop).
  3. Commits the Kafka offset only after the DB-1 write succeeds.

Architecture contract
---------------------
- Topic:          ``indexing.status``  (value from KAFKA_INDEXING_STATUS_TOPIC)
- Consumer group: ``django-indexing-status-consumer``
- Auto-offset:    ``earliest`` so a freshly deployed container catches any
                  status changes that arrived while it was down.
- At-least-once delivery: duplicate events are safely rejected by the
  processed_events idempotency check.
- No outbox write: this command uses .filter().update() so no new Kafka
  event is emitted back to FastAPI, breaking any potential loop.

Usage
-----
    python manage.py consume_indexing_status

Lifecycle in docker-compose
---------------------------
The ``django-kafka-consumer`` service in docker-compose.yml runs this
command. It restarts automatically (restart: unless-stopped) so transient
Kafka disconnects are healed without manual intervention.

Architecture reference: NeuralOps Technical Documentation — Sections 3, 5, 6
"""

from __future__ import annotations

import json
import logging
import signal
import time
import uuid

from confluent_kafka import Consumer, KafkaError, KafkaException
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import IntegrityError, transaction
from integrations.models import GitHubIntegration
from outbox.models import ProcessedEvent
from apps.websockets.publisher import push_collaboration_event

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

CONSUMER_GROUP = getattr(
    settings,
    "KAFKA_INDEXING_STATUS_GROUP_ID",
    "django-indexing-status-consumer",
)
TOPIC = getattr(settings, "KAFKA_INDEXING_STATUS_TOPIC", "indexing.status")
BOOTSTRAP_SERVERS = getattr(settings, "KAFKA_BOOTSTRAP_SERVERS", "kafka:9092")

# Valid indexing status values — guard against malformed payloads.
_VALID_STATUSES = frozenset({"pending", "indexing", "indexed", "failed"})

# Seconds to wait between reconnection attempts after a fatal Kafka error.
_RECONNECT_DELAY = 5


class Command(BaseCommand):
    """
    consume_indexing_status — Kafka consumer for ``indexing.status`` events.

    Keeps ``GitHubIntegration.indexing_status`` in DB-1 in sync with the
    actual indexing state recorded by FastAPI in DB-2.
    """

    help = (
        "Long-running Kafka consumer: syncs GitHub indexing status "
        "from FastAPI (DB-2) into Django (DB-1) via the indexing.status topic."
    )

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._running = True

    # ── Django management command entry point ─────────────────────────────────

    def handle(self, *args, **options) -> None:
        """Start the consumer loop. Runs until SIGTERM / SIGINT is received."""
        # Register graceful shutdown handlers.
        signal.signal(signal.SIGTERM, self._handle_shutdown)
        signal.signal(signal.SIGINT, self._handle_shutdown)

        self.stdout.write(
            self.style.SUCCESS(
                f"[consume_indexing_status] Starting — topic={TOPIC!r} "
                f"group={CONSUMER_GROUP!r} brokers={BOOTSTRAP_SERVERS!r}"
            )
        )
        logger.info(
            "indexing_status_consumer_starting",
            extra={
                "topic": TOPIC,
                "group_id": CONSUMER_GROUP,
                "bootstrap_servers": BOOTSTRAP_SERVERS,
            },
        )

        while self._running:
            try:
                self._run_consumer_loop()
            except KafkaException as exc:
                logger.error(
                    "indexing_status_consumer_kafka_error",
                    extra={"error": str(exc)},
                    exc_info=True,
                )
            except Exception as exc:
                logger.error(
                    "indexing_status_consumer_unexpected_error",
                    extra={"error": str(exc)},
                    exc_info=True,
                )

            if self._running:
                logger.info(
                    "indexing_status_consumer_reconnecting",
                    extra={"delay_seconds": _RECONNECT_DELAY},
                )
                time.sleep(_RECONNECT_DELAY)

        logger.info("indexing_status_consumer_stopped")
        self.stdout.write(self.style.WARNING("[consume_indexing_status] Stopped."))

    # ── Core consumer loop ────────────────────────────────────────────────────

    def _run_consumer_loop(self) -> None:
        """
        Create a confluent-kafka Consumer, subscribe to the topic, and poll
        messages until self._running is False or a fatal error occurs.
        """
        consumer = Consumer(
            {
                "bootstrap.servers": BOOTSTRAP_SERVERS,
                "group.id": CONSUMER_GROUP,
                # Replay from the beginning when the consumer group has no
                # committed offset (fresh deployment or group reset).
                "auto.offset.reset": "earliest",
                # Manual commit — we commit only AFTER DB-1 is written.
                "enable.auto.commit": False,
                # Allow the consumer to auto-create the topic if it doesn’t
                # exist yet (handles the case where no indexing task has run
                # since a fresh Kafka deploy wiped the topic list).
                "allow.auto.create.topics": True,
                # Session and heartbeat tuning for long-lived consumers.
                "session.timeout.ms": 30000,
                "heartbeat.interval.ms": 10000,
                "max.poll.interval.ms": 300000,
            }
        )
        consumer.subscribe([TOPIC])
        logger.info("indexing_status_consumer_subscribed", extra={"topic": TOPIC})

        try:
            while self._running:
                msg = consumer.poll(timeout=2.0)

                if msg is None:
                    # No message within the poll timeout — keep looping.
                    continue

                if msg.error():
                    err = msg.error()
                    if err.code() == KafkaError._PARTITION_EOF:
                        # End of partition — not an error, just caught up.
                        logger.debug(
                            "indexing_status_consumer_partition_eof",
                            extra={
                                "partition": msg.partition(),
                                "offset": msg.offset(),
                            },
                        )
                        continue
                    if err.code() == KafkaError.UNKNOWN_TOPIC_OR_PART:
                        # Topic doesn’t exist yet — transient during fresh
                        # Kafka deployments before any indexing task has run.
                        # Wait briefly and retry rather than crashing.
                        logger.warning(
                            "indexing_status_topic_not_ready",
                            extra={"topic": TOPIC, "error": str(err)},
                        )
                        continue
                    # Fatal broker/network error — let the outer loop reconnect.
                    raise KafkaException(err)

                # ── Process the message ───────────────────────────────────────
                self._handle_message(msg)

                # ── Commit offset AFTER successful DB write ───────────────────
                consumer.commit(message=msg, asynchronous=False)

        finally:
            consumer.close()
            logger.info("indexing_status_consumer_closed")

    # ── Message handler ───────────────────────────────────────────────────────

    def _handle_message(self, msg) -> None:
        """
        Process a single ``indexing.status`` Kafka message.

        Steps:
          1. Parse JSON payload.
          2. Validate required fields.
          3. Idempotency check via processed_events.
          4. Update GitHubIntegration in DB-1.
        """
        raw_value = msg.value()
        topic = msg.topic()
        offset = msg.offset()
        partition = msg.partition()

        logger.debug(
            "indexing_status_message_received",
            extra={"topic": topic, "partition": partition, "offset": offset},
        )

        # ── 1. Parse JSON ─────────────────────────────────────────────────────
        try:
            payload: dict = json.loads(raw_value.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            logger.error(
                "indexing_status_json_decode_error",
                extra={
                    "topic": topic,
                    "offset": offset,
                    "error": str(exc),
                    "raw": raw_value[:200] if raw_value else None,
                },
            )
            # Permanently malformed — commit and skip (dead message).
            return

        # ── 2. Validate required fields ───────────────────────────────────────
        event_id_str = payload.get("event_id")
        tenant_id_str = payload.get("tenant_id")
        status = payload.get("status")
        commit_sha = payload.get("commit_sha")  # may be None for non-indexed states

        if not event_id_str or not tenant_id_str or not status:
            logger.error(
                "indexing_status_missing_fields",
                extra={"payload_keys": list(payload.keys()), "offset": offset},
            )
            return

        if status not in _VALID_STATUSES:
            logger.warning(
                "indexing_status_invalid_status",
                extra={"status": status, "tenant_id": tenant_id_str},
            )
            return

        try:
            event_uuid = uuid.UUID(event_id_str)
            tenant_uuid = uuid.UUID(tenant_id_str)
        except ValueError as exc:
            logger.error(
                "indexing_status_invalid_uuid",
                extra={
                    "error": str(exc),
                    "event_id": event_id_str,
                    "tenant_id": tenant_id_str,
                },
            )
            return

        # ── 3. Idempotency check + DB-1 update (atomic) ───────────────────────
        try:
            with transaction.atomic():
                # Insert into processed_events. If the event was already
                # processed (duplicate Kafka delivery), this raises
                # IntegrityError and we abort without updating the model.
                try:
                    ProcessedEvent.objects.create(
                        event_id=event_uuid,
                        consumer_group=CONSUMER_GROUP,
                        topic=topic,
                    )
                except IntegrityError:
                    logger.info(
                        "indexing_status_duplicate_event_skipped",
                        extra={"event_id": event_id_str, "tenant_id": tenant_id_str},
                    )
                    return

                # ── 4. Update GitHubIntegration in DB-1 ──────────────────────
                # Use .filter().update() — NOT .save() — so that:
                #   a) The source_version F() increment is NOT triggered.
                #   b) No new outbox event is written to config.tenants.
                #   c) No Kafka loop is created.
                update_fields: dict = {"indexing_status": status}
                if commit_sha:
                    update_fields["last_indexed_commit"] = commit_sha

                updated_count = GitHubIntegration.objects.filter(
                    tenant_id=tenant_uuid
                ).update(**update_fields)

                if updated_count == 0:
                    logger.warning(
                        "indexing_status_no_integration_found",
                        extra={"tenant_id": tenant_id_str, "status": status},
                    )
                    # Still commit — retrying won't help if the integration
                    # row doesn't exist yet (race between consumer and create).
                    return

                # Push real-time update to the frontend via WebSockets
                try:
                    push_collaboration_event(
                        tenant_id_str,
                        "github_indexing",
                        {"status": status, "commit_sha": commit_sha}
                    )
                except Exception as wsexc:
                    logger.error(
                        "indexing_status_websocket_push_failed",
                        extra={"error": str(wsexc), "tenant_id": tenant_id_str}
                    )

        except Exception as exc:
            logger.error(
                "indexing_status_db_write_failed",
                extra={
                    "tenant_id": tenant_id_str,
                    "status": status,
                    "error": str(exc),
                },
                exc_info=True,
            )
            # Re-raise so the outer loop does NOT commit the offset.
            # Kafka will redeliver this message on the next poll.
            raise

        logger.info(
            "indexing_status_synced",
            extra={
                "tenant_id": tenant_id_str,
                "status": status,
                "commit_sha": commit_sha,
                "event_id": event_id_str,
            },
        )

    # ── Signal handlers ───────────────────────────────────────────────────────

    def _handle_shutdown(self, signum, frame) -> None:
        """Handle SIGTERM / SIGINT by setting the running flag to False."""
        logger.info(
            "indexing_status_consumer_shutdown_signal",
            extra={"signal": signum},
        )
        self._running = False
