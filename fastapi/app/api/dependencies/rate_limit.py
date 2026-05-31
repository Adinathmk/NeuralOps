"""
app/api/dependencies/rate_limit.py

Inner-layer tenant-aware rate limiting using Token Bucket algorithm in Redis.
Enforces limits based on the tenant's plan tier.
"""

from __future__ import annotations

import time
from typing import Annotated

from fastapi import Depends, HTTPException, Request

from app.api.dependencies.tenant import ValidatedTenant
from app.core.logging import get_logger
from app.database.redis import get_redis

logger = get_logger(__name__)

# Token Bucket Lua Script
# KEYS[1] = rl:ingest:{tenant_id}
# ARGV[1] = capacity (max tokens)
# ARGV[2] = refill_rate (tokens per second)
# ARGV[3] = current_time (seconds since epoch)
# Returns: 1 if allowed, 0 if blocked
TOKEN_BUCKET_SCRIPT = """
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local refill_rate = tonumber(ARGV[2])
local current_time = tonumber(ARGV[3])

local bucket = redis.call('HMGET', key, 'tokens', 'last_refill')
local tokens = tonumber(bucket[1])
local last_refill = tonumber(bucket[2])

if not tokens or not last_refill then
    tokens = capacity
    last_refill = current_time
end

local time_passed = current_time - last_refill
local new_tokens = time_passed * refill_rate
tokens = math.min(capacity, tokens + new_tokens)

if tokens >= 1 then
    tokens = tokens - 1
    redis.call('HMSET', key, 'tokens', tokens, 'last_refill', current_time)
    redis.call('EXPIRE', key, 120)
    return 1
else
    return 0
end
"""

_PLAN_LIMITS = {
    "free": 200,
    "pro": 1000,
    "enterprise": 5000,
}

_DEFAULT_LIMIT = 200


async def rate_limit_dependency(request: Request, tenant: ValidatedTenant) -> None:
    """
    Token bucket rate limiter utilizing Redis Lua script.
    Limits are enforced per-tenant based on their billing tier.
    """
    plan_tier = tenant.plan_tier or "free"
    capacity = _PLAN_LIMITS.get(plan_tier.lower(), _DEFAULT_LIMIT)
    refill_rate = capacity / 60.0  # Full refill in 60s

    redis = get_redis()
    current_time = time.time()
    key = f"rl:ingest:{tenant.tenant_id}"

    try:
        allowed = await redis.eval(
            TOKEN_BUCKET_SCRIPT,
            1,
            key,
            capacity,
            refill_rate,
            current_time,
        )
    except Exception as exc:
        logger.warning(
            "rate_limit_redis_failed",
            tenant_id=str(tenant.tenant_id),
            error=str(exc),
            detail="Redis unavailable; failing open to avoid dropping traffic.",
        )
        return

    if not allowed:
        logger.warning(
            "rate_limit_exceeded",
            tenant_id=str(tenant.tenant_id),
            plan_tier=plan_tier,
            capacity=capacity,
        )
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded. Your {plan_tier} plan allows {capacity} requests per minute burst.",
            headers={"Retry-After": "1"},
        )


RateLimitDependency = Annotated[None, Depends(rate_limit_dependency)]
