"""
app/api/dependencies/tenant.py

FastAPI dependency: resolve and validate the current request's tenant.

Two-layer validation:

  Layer 1 — Redis suspension flag (authoritative, ~5 s staleness SLO):
      Django writes `tenant:{id}:suspended = true` directly to Redis on
      suspend; deletes the key on reinstate.  Checked on *every* request.

  Layer 1b — Redis L1 config cache (`tenant:{id}:config`, 1-hour TTL):
      A serialised JSON snapshot of the TenantSnapshot row is stored here
      after the first successful DB-2 read.  All subsequent requests within
      the TTL window are served from Redis without touching Postgres.
      Cache invalidation is handled by ConfigSyncConsumer after every
      successful snapshot upsert — this module only handles reads and
      cache-population writes.

  Layer 2 — DB-2 `tenant_snapshots` (eventual-consistent, ~60 s lag):
      On a Redis cache miss the snapshot row is fetched from Postgres,
      serialised, and written to the Redis L1 cache before being returned.

Usage in route functions:
    from app.api.dependencies.tenant import get_validated_tenant
    from app.models.snapshots import TenantSnapshot

    @router.post("/ingest/logs")
    async def ingest_logs(
        tenant: TenantSnapshot = Depends(get_validated_tenant),
        ...
    ):
        ...

Architecture reference: NeuralOps Technical Documentation — Sections 5, 7, 13
"""

from __future__ import annotations

import json
import uuid
from typing import Annotated, Optional

import redis.asyncio as aioredis
from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import (
    TenantConfigStaleError,
    TenantNotFoundError,
    TenantSuspendedError,
    TokenMissingError,
)
from app.core.logging import get_logger
from app.database.session import get_db
from app.models.snapshots import TenantSnapshot

logger = get_logger(__name__)

_settings = get_settings()
_redis_client: Optional[aioredis.Redis] = None

# ── Redis client singleton ─────────────────────────────────────────────────────


