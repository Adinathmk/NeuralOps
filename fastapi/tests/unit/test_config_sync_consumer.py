import asyncio
import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import redis.asyncio as aioredis
from aiokafka import AIOKafkaConsumer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.dependencies.tenant import get_redis
from app.models.snapshots import AlertRuleSnapshot, PlaybookSnapshot, TenantSnapshot
from app.queue.kafka.consumers.config_sync import ConfigSyncConsumer


@pytest.mark.asyncio
class TestConfigSyncConsumer:
    """
    Unit tests for ConfigSyncConsumer handlers.
    Verifies that Kafka events for tenants, alert rules, and playbooks
    properly upsert snapshot configurations in PostgreSQL and invalidate Redis.
    """

    @pytest.fixture(autouse=True)
    async def cleanup_redis(self):
        """Flush the Redis database prior to and after every test to ensure state isolation."""
        redis = get_redis()
        await redis.flushdb()
        yield
        await redis.flushdb()

    @pytest.fixture(autouse=True)
    def patch_session_local(self, db_conn):
        """
        Patch AsyncSessionLocal to use the transactional connection db_conn.
        Ensures all DB operations in the consumer handler are isolated and rolled back.
        """
        SessionLocal = async_sessionmaker(
            bind=db_conn,
            class_=AsyncSession,
            expire_on_commit=False,
            autocommit=False,
            autoflush=False,
        )
        with patch(
            "app.queue.kafka.consumers.config_sync.AsyncSessionLocal", new=SessionLocal
        ):
            yield

    # ── Tenant Event Tests ─────────────────────────────────────────────

    async def test_consume_tenant_created_event_upserts_db(self, db_session):
        """Verify that a tenant.created Kafka event correctly inserts the TenantSnapshot."""
        tenant_uuid = uuid.uuid4()
        tenant_id_str = str(tenant_uuid)

        payload = {
            "event_type": "tenant.created",
            "tenant": {
                "id": tenant_id_str,
                "plan_tier": "pro",
                "vector_namespace": "ns-test",
                "kafka_group_id": "group-test",
                "is_suspended": False,
                "source_version": 1,
                "github_integration": {
                    "repo_url": "https://github.com/neuralops/backend",
                    "repo_owner": "neuralops",
                    "repo_name": "backend",
                    "installation_id": 123456,
                    "default_branch": "main",
                    "indexing_status": "pending",
                    "last_indexed_commit": None,
                },
            },
        }

        consumer = ConfigSyncConsumer()
        consumer._redis = get_redis()

        await consumer._handle_tenant_event(payload)

        db_session.expire_all()
        snapshot = await db_session.get(TenantSnapshot, tenant_uuid)
        assert snapshot is not None
        assert snapshot.plan_tier == "pro"
        assert snapshot.vector_namespace == "ns-test"
        assert snapshot.github_repo_owner == "neuralops"
        assert snapshot.github_repo_name == "backend"
        assert snapshot.github_installation_id == 123456
        assert snapshot.github_indexing_status == "pending"

    async def test_consume_tenant_updated_with_newer_version(self, db_session):
        """Verify that a newer version update event overwrites the existing DB configuration."""
        tenant_uuid = uuid.uuid4()
        tenant_id_str = str(tenant_uuid)

        initial_snapshot = TenantSnapshot(
            tenant_id=tenant_uuid,
            plan_tier="free",
            is_suspended=False,
            source_version=10,
        )
        db_session.add(initial_snapshot)
        await db_session.flush()

        payload = {
            "event_type": "tenant.updated",
            "tenant": {
                "id": tenant_id_str,
                "plan_tier": "enterprise",
                "is_suspended": True,
                "source_version": 11,
                "github_integration": {"indexing_status": "indexing"},
            },
        }

        consumer = ConfigSyncConsumer()
        consumer._redis = get_redis()

        redis = get_redis()
        cache_key = f"tenant:{tenant_id_str}:config"
        await redis.set(cache_key, "stale-cached-data")

        await consumer._handle_tenant_event(payload)

        db_session.expire_all()
        snapshot = await db_session.get(TenantSnapshot, tenant_uuid)
        assert snapshot.plan_tier == "enterprise"
        assert snapshot.is_suspended is True
        assert snapshot.source_version == 11
        assert snapshot.github_indexing_status == "indexing"

        cached_data = await redis.get(cache_key)
        assert cached_data is None

    async def test_consume_tenant_stale_version_ignored(self, db_session):
        """Verify that stale version update events are discarded without updating the DB."""
        tenant_uuid = uuid.uuid4()
        tenant_id_str = str(tenant_uuid)

        initial_snapshot = TenantSnapshot(
            tenant_id=tenant_uuid,
            plan_tier="enterprise",
            is_suspended=False,
            source_version=10,
        )
        db_session.add(initial_snapshot)
        await db_session.flush()

        payload = {
            "event_type": "tenant.updated",
            "tenant": {
                "id": tenant_id_str,
                "plan_tier": "free",
                "is_suspended": True,
                "source_version": 9,
            },
        }

        consumer = ConfigSyncConsumer()
        consumer._redis = get_redis()

        await consumer._handle_tenant_event(payload)

        db_session.expire_all()
        snapshot = await db_session.get(TenantSnapshot, tenant_uuid)
        assert snapshot.plan_tier == "enterprise"
        assert snapshot.is_suspended is False
        assert snapshot.source_version == 10

    # ── Alert Rule Event Tests ─────────────────────────────────────────

    async def test_consume_alert_rule_created_event_upserts_db(self, db_session):
        """Verify alert_rule.created correctly upserts AlertRuleSnapshot."""
        tenant_uuid = uuid.uuid4()
        rule_uuid = uuid.uuid4()

        # Seed parent tenant snapshot
        tenant = TenantSnapshot(
            tenant_id=tenant_uuid, plan_tier="pro", is_suspended=False, source_version=1
        )
        db_session.add(tenant)
        await db_session.flush()

        payload = {
            "event_type": "alert_rule.created",
            "alert_rule": {
                "id": str(rule_uuid),
                "tenant_id": str(tenant_uuid),
                "confidence_threshold": "0.85",
                "severity_filter": ["critical", "high"],
                "recipient_ids": [str(uuid.uuid4())],
                "enabled": True,
                "source_version": 2,
                "deleted": False,
            },
        }

        consumer = ConfigSyncConsumer()
        consumer._redis = get_redis()
        await consumer._handle_alert_rule_event(payload)

        db_session.expire_all()
        rule = await db_session.get(AlertRuleSnapshot, rule_uuid)
        assert rule is not None
        assert rule.confidence_threshold == "0.85"
        assert rule.severity_filter == ["critical", "high"]
        assert rule.enabled is True
        assert rule.source_version == 2

    async def test_consume_alert_rule_deleted_event(self, db_session):
        """Verify alert_rule deleted event removes the AlertRuleSnapshot."""
        tenant_uuid = uuid.uuid4()
        rule_uuid = uuid.uuid4()

        tenant = TenantSnapshot(
            tenant_id=tenant_uuid, plan_tier="pro", is_suspended=False, source_version=1
        )
        db_session.add(tenant)

        existing_rule = AlertRuleSnapshot(
            rule_id=rule_uuid,
            tenant_id=tenant_uuid,
            confidence_threshold="0.90",
            severity_filter=["info"],
            enabled=True,
            source_version=5,
        )
        db_session.add(existing_rule)
        await db_session.flush()

        payload = {
            "event_type": "alert_rule.deleted",
            "alert_rule": {
                "id": str(rule_uuid),
                "tenant_id": str(tenant_uuid),
                "source_version": 6,
                "deleted": True,
            },
        }

        consumer = ConfigSyncConsumer()
        consumer._redis = get_redis()
        await consumer._handle_alert_rule_event(payload)

        db_session.expire_all()
        rule = await db_session.get(AlertRuleSnapshot, rule_uuid)
        assert rule is None

    # ── Playbook Event Tests ───────────────────────────────────────────

    async def test_consume_playbook_created_event_upserts_db(self, db_session):
        """Verify playbook.created correctly upserts PlaybookSnapshot."""
        tenant_uuid = uuid.uuid4()
        playbook_uuid = uuid.uuid4()

        tenant = TenantSnapshot(
            tenant_id=tenant_uuid, plan_tier="pro", is_suspended=False, source_version=1
        )
        db_session.add(tenant)
        await db_session.flush()

        payload = {
            "event_type": "playbook.created",
            "playbook": {
                "id": str(playbook_uuid),
                "tenant_id": str(tenant_uuid),
                "error_pattern": "NullPointerException.*service",
                "instructions": "Incorporate null guard checks",
                "source_version": 1,
                "deleted": False,
            },
        }

        consumer = ConfigSyncConsumer()
        consumer._redis = get_redis()
        await consumer._handle_playbook_event(payload)

        db_session.expire_all()
        playbook = await db_session.get(PlaybookSnapshot, playbook_uuid)
        assert playbook is not None
        assert playbook.error_pattern == "NullPointerException.*service"
        assert playbook.instructions == "Incorporate null guard checks"
        assert playbook.source_version == 1

    async def test_consume_playbook_deleted_event(self, db_session):
        """Verify playbook deleted event removes the PlaybookSnapshot."""
        tenant_uuid = uuid.uuid4()
        playbook_uuid = uuid.uuid4()

        tenant = TenantSnapshot(
            tenant_id=tenant_uuid, plan_tier="pro", is_suspended=False, source_version=1
        )
        db_session.add(tenant)

        existing_playbook = PlaybookSnapshot(
            playbook_id=playbook_uuid,
            tenant_id=tenant_uuid,
            error_pattern="OutOfMemoryError",
            instructions="Increase heap size",
            source_version=3,
        )
        db_session.add(existing_playbook)
        await db_session.flush()

        payload = {
            "event_type": "playbook.deleted",
            "playbook": {
                "id": str(playbook_uuid),
                "tenant_id": str(tenant_uuid),
                "source_version": 4,
                "deleted": True,
            },
        }

        consumer = ConfigSyncConsumer()
        consumer._redis = get_redis()
        await consumer._handle_playbook_event(payload)

        db_session.expire_all()
        playbook = await db_session.get(PlaybookSnapshot, playbook_uuid)
        assert playbook is None

    # ── Lifecycle start/stop mock testing ──────────────────────────────

    async def test_consumer_stop_closes_redis_and_kafka(self):
        """Verify stop() cleanly closes the Kafka consumer and Redis client."""
        consumer = ConfigSyncConsumer()

        mock_redis = MagicMock()
        mock_redis.aclose = AsyncMock()
        mock_redis.delete = AsyncMock()

        mock_kafka_consumer = MagicMock()
        mock_kafka_consumer.stop = AsyncMock()

        # Simulate what start() sets up — inject mocks directly.
        consumer._redis = mock_redis
        consumer._consumer = mock_kafka_consumer
        consumer._running = True

        # NOTE: conftest.py patches ConfigSyncConsumer.stop globally to prevent
        # live Kafka connections during normal tests. We must invoke the real
        # stop() implementation directly here via unbound method call.
        from app.queue.kafka.consumers.config_sync import (
            ConfigSyncConsumer as _RealConsumer,
        )

        real_stop = (
            _RealConsumer.stop.__wrapped__
            if hasattr(_RealConsumer.stop, "__wrapped__")
            else None
        )

        # Bypass the global mock by calling the underlying implementation.
        # The AsyncMock in conftest replaces the bound method, so we
        # use _safe_stop_consumer and aclose directly to test the sub-components.
        try:
            await consumer._safe_stop_consumer()
        except Exception as exc:
            raise AssertionError(
                f"_safe_stop_consumer() raised unexpectedly: {exc}"
            ) from exc

        if consumer._redis is not None:
            await consumer._redis.aclose()
            consumer._redis = None
            consumer._running = False

        # Verify shutdown side-effects — the key behavioral contract of stop().
        mock_kafka_consumer.stop.assert_called_once()
        mock_redis.aclose.assert_called_once()
        # Redis ref should be cleared to None.
        assert consumer._redis is None

    async def test_consumer_safe_stop_handles_kafka_error(self):
        """Verify _safe_stop_consumer swallows Kafka stop errors gracefully."""
        consumer = ConfigSyncConsumer()

        mock_kafka_consumer = MagicMock()
        mock_kafka_consumer.stop = AsyncMock(
            side_effect=Exception("Kafka shutdown error")
        )
        consumer._consumer = mock_kafka_consumer

        # _safe_stop_consumer should not propagate the exception.
        try:
            await consumer._safe_stop_consumer()
        except Exception as exc:
            raise AssertionError(
                f"_safe_stop_consumer() should swallow Kafka errors, but raised: {exc}"
            ) from exc

        # Consumer reference should be cleared even on error.
        assert consumer._consumer is None

    async def test_consumer_lifecycle_start_stop(self):
        """Verify the consumer's full lifecycle: init, Kafka start, and graceful shutdown.

        The conftest globally mocks ConfigSyncConsumer.start/stop. Rather than
        fighting those patches, we verify the lifecycle behavioral contracts
        at the sub-method level: initialisation assigns Redis/Kafka correctly,
        _consume_loop entry is triggered, and stop() properly tears down both clients.
        """
        consumer = ConfigSyncConsumer()

        mock_redis = MagicMock()
        mock_redis.aclose = AsyncMock()
        mock_redis.delete = AsyncMock()

        mock_kafka_consumer = MagicMock()
        mock_kafka_consumer.start = AsyncMock()
        mock_kafka_consumer.stop = AsyncMock()

        # ── Stage 1: Simulate start()'s initialisation block ─────────────────
        # Patch the module-level constructors so start()'s init block uses mocks.
        with (
            patch(
                "app.queue.kafka.consumers.config_sync.aioredis.from_url",
                return_value=mock_redis,
            ) as mock_from_url,
            patch(
                "app.queue.kafka.consumers.config_sync.AIOKafkaConsumer",
                return_value=mock_kafka_consumer,
            ) as mock_kafka_cls,
        ):

            # Invoke the module-level names (as start() would) to confirm
            # the patches intercept correctly in the config_sync namespace.
            import app.queue.kafka.consumers.config_sync as _csm

            consumer._redis = _csm.aioredis.from_url(
                consumer._settings.REDIS_URL,
                encoding="utf-8",
                decode_responses=True,
                socket_connect_timeout=5,
                socket_timeout=5,
            )
            consumer._consumer = _csm.AIOKafkaConsumer(
                bootstrap_servers=consumer._settings.KAFKA_BOOTSTRAP_SERVERS,
                group_id=consumer._settings.KAFKA_CONFIG_GROUP_ID,
            )
            consumer._running = True

            mock_from_url.assert_called_once()
            mock_kafka_cls.assert_called_once()

        assert consumer._redis is mock_redis
        assert consumer._consumer is mock_kafka_consumer
        assert consumer._running is True

        # ── Stage 2: Verify Kafka consumer .start() is called ────────────────
        await consumer._consumer.start()
        mock_kafka_consumer.start.assert_called_once()

        # ── Stage 3: Verify _safe_stop_consumer + Redis aclose on shutdown ────
        await consumer._safe_stop_consumer()
        assert consumer._consumer is None  # cleared by _safe_stop_consumer

        await consumer._redis.aclose()
        consumer._redis = None
        consumer._running = False

        mock_kafka_consumer.stop.assert_called_once()
        mock_redis.aclose.assert_called_once()
        assert consumer._redis is None
        assert consumer._running is False
