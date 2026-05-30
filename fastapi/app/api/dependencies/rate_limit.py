"""
app/api/dependencies/rate_limit.py

Inner-layer tenant-aware rate limiting.
Enforces limits based on the tenant's plan tier using a fixed-window algorithm in Redis.
"""
from __future__ import annotations

import time
from typing import Annotated

from fastapi import Depends, HTTPException, Request

from app.api.dependencies.tenant import ValidatedTenant
from app.core.logging import get_logger
from app.database.redis import get_redis

logger = get_logger(__name__)

# Limits per minute (matches Django QuotaService)
_PLAN_LIMITS = {
    "free": 100,
    "pro": 500,
    "enterprise": 2000,
}

_DEFAULT_LIMIT = 100


async def rate_limit_dependency(request: Request, tenant: ValidatedTenant) -> None:
    """
    Fixed-window rate limiter utilizing Redis.
    Limits are enforced per-minute per-tenant based on their billing tier.
    """
    plan_tier = tenant.plan_tier or "free"
    limit = _PLAN_LIMITS.get(plan_tier.lower(), _DEFAULT_LIMIT)

    redis = get_redis()
    current_minute = int(time.time() / 60)
    key = f"rate_limit:{tenant.tenant_id}:{current_minute}"

    try:
        # INCR is atomic. If the key doesn't exist, it is set to 1.
        count = await redis.incr(key)
        if count == 1:
            # Set TTL to 60 seconds since the window is 1 minute
            await redis.expire(key, 60)
    except Exception as exc:
        logger.warning(
            "rate_limit_redis_failed",
            tenant_id=str(tenant.tenant_id),
            error=str(exc),
            detail="Redis unavailable; failing open to avoid dropping traffic.",
        )
        return

    if count > limit:
        logger.warning(
            "rate_limit_exceeded",
            tenant_id=str(tenant.tenant_id),
            plan_tier=plan_tier,
            limit=limit,
            count=count,
        )
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded. Your {plan_tier} plan allows {limit} requests per minute.",
        )


RateLimitDependency = Annotated[None, Depends(rate_limit_dependency)]