def get_redis() -> aioredis.Redis:
    """Return the module-level async Redis client, creating it on first call."""
    global _redis_client
    if _redis_client is None:
        _redis_client = aioredis.from_url(
            _settings.REDIS_URL,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
    return _redis_client


# ── Suspension check (Layer 1) ─────────────────────────────────────────────────


async def _check_suspension_flag(tenant_id: str) -> None:
    """
    Check Redis for the authoritative tenant suspension flag.

    Key: tenant:{tenant_id}:suspended
    No TTL — persists until Django explicitly deletes it on reinstate.

    Raises TenantSuspendedError if the key exists.
    Falls through silently if Redis is unavailable (fail-open on Redis
    errors for the suspension check; snapshot table is the fallback).
    """
    redis = get_redis()
    key = _settings.tenant_suspension_key(tenant_id)

    try:
        value = await redis.get(key)
    except Exception as exc:
        logger.error(
            "redis_suspension_check_failed",
            tenant_id=tenant_id,
            error=str(exc),
            detail="Falling back to snapshot table suspension flag.",
        )
        return

    if value is not None:
        logger.warning("tenant_suspended_via_redis", tenant_id=tenant_id)
        raise TenantSuspendedError(
            f"Tenant {tenant_id} is suspended. Contact support for assistance."
        )


# ── Redis L1 cache helpers (Layer 1b) ─────────────────────────────────────────

_CACHE_TTL_SECONDS = 3600  # 1 hour


def _snapshot_to_dict(snapshot: TenantSnapshot) -> dict:
    """
    Serialise a TenantSnapshot ORM instance to a plain dict suitable for
    JSON storage in Redis.  Only the fields needed by downstream consumers
    are included.
    """
    return {
        "tenant_id": str(snapshot.tenant_id),
        "plan_tier": snapshot.plan_tier,
        "vector_namespace": snapshot.vector_namespace,
        "kafka_group_id": snapshot.kafka_group_id,
        "is_suspended": snapshot.is_suspended,
        "source_version": snapshot.source_version,
    }


def _dict_to_snapshot(data: dict) -> TenantSnapshot:
    """
    Reconstruct a *detached* (not session-bound) TenantSnapshot instance
    from a cached dict.  The instance is safe to read from but must not
    be used for ORM write operations.
    """
    snapshot = TenantSnapshot()
    snapshot.tenant_id = uuid.UUID(data["tenant_id"])
    snapshot.plan_tier = data.get("plan_tier")
    snapshot.vector_namespace = data.get("vector_namespace")
    snapshot.kafka_group_id = data.get("kafka_group_id")
    snapshot.is_suspended = bool(data.get("is_suspended", False))
    snapshot.source_version = data.get("source_version")
    return snapshot


async def _get_cached_snapshot(tenant_id: str) -> Optional[TenantSnapshot]:
    """
    Attempt to load the tenant snapshot from the Redis L1 cache.

    Returns a detached TenantSnapshot on a cache hit, or None on a miss
    or any Redis error (allows the request to fall through to Postgres).
    """
    redis = get_redis()
    key = _settings.tenant_config_cache_key(tenant_id)

    try:
        raw = await redis.get(key)
    except Exception as exc:
        logger.warning(
            "redis_l1_cache_read_failed",
            tenant_id=tenant_id,
            error=str(exc),
            detail="Falling back to Postgres snapshot lookup.",
        )
        return None

    if raw is None:
        logger.debug("redis_l1_cache_miss", tenant_id=tenant_id)
        return None

    try:
        data = json.loads(raw)
        snapshot = _dict_to_snapshot(data)
        logger.debug("redis_l1_cache_hit", tenant_id=tenant_id)
        return snapshot
    except (json.JSONDecodeError, KeyError, ValueError) as exc:
        # Corrupt cache entry — treat as a miss so Postgres repairs it.
        logger.warning(
            "redis_l1_cache_corrupt",
            tenant_id=tenant_id,
            error=str(exc),
            detail="Ignoring corrupt cache entry; falling back to Postgres.",
        )
        return None


async def _populate_cache(tenant_id: str, snapshot: TenantSnapshot) -> None:
    """
    Write a freshly-fetched TenantSnapshot to the Redis L1 cache.

    TTL is set to _CACHE_TTL_SECONDS (3600 s / 1 hour).
    Cache write errors are logged but never propagated — a failed cache
    write does not prevent a successful response.
    """
    redis = get_redis()
    key = _settings.tenant_config_cache_key(tenant_id)

    try:
        payload = json.dumps(_snapshot_to_dict(snapshot))
        await redis.setex(key, _CACHE_TTL_SECONDS, payload)
        logger.debug(
            "redis_l1_cache_populated",
            tenant_id=tenant_id,
            ttl_seconds=_CACHE_TTL_SECONDS,
        )
    except Exception as exc:
        logger.warning(
            "redis_l1_cache_write_failed",
            tenant_id=tenant_id,
            error=str(exc),
            detail="Cache population failed; request succeeded via Postgres.",
        )


# ── Postgres snapshot lookup (Layer 2) ────────────────────────────────────────


async def _get_tenant_snapshot(
    tenant_id: str,
    db: AsyncSession,
) -> TenantSnapshot:
    """
    Fetch the tenant snapshot from DB-2 (cache-miss path).

    Read-through logic:
      1. Check the Redis L1 cache.
      2. On a hit, return the cached snapshot without touching Postgres.
      3. On a miss, query Postgres, populate the cache, and return the row.

    Raises:
        TenantNotFoundError     — invalid UUID format.
        TenantConfigStaleError  — snapshot row missing (Kafka consumer lag).
        TenantSuspendedError    — snapshot.is_suspended is True (fallback).
    """
    # ── Step 1: validate UUID ─────────────────────────────────────────────────
    try:
        tenant_uuid = uuid.UUID(tenant_id)
    except ValueError as exc:
        raise TenantNotFoundError(f"'{tenant_id}' is not a valid tenant UUID.") from exc

    # ── Step 2: Redis L1 cache check ─────────────────────────────────────────
    cached = await _get_cached_snapshot(tenant_id)
    if cached is not None:
        # Still honour the snapshot-level suspension flag even from cache.
        if cached.is_suspended:
            logger.warning(
                "tenant_suspended_via_cached_snapshot",
                tenant_id=tenant_id,
            )
            raise TenantSuspendedError(
                f"Tenant {tenant_id} is suspended. Contact support for assistance."
            )
        return cached

    # ── Step 3: Postgres lookup (cache miss) ──────────────────────────────────
    logger.debug("postgres_snapshot_lookup", tenant_id=tenant_id)

    result = await db.execute(
        select(TenantSnapshot).where(TenantSnapshot.tenant_id == tenant_uuid)
    )
    snapshot: Optional[TenantSnapshot] = result.scalar_one_or_none()

    if snapshot is None:
        logger.warning(
            "tenant_snapshot_missing",
            tenant_id=tenant_id,
            detail=(
                "Snapshot not found in DB-2. "
                "Kafka config-sync consumer may be lagging."
            ),
        )
        raise TenantConfigStaleError(
            f"Tenant configuration for {tenant_id} is not yet available. "
            "Please retry in a few moments."
        )

    # Eventual-consistent suspension fallback (Redis check already passed).
    if snapshot.is_suspended:
        logger.warning(
            "tenant_suspended_via_snapshot",
            tenant_id=tenant_id,
            detail="Redis suspension key absent but snapshot.is_suspended=True.",
        )
        raise TenantSuspendedError(
            f"Tenant {tenant_id} is suspended. Contact support for assistance."
        )

    # ── Step 4: Populate Redis L1 cache for subsequent requests ──────────────
    await _populate_cache(tenant_id, snapshot)

    return snapshot


# ── Main dependency ────────────────────────────────────────────────────────────


async def get_validated_tenant(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> TenantSnapshot:
    """
    FastAPI dependency: validate the current request's tenant.

    Resolution order:
      1. Read tenant_id from request.state (set by JWTAuthMiddleware).
      2. Check Redis suspension flag (authoritative, ~5 s staleness SLO).
      3. Check Redis L1 config cache (`tenant:{id}:config`, 1-hour TTL).
      4. On cache miss: fetch snapshot from DB-2 Postgres, populate cache.
      5. Check snapshot.is_suspended as eventual-consistent fallback.

    Returns:
        TenantSnapshot — may be a detached instance reconstructed from
        the Redis cache or a session-bound ORM instance from Postgres.

    Raises:
        TokenMissingError       — no tenant_id in request.state.
        TenantSuspendedError    — tenant is suspended (Redis or snapshot).
        TenantConfigStaleError  — snapshot not yet available in DB-2.
        TenantNotFoundError     — invalid tenant UUID format.
    """
    tenant_id: str = getattr(request.state, "tenant_id", "")

    if not tenant_id:
        raise TokenMissingError(
            "Tenant context is missing from the request. "
            "Ensure the request is authenticated."
        )

    logger.debug("tenant_validation_start", tenant_id=tenant_id)

    # Layer 1: Redis suspension flag (fast, authoritative)
    await _check_suspension_flag(tenant_id)

    # Layer 1b + 2: Redis L1 cache → Postgres read-through
    snapshot = await _get_tenant_snapshot(tenant_id, db)

    logger.debug(
        "tenant_validation_success",
        tenant_id=tenant_id,
        plan_tier=snapshot.plan_tier,
        source="cache" if snapshot.source_version else "postgres",
    )

    return snapshot


# ── Annotated type alias ───────────────────────────────────────────────────────
ValidatedTenant = Annotated[TenantSnapshot, Depends(get_validated_tenant)]
