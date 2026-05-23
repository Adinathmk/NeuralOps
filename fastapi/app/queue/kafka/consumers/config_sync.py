"""
app/queue/kafka/consumers/config_sync.py

Kafka consumer for configuration snapshot synchronisation (Phase 2, Part 1).

Architecture overview
---------------------
Django (Service 1 / DB-1) owns the canonical copies of:
  - Tenant configuration   → published to Kafka topic: config.tenants
  - Alert rules            → published to Kafka topic: config.alert_rules
  - Playbooks              → published to Kafka topic: config.playbooks

Configuration changes travel through the Transactional Outbox in DB-1.
Debezium tails the DB-1 WAL and delivers outbox rows to Kafka via the
Outbox Event Router transform, which strips the envelope and produces a
raw JSON payload as the Kafka message value.

This consumer subscribes to all three topics, parses the events, and
upserts the corresponding snapshot tables in DB-2 so that the FastAPI
service can read tenant/alert-rule/playbook config locally without ever
making a synchronous HTTP call to Django.

Idempotency & staleness protection
-----------------------------------
Every event payload includes a `source_version` integer that is
monotonically incremented on the source entity in DB-1.

RULE: If the local DB-2 snapshot row already exists AND its
`source_version >= the incoming event's source_version`, the event is
a stale re-delivery (e.g. Kafka at-least-once) and MUST be discarded.
Only events with a strictly higher `source_version` are applied.

Redis L1 cache invalidation
-----------------------------
After every successful DB-2 commit, the Redis key
`tenant:{tenant_id}:config` is deleted so that the next API request
picks up the fresh snapshot from DB-2 and repopulates the 1-hour TTL
cache.  We deliberately DELETE rather than re-populate here because the
cache aggregation query is owned by the API layer (keeps concerns separate).

Kafka topic: config.* topics use log compaction.
A fresh FastAPI deployment replays the compacted topic from the
beginning to rebuild snapshots without requiring Django to re-push
all records.

Architecture reference: NeuralOps Technical Documentation — Sections 5, 6, 7
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any, Dict, Optional

import redis.asyncio as aioredis
from aiokafka import AIOKafkaConsumer
from aiokafka.errors import KafkaError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.database.session import SessionLocal
from app.models.snapshots import AlertRuleSnapshot, PlaybookSnapshot, TenantSnapshot

logger = logging.getLogger(__name__)

# ── Kafka topic names ─────────────────────────────────────────────────────────
TOPIC_TENANTS = "config.tenants"
TOPIC_ALERT_RULES = "config.alert_rules"
TOPIC_PLAYBOOKS = "config.playbooks"

CONFIG_TOPICS = (TOPIC_TENANTS, TOPIC_ALERT_RULES, TOPIC_PLAYBOOKS)


# ── ConfigSyncConsumer ────────────────────────────────────────────────────────


class ConfigSyncConsumer:
    """
    Long-running async Kafka consumer that keeps DB-2 snapshot tables in
    sync with Django's authoritative configuration in DB-1.

    Lifecycle
    ---------
    Instantiate once at application startup.
    Call `start()` inside an `asyncio.create_task()` so it runs as a
    background task without blocking the FastAPI event loop.
    Call `stop()` during graceful shutdown to drain in-flight messages and
    close the Kafka consumer cleanly.

    Usage in main.py lifespan:
        consumer = ConfigSyncConsumer()
        asyncio.create_task(consumer.start())
        ...
        await consumer.stop()
    """

    def __init__(self) -> None:
        self._settings = get_settings()
        self._consumer: Optional[AIOKafkaConsumer] = None
        self._redis: Optional[aioredis.Redis] = None
        self._running: bool = False

    # ── Public lifecycle methods ──────────────────────────────────────────────

    async def start(self) -> None:
        """
        Initialise the Kafka consumer and Redis client, then enter the
        message processing loop.

        This method is designed to be run as a background asyncio task.
        It handles its own exceptions so a transient Kafka or DB error
        will not crash the FastAPI process — it logs the error, waits
        briefly, then retries.
        """
        logger.info(
            "config_sync_consumer_starting",
            extra={
                "bootstrap_servers": self._settings.KAFKA_BOOTSTRAP_SERVERS,
                "group_id": self._settings.KAFKA_CONFIG_GROUP_ID,
                "topics": CONFIG_TOPICS,
            },
        )

        self._redis = aioredis.from_url(
            self._settings.REDIS_URL,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=5,
        )

        self._consumer = AIOKafkaConsumer(
            *CONFIG_TOPICS,
            bootstrap_servers=self._settings.KAFKA_BOOTSTRAP_SERVERS,
            group_id=self._settings.KAFKA_CONFIG_GROUP_ID,
            # Replay the full compacted topic from the beginning when the
            # consumer group has no committed offset (fresh deployment).
            auto_offset_reset="earliest",
            # Disable auto-commit so we control exactly when offsets are
            # committed: only AFTER the DB-2 transaction succeeds.
            enable_auto_commit=False,
            # Decode message values as UTF-8 strings; keys may be None.
            value_deserializer=lambda raw: raw.decode("utf-8") if raw else None,
            key_deserializer=lambda raw: raw.decode("utf-8") if raw else None,
        )

        self._running = True

        # Outer retry loop: if the consumer crashes (e.g. Kafka broker
        # unavailable at startup), wait and retry rather than dying.
        while self._running:
            try:
                await self._consumer.start()
                logger.info(
                    "config_sync_consumer_started",
                    extra={"topics": CONFIG_TOPICS},
                )
                await self._consume_loop()
            except KafkaError as exc:
                logger.error(
                    "config_sync_kafka_error",
                    extra={"error": str(exc)},
                    exc_info=True,
                )
            except asyncio.CancelledError:
                # Raised when the background task is cancelled during shutdown.
                logger.info("config_sync_consumer_cancelled")
                break
            except Exception as exc:
                logger.error(
                    "config_sync_unexpected_error",
                    extra={"error": str(exc)},
                    exc_info=True,
                )
            finally:
                # Always attempt to stop the consumer before retrying or exiting.
                await self._safe_stop_consumer()

            if self._running:
                logger.info(
                    "config_sync_consumer_retrying",
                    extra={"retry_delay_seconds": 5},
                )
                await asyncio.sleep(5)

        logger.info("config_sync_consumer_stopped")

    async def stop(self) -> None:
        """
        Signal the consumer loop to exit and wait for a clean shutdown.
        Call this from the FastAPI lifespan shutdown block.
        """
        logger.info("config_sync_consumer_stopping")
        self._running = False
        await self._safe_stop_consumer()
        if self._redis:
            await self._redis.aclose()
            self._redis = None

    # ── Internal: core consume loop ───────────────────────────────────────────

    async def _consume_loop(self) -> None:
        """
        Core message processing loop.

        Iterates over incoming Kafka messages.  For each message:
          1. Parse the JSON payload.
          2. Route to the appropriate handler based on topic.
          3. Commit the Kafka offset only after DB-2 is updated.

        Any per-message error (bad JSON, unknown schema, DB constraint
        violation) is caught and logged without crashing the loop — the
        offset is NOT committed so the message will be retried on next start.

        Note on offset commit strategy:
        `enable_auto_commit=False` means we call `consumer.commit()` manually
        after each successful DB write.  If the process crashes mid-write,
        the message will be re-delivered (at-least-once semantics) and the
        staleness check in each handler will discard the duplicate safely.
        """
        async for message in self._consumer:
            if not self._running:
                break

            topic = message.topic
            raw_value = message.value

            logger.debug(
                "config_sync_message_received",
                extra={
                    "topic": topic,
                    "partition": message.partition,
                    "offset": message.offset,
                    "key": message.key,
                },
            )

            # ── 1. Parse JSON ─────────────────────────────────────────────────
            try:
                payload: Dict[str, Any] = json.loads(raw_value)
            except (json.JSONDecodeError, TypeError) as exc:
                logger.error(
                    "config_sync_json_decode_error",
                    extra={
                        "topic": topic,
                        "offset": message.offset,
                        "raw_value": raw_value[:200] if raw_value else None,
                        "error": str(exc),
                    },
                )
                # Commit and skip — a permanently malformed message should not
                # block the consumer indefinitely.
                await self._consumer.commit()
                continue

            # ── 2. Route to handler ───────────────────────────────────────────
            try:
                if topic == TOPIC_TENANTS:
                    await self._handle_tenant_event(payload)
                elif topic == TOPIC_ALERT_RULES:
                    await self._handle_alert_rule_event(payload)
                elif topic == TOPIC_PLAYBOOKS:
                    await self._handle_playbook_event(payload)
                else:
                    logger.warning(
                        "config_sync_unknown_topic",
                        extra={"topic": topic},
                    )
            except KeyError as exc:
                logger.error(
                    "config_sync_missing_field",
                    extra={
                        "topic": topic,
                        "offset": message.offset,
                        "missing_key": str(exc),
                        "payload_keys": list(payload.keys()),
                    },
                )
                # Do NOT commit — allow retry on restart in case it was a
                # transient schema mismatch. If this persists, a DLQ alert
                # (Phase 8) will catch it.
                continue
            except Exception as exc:
                logger.error(
                    "config_sync_handler_error",
                    extra={
                        "topic": topic,
                        "offset": message.offset,
                        "error": str(exc),
                    },
                    exc_info=True,
                )
                # Do not commit; will be retried on restart.
                continue

            # ── 3. Commit offset (only after successful DB write) ─────────────
            await self._consumer.commit()
            logger.debug(
                "config_sync_offset_committed",
                extra={
                    "topic": topic,
                    "partition": message.partition,
                    "offset": message.offset,
                },
            )

    # ── Internal: per-topic handlers ──────────────────────────────────────────

    async def _handle_tenant_event(self, payload: Dict[str, Any]) -> None:
        """
        Process a config.tenants event.

        Expected payload shape (written by Django's write_outbox helper):
        {
            "event_type": "tenant.updated" | "tenant.created" | "tenant.suspended" | "tenant.reinstated",
            "tenant": {
                "id": "<uuid>",
                "plan_tier": "free" | "pro" | "enterprise",
                "vector_namespace": "<string>",
                "kafka_group_id": "<string>",
                "is_suspended": false,
                "source_version": <int>
            }
        }

        Deletion of a tenant is out-of-scope for this consumer (tenant
        deletion is an admin-only super-admin action handled separately).
        """
        tenant_data: Dict[str, Any] = payload["tenant"]

        tenant_id = uuid.UUID(str(tenant_data["id"]))
        incoming_version: int = int(tenant_data["source_version"])

        async with SessionLocal() as session:
            async with session.begin():
                existing = await self._get_tenant_snapshot(session, tenant_id)

                # ── Staleness check ───────────────────────────────────────────
                if existing is not None:
                    if existing.source_version is not None and existing.source_version >= incoming_version:
                        logger.warning(
                            "config_sync_stale_tenant_event",
                            extra={
                                "tenant_id": str(tenant_id),
                                "existing_version": existing.source_version,
                                "incoming_version": incoming_version,
                            },
                        )
                        return  # Discard stale event

                    # ── Update existing snapshot ──────────────────────────────
                    existing.plan_tier = tenant_data.get("plan_tier", existing.plan_tier)
                    existing.vector_namespace = tenant_data.get("vector_namespace", existing.vector_namespace)
                    existing.kafka_group_id = tenant_data.get("kafka_group_id", existing.kafka_group_id)
                    existing.is_suspended = bool(tenant_data.get("is_suspended", existing.is_suspended))
                    existing.source_version = incoming_version
                    session.add(existing)

                    logger.info(
                        "config_sync_tenant_updated",
                        extra={
                            "tenant_id": str(tenant_id),
                            "source_version": incoming_version,
                        },
                    )
                else:
                    # ── Create new snapshot ───────────────────────────────────
                    snapshot = TenantSnapshot(
                        tenant_id=tenant_id,
                        plan_tier=tenant_data.get("plan_tier"),
                        vector_namespace=tenant_data.get("vector_namespace"),
                        kafka_group_id=tenant_data.get("kafka_group_id"),
                        is_suspended=bool(tenant_data.get("is_suspended", False)),
                        source_version=incoming_version,
                    )
                    session.add(snapshot)

                    logger.info(
                        "config_sync_tenant_created",
                        extra={
                            "tenant_id": str(tenant_id),
                            "source_version": incoming_version,
                        },
                    )

        # ── Redis L1 cache invalidation (outside the DB transaction) ──────────
        await self._invalidate_tenant_cache(str(tenant_id))

    async def _handle_alert_rule_event(self, payload: Dict[str, Any]) -> None:
        """
        Process a config.alert_rules event.

        Expected payload shape:
        {
            "event_type": "alert_rule.created" | "alert_rule.updated" | "alert_rule.deleted",
            "alert_rule": {
                "id": "<uuid>",
                "tenant_id": "<uuid>",
                "confidence_threshold": "0.85",
                "severity_filter": ["critical", "high"],
                "recipient_ids": ["<uuid>", ...],
                "enabled": true,
                "source_version": <int>,
                "deleted": false       ← present and true on deletion events
            }
        }
        """
        rule_data: Dict[str, Any] = payload["alert_rule"]

        rule_id = uuid.UUID(str(rule_data["id"]))
        tenant_id = uuid.UUID(str(rule_data["tenant_id"]))
        incoming_version: int = int(rule_data["source_version"])
        is_deleted: bool = bool(rule_data.get("deleted", False))

        async with SessionLocal() as session:
            async with session.begin():
                existing = await self._get_alert_rule_snapshot(session, rule_id)

                # ── Staleness check ───────────────────────────────────────────
                if existing is not None:
                    if existing.source_version is not None and existing.source_version >= incoming_version:
                        logger.warning(
                            "config_sync_stale_alert_rule_event",
                            extra={
                                "rule_id": str(rule_id),
                                "existing_version": existing.source_version,
                                "incoming_version": incoming_version,
                            },
                        )
                        return

                    # ── Delete ────────────────────────────────────────────────
                    if is_deleted:
                        await session.delete(existing)
                        logger.info(
                            "config_sync_alert_rule_deleted",
                            extra={
                                "rule_id": str(rule_id),
                                "tenant_id": str(tenant_id),
                            },
                        )
                    else:
                        # ── Update ────────────────────────────────────────────
                        existing.tenant_id = tenant_id
                        existing.confidence_threshold = rule_data.get(
                            "confidence_threshold", existing.confidence_threshold
                        )
                        existing.severity_filter = rule_data.get(
                            "severity_filter", existing.severity_filter
                        )
                        existing.recipient_ids = rule_data.get(
                            "recipient_ids", existing.recipient_ids
                        )
                        existing.enabled = bool(rule_data.get("enabled", existing.enabled))
                        existing.source_version = incoming_version
                        session.add(existing)

                        logger.info(
                            "config_sync_alert_rule_updated",
                            extra={
                                "rule_id": str(rule_id),
                                "tenant_id": str(tenant_id),
                                "source_version": incoming_version,
                            },
                        )
                else:
                    if is_deleted:
                        # Nothing to delete; already absent.
                        logger.debug(
                            "config_sync_alert_rule_delete_noop",
                            extra={"rule_id": str(rule_id)},
                        )
                        return

                    # ── Create ────────────────────────────────────────────────
                    snapshot = AlertRuleSnapshot(
                        rule_id=rule_id,
                        tenant_id=tenant_id,
                        confidence_threshold=rule_data.get("confidence_threshold"),
                        severity_filter=rule_data.get("severity_filter"),
                        recipient_ids=rule_data.get("recipient_ids"),
                        enabled=bool(rule_data.get("enabled", True)),
                        source_version=incoming_version,
                    )
                    session.add(snapshot)

                    logger.info(
                        "config_sync_alert_rule_created",
                        extra={
                            "rule_id": str(rule_id),
                            "tenant_id": str(tenant_id),
                            "source_version": incoming_version,
                        },
                    )

        # ── Redis L1 cache invalidation ───────────────────────────────────────
        await self._invalidate_tenant_cache(str(tenant_id))

    async def _handle_playbook_event(self, payload: Dict[str, Any]) -> None:
        """
        Process a config.playbooks event.

        Expected payload shape:
        {
            "event_type": "playbook.created" | "playbook.updated" | "playbook.deleted",
            "playbook": {
                "id": "<uuid>",
                "tenant_id": "<uuid>",
                "error_pattern": "NullPointerException.*service",
                "instructions": "Check the null guard on line ...",
                "source_version": <int>,
                "deleted": false
            }
        }
        """
        playbook_data: Dict[str, Any] = payload["playbook"]

        playbook_id = uuid.UUID(str(playbook_data["id"]))
        tenant_id = uuid.UUID(str(playbook_data["tenant_id"]))
        incoming_version: int = int(playbook_data["source_version"])
        is_deleted: bool = bool(playbook_data.get("deleted", False))

        async with SessionLocal() as session:
            async with session.begin():
                existing = await self._get_playbook_snapshot(session, playbook_id)

                # ── Staleness check ───────────────────────────────────────────
                if existing is not None:
                    if existing.source_version is not None and existing.source_version >= incoming_version:
                        logger.warning(
                            "config_sync_stale_playbook_event",
                            extra={
                                "playbook_id": str(playbook_id),
                                "existing_version": existing.source_version,
                                "incoming_version": incoming_version,
                            },
                        )
                        return

                    # ── Delete ────────────────────────────────────────────────
                    if is_deleted:
                        await session.delete(existing)
                        logger.info(
                            "config_sync_playbook_deleted",
                            extra={
                                "playbook_id": str(playbook_id),
                                "tenant_id": str(tenant_id),
                            },
                        )
                    else:
                        # ── Update ────────────────────────────────────────────
                        existing.tenant_id = tenant_id
                        existing.error_pattern = playbook_data.get(
                            "error_pattern", existing.error_pattern
                        )
                        existing.instructions = playbook_data.get(
                            "instructions", existing.instructions
                        )
                        existing.source_version = incoming_version
                        session.add(existing)

                        logger.info(
                            "config_sync_playbook_updated",
                            extra={
                                "playbook_id": str(playbook_id),
                                "tenant_id": str(tenant_id),
                                "source_version": incoming_version,
                            },
                        )
                else:
                    if is_deleted:
                        logger.debug(
                            "config_sync_playbook_delete_noop",
                            extra={"playbook_id": str(playbook_id)},
                        )
                        return

                    # ── Create ────────────────────────────────────────────────
                    snapshot = PlaybookSnapshot(
                        playbook_id=playbook_id,
                        tenant_id=tenant_id,
                        error_pattern=playbook_data.get("error_pattern"),
                        instructions=playbook_data.get("instructions"),
                        source_version=incoming_version,
                    )
                    session.add(snapshot)

                    logger.info(
                        "config_sync_playbook_created",
                        extra={
                            "playbook_id": str(playbook_id),
                            "tenant_id": str(tenant_id),
                            "source_version": incoming_version,
                        },
                    )

        # ── Redis L1 cache invalidation ───────────────────────────────────────
        await self._invalidate_tenant_cache(str(tenant_id))

    # ── Internal: DB query helpers ────────────────────────────────────────────

    @staticmethod
    async def _get_tenant_snapshot(
        session: AsyncSession, tenant_id: uuid.UUID
    ) -> Optional[TenantSnapshot]:
        result = await session.execute(
            select(TenantSnapshot).where(TenantSnapshot.tenant_id == tenant_id)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def _get_alert_rule_snapshot(
        session: AsyncSession, rule_id: uuid.UUID
    ) -> Optional[AlertRuleSnapshot]:
        result = await session.execute(
            select(AlertRuleSnapshot).where(AlertRuleSnapshot.rule_id == rule_id)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def _get_playbook_snapshot(
        session: AsyncSession, playbook_id: uuid.UUID
    ) -> Optional[PlaybookSnapshot]:
        result = await session.execute(
            select(PlaybookSnapshot).where(PlaybookSnapshot.playbook_id == playbook_id)
        )
        return result.scalar_one_or_none()

    # ── Internal: Redis cache invalidation ───────────────────────────────────

    async def _invalidate_tenant_cache(self, tenant_id: str) -> None:
        """
        Delete the Redis L1 cache key for the given tenant's aggregated config.

        Key: tenant:{tenant_id}:config
        TTL: 1 hour (set by the API layer on next cache-miss read)

        We DELETE rather than re-populate because:
          - Cache aggregation (alert rules + playbooks + tenant config) is
            owned by the API dependency layer, not by this consumer.
          - Avoids a second DB read inside the consumer hot path.
          - Ensures the cache is always built from a freshly committed state.

        Redis errors are caught and logged; a cache invalidation failure
        is non-fatal (the DB-2 snapshot is already updated and authoritative).
        """
        if not self._redis:
            return

        key = self._settings.tenant_config_cache_key(tenant_id)
        try:
            await self._redis.delete(key)
            logger.debug(
                "config_sync_cache_invalidated",
                extra={"redis_key": key, "tenant_id": tenant_id},
            )
        except Exception as exc:
            logger.error(
                "config_sync_cache_invalidation_failed",
                extra={
                    "redis_key": key,
                    "tenant_id": tenant_id,
                    "error": str(exc),
                },
            )

    # ── Internal: safe consumer stop ─────────────────────────────────────────

    async def _safe_stop_consumer(self) -> None:
        """Stop the AIOKafkaConsumer, suppressing errors during shutdown."""
        if self._consumer:
            try:
                await self._consumer.stop()
            except Exception as exc:
                logger.warning(
                    "config_sync_consumer_stop_error",
                    extra={"error": str(exc)},
                )
            finally:
                self._consumer = None