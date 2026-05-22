"""
app/api/v1/health.py

Health check endpoint — the ONLY endpoint implemented in Phase 1.

GET /health

Returns a structured response indicating:
  - Service liveness (always 200 if the process is running)
  - Database connectivity (DB-2 async ping)
  - Redis connectivity (async ping)

Response shape (JSend-style success):
{
    "status": "ok",
    "data": {
        "service": "neuralops-fastapi",
        "version": "1.0.0",
        "environment": "development",
        "checks": {
            "database": "ok" | "degraded",
            "redis":    "ok" | "degraded"
        }
    }
}

HTTP status codes:
  200 — all checks pass (status: "ok")
  207 — one or more checks degraded but service is still running
        (status: "degraded")

Note: the liveness probe in Kubernetes should target /health.
The readiness probe may target the same endpoint and treat a 207
as "not ready" depending on operational policy.
"""

from __future__ import annotations

from typing import Dict, Literal

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.core.config import get_settings
from app.core.logging import get_logger
from app.database.session import engine

logger = get_logger(__name__)
router = APIRouter(tags=["health"])

CheckResult = Literal["ok", "degraded"]


async def _check_database() -> CheckResult:
    """Attempt a minimal async ping against DB-2."""
    from sqlalchemy import text

    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return "ok"
    except Exception as exc:
        logger.error("health_db_check_failed", error=str(exc))
        return "degraded"


async def _check_redis() -> CheckResult:
    """Attempt a PING against Redis."""
    import redis.asyncio as aioredis

    settings = get_settings()
    try:
        client = aioredis.from_url(
            settings.REDIS_URL,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
        await client.ping()
        await client.aclose()
        return "ok"
    except Exception as exc:
        logger.error("health_redis_check_failed", error=str(exc))
        return "degraded"


@router.get(
    "/health",
    summary="Service health check",
    description=(
        "Returns liveness status and connectivity checks for DB-2 and Redis. "
        "Used by Kubernetes liveness / readiness probes."
    ),
    response_class=JSONResponse,
    # Exclude from OpenAPI auth requirements
    include_in_schema=True,
)
async def health_check() -> JSONResponse:
    """
    Perform health checks and return a structured status response.

    Returns 200 when all dependencies are reachable, 207 when one or
    more checks are degraded (service is alive but partially impaired).
    """
    settings = get_settings()

    db_status: CheckResult = await _check_database()
    redis_status: CheckResult = await _check_redis()

    checks: Dict[str, CheckResult] = {
        "database": db_status,
        "redis": redis_status,
    }

    overall_ok = all(v == "ok" for v in checks.values())
    overall_status = "ok" if overall_ok else "degraded"
    http_status = 200 if overall_ok else 207

    body = {
        "status": overall_status,
        "data": {
            "service": settings.APP_NAME,
            "version": settings.APP_VERSION,
            "environment": settings.APP_ENV,
            "checks": checks,
        },
    }

    logger.info(
        "health_check",
        overall=overall_status,
        db=db_status,
        redis=redis_status,
    )

    return JSONResponse(content=body, status_code=http_status)