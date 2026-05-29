import json
import uuid
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException, Request

from app.api.dependencies.tenant import get_redis, get_validated_tenant
from app.core.exceptions import (
    TenantConfigStaleError,
    TenantSuspendedError,
    TokenMissingError,
)
from app.models.snapshots import TenantSnapshot


@pytest.mark.asyncio
class TestTenantDependency:
    """
    Unit and integration tests for the multi-layer get_validated_tenant dependency.
    Validates Redis L1 caching, Postgres eventual-consistent read-through fallbacks,
    authoritative Redis suspension checks, and Starlette state extraction.
    """

    @pytest.fixture(autouse=True)
    async def cleanup_redis(self):
        """Flush the Redis database prior to every test to ensure state isolation."""
        redis = get_redis()
        await redis.flushdb()
        yield
        await redis.flushdb()

    def make_mock_request(self, tenant_id_str: str) -> Request:
        """Construct a virtual Starlette Request object with a request.state context."""
        request = MagicMock(spec=Request)
        request.state = MagicMock()
        request.state.tenant_id = tenant_id_str
        return request

    # ── 1. Caching & Read-Through Tests ───────────────────────────────────────

    async def test_get_tenant_config_cache_miss_populates_redis(self, db_session):
        """Verify GET config misses cache, reads Postgres snapshot, and populates Redis L1."""
        tenant_uuid = uuid.uuid4()
        tenant_id_str = str(tenant_uuid)

        # 1. Seed database snapshot in Postgres DB-2
        snapshot = TenantSnapshot(
            tenant_id=tenant_uuid,
            plan_tier="enterprise",
            vector_namespace="namespace-1",
            is_suspended=False,
        )
        db_session.add(snapshot)
        await db_session.flush()

        # 2. Call the dependency (Redis cache is empty, so this triggers cache miss path)
        request = self.make_mock_request(tenant_id_str)
        result = await get_validated_tenant(request, db_session)

        assert result.tenant_id == tenant_uuid
        assert result.plan_tier == "enterprise"
        assert result.is_suspended is False

        # 3. Assert Redis L1 cache was populated with serialized snaphost JSON
        redis = get_redis()
        cache_key = f"tenant:{tenant_id_str}:config"
        cached_data = await redis.get(cache_key)
        assert cached_data is not None

        parsed = json.loads(cached_data)
        assert parsed["tenant_id"] == tenant_id_str
        assert parsed["plan_tier"] == "enterprise"

    async def test_get_tenant_config_cache_hit_bypasses_postgres(self, db_session):
        """Verify GET config hits Redis L1 cache and returns snapshot without Postgres query."""
        tenant_uuid = uuid.uuid4()
        tenant_id_str = str(tenant_uuid)

        # 1. Seed Redis cache directly (but NO rows exist in PostgreSQL)
        redis = get_redis()
        cache_key = f"tenant:{tenant_id_str}:config"
        payload = {
            "tenant_id": tenant_id_str,
            "plan_tier": "professional",
            "vector_namespace": "namespace-cached",
            "is_suspended": False,
        }
        await redis.set(cache_key, json.dumps(payload))

        # 2. Call the dependency. It should hit cache instantly and bypass PostgreSQL
        request = self.make_mock_request(tenant_id_str)
        result = await get_validated_tenant(request, db_session)

        assert result.tenant_id == tenant_uuid
        assert result.plan_tier == "professional"
        assert result.vector_namespace == "namespace-cached"
        assert result.is_suspended is False

    # ── 2. Suspension flag Tests ──────────────────────────────────────────────

    async def test_tenant_suspended_authoritative_in_redis(self, db_session):
        """Verify Redis suspension flag blocks requests immediately (HTTP 403) before DB lookup."""
        tenant_uuid = uuid.uuid4()
        tenant_id_str = str(tenant_uuid)

        # 1. Seed PostgreSQL snapshot (active)
        snapshot = TenantSnapshot(
            tenant_id=tenant_uuid, plan_tier="free", is_suspended=False
        )
        db_session.add(snapshot)
        await db_session.flush()

        # 2. Set authoritative suspension flag in Redis
        redis = get_redis()
        suspended_key = f"tenant:{tenant_id_str}:suspended"
        await redis.set(suspended_key, "true")

        # 3. Call dependency and verify it throws TenantSuspendedError (HTTP 403) immediately
        request = self.make_mock_request(tenant_id_str)
        with pytest.raises(TenantSuspendedError):
            await get_validated_tenant(request, db_session)

    async def test_tenant_suspended_in_database_fallback(self, db_session):
        """Verify that database suspension acts as eventual-consistent fallback if Redis key absent."""
        tenant_uuid = uuid.uuid4()
        tenant_id_str = str(tenant_uuid)

        # Seed Postgres snapshot as suspended (is_suspended=True)
        snapshot = TenantSnapshot(
            tenant_id=tenant_uuid, plan_tier="free", is_suspended=True
        )
        db_session.add(snapshot)
        await db_session.flush()

        request = self.make_mock_request(tenant_id_str)
        with pytest.raises(TenantSuspendedError):
            await get_validated_tenant(request, db_session)

    # ── 3. Eventual Consistency / Lag Tests ───────────────────────────────────

    async def test_get_tenant_config_stale_raises_exception(self, db_session):
        """Verify missing Postgres snapshot raises TenantConfigStaleError representing consumer lag."""
        tenant_id_str = str(uuid.uuid4())

        # Call dependency with a random unseeded tenant context (cache miss and DB miss)
        request = self.make_mock_request(tenant_id_str)
        with pytest.raises(TenantConfigStaleError):
            await get_validated_tenant(request, db_session)

    async def test_get_tenant_token_missing_raises_exception(self, db_session):
        """Verify that request contexts missing tenant token context raise TokenMissingError."""
        request = MagicMock(spec=Request)
        request.state = MagicMock()
        request.state.tenant_id = ""  # Missing context

        with pytest.raises(TokenMissingError):
            await get_validated_tenant(request, db_session)
