"""
main.py

FastAPI service entry point — NeuralOps Service 2 (AI + Real-time).

Phase 1 scope:
  - Database & ORM initialisation (DB-2 / asyncpg)
  - JWT validation middleware (RS256 public key)
  - Tenant RLS middleware (sets app.tenant_id on every DB connection)
  - Tenant validation dependency (reads tenant_snapshots + Redis suspension flag)
  - OutboxEvent and TenantSnapshot models registered with SQLAlchemy
  - GET /health endpoint (only endpoint in Phase 1)
  - Structured JSON error handling (global exception handlers)
  - Structured logging (structlog / JSON in production)

Phase 2 scope (added here):
  - ConfigSyncConsumer background task started on lifespan startup
  - ConfigSyncConsumer cleanly stopped on lifespan shutdown
  - Kafka bootstrap servers and consumer group ID added to Settings

Later phases will add:
  - Log ingestion endpoints (Phase 2+)
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

# Import models so they are registered with SQLAlchemy metadata
# before Alembic migrations or application queries run.
import app.models.outbox  # noqa: F401
import app.models.snapshots  # noqa: F401

from app.middleware.auth import JWTAuthMiddleware
from app.middleware.error_handler import register_exception_handlers
from app.middleware.tenant_rls import TenantRLSMiddleware
from app.api.v1.health import router as health_router

# Phase 2: Config sync consumer
from app.queue.kafka.consumers.config_sync import ConfigSyncConsumer

settings = get_settings()

# ── Module-level consumer instance ────────────────────────────────────────────
# A single instance is created at module load time so the same object is
# referenced by both the lifespan startup (create_task) and shutdown
# (await stop) blocks.  It is NOT started until the lifespan begins.
_config_sync_consumer = ConfigSyncConsumer()


# ── Lifespan (startup / shutdown) ─────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application lifespan context manager.

    Startup:
      1. Configure structured logging.
      2. Log service start metadata.
      3. Start the ConfigSyncConsumer as a background asyncio task.
         The consumer subscribes to config.tenants, config.alert_rules,
         and config.playbooks and keeps DB-2 snapshot tables in sync.

    Shutdown:
      1. Signal the ConfigSyncConsumer to stop and drain in-flight messages.
      2. Dispose the async engine connection pool.
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

    # Start the Kafka config-sync consumer as a background task.
    # asyncio.create_task schedules _config_sync_consumer.start() on the
    # running event loop without blocking the lifespan coroutine.
    # The consumer's internal retry loop means a transient Kafka outage at
    # startup does NOT prevent FastAPI from becoming ready to serve traffic.
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

    # Stop the Kafka consumer gracefully: drain in-flight messages, commit
    # last offsets, and close the broker connection.
    await _config_sync_consumer.stop()
    logger.info("config_sync_consumer_stopped")

    # Cancel the background task if it is still running (e.g. the consumer's
    # internal retry loop is sleeping between reconnect attempts).
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

# ── Middleware (outermost → innermost; first registered = outermost) ──────────

# 1. CORS — must be outermost so preflight OPTIONS requests are handled
#    before auth middleware rejects them.
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

# 3. Tenant RLS — reads tenant_id from request.state and stores it so the
#    get_db() dependency can emit SET LOCAL app.tenant_id = '...' on the
#    PostgreSQL connection before the first ORM query.
app.add_middleware(TenantRLSMiddleware)

# ── Routers ───────────────────────────────────────────────────────────────────
# Phase 1: health only.
# Future phases will add:
#   app.include_router(ingest_router, prefix="/api/v1")
#   app.include_router(incidents_router, prefix="/api/v1")
#   ...

app.include_router(health_router)