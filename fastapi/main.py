"""
main.py

FastAPI service entry point — NeuralOps Service 2 (AI + Real-time).

Phase 1 scope:
  - Database & ORM initialisation (DB-2 / asyncpg)
  - JWT validation middleware (RS256 public key)
  - Tenant RLS middleware (sets app.tenant_id on every DB connection)
  - Tenant validation dependency (reads tenant_snapshots + Redis suspension flag)
  - OutboxEvent and TenantSnapshot models registered with SQLAlchemy
  - GET /health endpoint
  - Structured JSON error handling (global exception handlers)
  - Structured logging (structlog / JSON in production)

Phase 2 scope (current):
  - ConfigSyncConsumer background task (startup/shutdown via lifespan)
  - POST /api/v1/ingest/logs endpoint:
      - Redis L1 read-through cache on tenant dependency
      - gzip compression + aioboto3 S3 upload
      - Atomic DB-2 transaction: IngestedLogMetadata + OutboxEvent
  - IngestedLogMetadata model registered with SQLAlchemy / Alembic

Later phases will add:
  - Log ingestion full pipeline (parse_log Celery task)
  - AI agent pipeline (Phase 4+)
  - WebSocket real-time updates (Phase 5+)
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.database.session import engine

# ── Model imports (register with SQLAlchemy metadata before Alembic / queries) ─
import app.models.outbox       # noqa: F401
import app.models.snapshots    # noqa: F401
import app.models.logs         # noqa: F401  ← Phase 2: IngestedLogMetadata

from app.middleware.auth import JWTAuthMiddleware
from app.middleware.error_handler import register_exception_handlers
from app.middleware.tenant_rls import TenantRLSMiddleware

# ── Routers ───────────────────────────────────────────────────────────────────
from app.api.v1.health import router as health_router
from app.api.v1.ingest import router as ingest_router   # ← Phase 2

# ── Background consumers ──────────────────────────────────────────────────────
from app.queue.kafka.consumers.config_sync import ConfigSyncConsumer

settings = get_settings()

# Module-level consumer instance — created once, started in lifespan.
_config_sync_consumer = ConfigSyncConsumer()


# ── Lifespan (startup / shutdown) ─────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application lifespan context manager.

    Startup
    -------
    1. Configure structured logging.
    2. Log service start metadata.
    3. Start ConfigSyncConsumer as a background asyncio task.
       Subscribes to config.tenants, config.alert_rules, config.playbooks
       and keeps DB-2 snapshot tables in sync with Django.

    Shutdown
    --------
    1. Signal ConfigSyncConsumer to stop and drain in-flight messages.
    2. Cancel the background task if still running.
    3. Dispose the async engine connection pool.
    """
    # ── Startup ───────────────────────────────────────────────────────────────
    configure_logging()
    logger = get_logger("startup")

    logger.info(
        "service_starting",
        service=settings.APP_NAME,
        version=settings.APP_VERSION,
        environment=settings.APP_ENV,
    )

    logger.info(
        "database_tables_ready",
        database_url=settings.DATABASE_URL.split("@")[-1],
    )

    _consumer_task = asyncio.create_task(
        _config_sync_consumer.start(),
        name="config_sync_consumer",
    )

    logger.info(
        "config_sync_consumer_task_created",
        kafka_bootstrap_servers=settings.KAFKA_BOOTSTRAP_SERVERS,
        kafka_group_id=settings.KAFKA_CONFIG_GROUP_ID,
    )

    yield

    # ── Shutdown ──────────────────────────────────────────────────────────────
    logger.info("service_shutting_down", service=settings.APP_NAME)

    await _config_sync_consumer.stop()
    logger.info("config_sync_consumer_stopped")

    if not _consumer_task.done():
        _consumer_task.cancel()
        try:
            await _consumer_task
        except asyncio.CancelledError:
            pass  # Expected during shutdown

    await engine.dispose()
    logger.info("database_engine_disposed")


# ── FastAPI application ────────────────────────────────────────────────────────

app = FastAPI(
    title="NeuralOps — AI + Real-time Service",
    description=(
        "Service 2 of the NeuralOps platform. "
        "Handles log ingestion, AI-powered incident analysis, "
        "and real-time WebSocket delivery."
    ),
    version=settings.APP_VERSION,
    docs_url="/docs" if settings.DEBUG else None,
    redoc_url="/redoc" if settings.DEBUG else None,
    openapi_url="/openapi.json" if settings.DEBUG else None,
    lifespan=lifespan,
)

# ── Exception handlers (register BEFORE middleware) ───────────────────────────
register_exception_handlers(app)

# ── Middleware (outermost → innermost) ────────────────────────────────────────

# 1. CORS — must be outermost so OPTIONS preflight is handled before auth.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 2. JWT authentication — reads Authorization header or gateway-injected
#    headers; attaches tenant_id, user_id, user_role to request.state.
app.add_middleware(JWTAuthMiddleware)

# 3. Tenant RLS — reads tenant_id from request.state and stores it so
#    get_db() can emit SET LOCAL app.tenant_id = '...' before the first
#    ORM query.
app.add_middleware(TenantRLSMiddleware)

# ── Routers ───────────────────────────────────────────────────────────────────
app.include_router(health_router)
app.include_router(ingest_router, prefix="/api/v1")   # ← Phase 2 wired in