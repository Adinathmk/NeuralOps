"""
app/api/dependencies/tenant.py

FastAPI dependency: resolve and validate the current request's tenant.

This dependency is the gatekeeper for all protected routes that interact
with tenant data. It implements the two-layer suspension check described
in the NeuralOps architecture documentation:

Layer 1 — Redis (synchronous, authoritative for suspension):
    Django writes `tenant:{id}:suspended = true` directly to Redis when
    it suspends a tenant.  This key has no TTL and is deleted on reinstate.
    FastAPI checks this key on *every* ingest/API request.
    Staleness SLO: 5 seconds (bypasses Kafka propagation entirely).

Layer 2 — DB-2 tenant_snapshots (eventual-consistent, ~60s lag):
    The snapshot table holds the full tenant configuration (plan tier,
    vector namespace, etc.).  It is populated by the Kafka consumer
    in app/queue/kafka/consumers/config_sync.py.
    If the snapshot row is missing, the dependency raises TenantConfigStaleError
    (503) — callers may choose to retry or surface a degraded-mode warning.

Usage in route functions:
    from app.api.dependencies.tenant import get_validated_tenant
    from app.models.snapshots import TenantSnapshot

    @router.post("/ingest/logs")
    async def ingest_logs(
        tenant: TenantSnapshot = Depends(get_validated_tenant),
        ...
    ):
        ...

Architecture reference: NeuralOps Technical Documentation — Sections 5, 13
"""

from __future__ import annotations

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

# ── Redis client singleton ─────────────────────────────────────────────────────
# Created once; reused across requests.  redis.asyncio is async-safe.
_settings = get_settings()
_redis_client: Optional[aioredis.Redis] = None


def get_redis() -> aioredis.Redis:
    """Return the module-level async Redis client, creating it if necessary."""
    global _redis_client
    if _redis_client is None:
        _redis_client = aioredis.from_url(
            _settings.REDIS_URL,
            encoding="utf-8",
            decode_responses=True,
            # socket_connect_timeout ensures we fail fast if Redis is down
            socket_connect_timeout=2,
            socket_timeout=2,
        )
    return _redis_client


# ── Suspension check ──────────────────────────────────────────────────────────

async def _check_suspension_flag(tenant_id: str) -> None:
    """
    Check Redis for the tenant suspension flag.

    Redis key: tenant:{tenant_id}:suspended
    Written by Django on suspend; deleted on reinstate.
    No TTL — the key persists until explicitly deleted.

    Raises TenantSuspendedError if the key exists (any value).
    """
    redis = get_redis()
    key = _settings.tenant_suspension_key(tenant_id)

    try:
        value = await redis.get(key)
    except Exception as exc:
        # Redis is unavailable. Log the failure but do NOT block the request —
        # falling back to the snapshot table's is_suspended field.
        # This matches the architecture's SLO: 5-second staleness on suspension.
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


# ── Snapshot lookup ────────────────────────────────────────────────────────────

async def _get_tenant_snapshot(
    tenant_id: str,
    db: AsyncSession,
) -> TenantSnapshot:
    """
    Fetch the tenant's snapshot row from DB-2.

    Because RLS is active, the session must already have app.tenant_id set
    (done by TenantRLSMiddleware → apply_tenant_rls_to_session).

    Raises:
        TenantNotFoundError     — tenant UUID is not a valid UUID4.
        TenantConfigStaleError  — snapshot row does not exist yet (Kafka lag).
        TenantSuspendedError    — snapshot's is_suspended flag is True
                                  (eventual-consistent fallback).
    """
    # Validate UUID format before hitting the DB
    try:
        tenant_uuid = uuid.UUID(tenant_id)
    except ValueError as exc:
        raise TenantNotFoundError(
            f"'{tenant_id}' is not a valid tenant UUID."
        ) from exc

    result = await db.execute(
        select(TenantSnapshot).where(TenantSnapshot.tenant_id == tenant_uuid)
    )
    snapshot: Optional[TenantSnapshot] = result.scalar_one_or_none()

    if snapshot is None:
        logger.warning(
            "tenant_snapshot_missing",
            tenant_id=tenant_id,
            detail=(
                "Tenant snapshot not found in DB-2. "
                "Kafka consumer may be lagging behind config.tenants topic."
            ),
        )
        raise TenantConfigStaleError(
            f"Tenant configuration for {tenant_id} is not yet available. "
            "Please retry in a few moments."
        )

    # Eventual-consistent suspension fallback (Redis check already passed)
    if snapshot.is_suspended:
        logger.warning(
            "tenant_suspended_via_snapshot",
            tenant_id=tenant_id,
            detail="Redis suspension key absent but snapshot.is_suspended=True.",
        )
        raise TenantSuspendedError(
            f"Tenant {tenant_id} is suspended. Contact support for assistance."
        )

    return snapshot


# ── Main dependency ────────────────────────────────────────────────────────────

async def get_validated_tenant(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> TenantSnapshot:
    """
    FastAPI dependency that validates the current request's tenant.

    Steps:
    1. Read tenant_id from request.state (set by JWTAuthMiddleware).
    2. Check Redis suspension flag (authoritative, ~5s staleness SLO).
    3. Fetch tenant snapshot from DB-2 (eventual-consistent, ~60s lag).
    4. Check snapshot's is_suspended flag (fallback if Redis missed it).

    Returns the TenantSnapshot instance for use in the route handler.

    Raises:
        TokenMissingError       — no tenant_id in request state.
        TenantSuspendedError    — tenant is suspended.
        TenantConfigStaleError  — snapshot not yet available.
        TenantNotFoundError     — invalid tenant UUID.
    """
    tenant_id: str = getattr(request.state, "tenant_id", "")

    if not tenant_id:
        raise TokenMissingError(
            "Tenant context is missing from the request. "
            "Ensure the request is authenticated."
        )

    logger.debug("tenant_validation_start", tenant_id=tenant_id)

    # Layer 1: Redis suspension check (fast, authoritative)
    await _check_suspension_flag(tenant_id)

    # Layer 2: DB-2 snapshot lookup (eventual-consistent config)
    snapshot = await _get_tenant_snapshot(tenant_id, db)

    logger.debug(
        "tenant_validation_success",
        tenant_id=tenant_id,
        plan_tier=snapshot.plan_tier,
    )

    return snapshot


# ── Annotated type alias for clean route signatures ───────────────────────────
# Usage: async def my_route(tenant: ValidatedTenant): ...
ValidatedTenant = Annotated[TenantSnapshot, Depends(get_validated_tenant)]