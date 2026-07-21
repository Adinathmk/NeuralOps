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

import sentry_sdk
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.celery import CeleryIntegration

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from prometheus_fastapi_instrumentator import Instrumentator

# ── Routers ───────────────────────────────────────────────────────────────────
from app.api.v1.health import router as health_router
from app.api.v1.incidents import router as incidents_router  # ← Phase 4
from app.api.v1.ingest import router as ingest_router  # ← Phase 2
from app.api.v1.log_search_endpoint import router as logs_router
from app.api.v1.webhooks import router as webhooks_router
from app.api.v1.dashboard import router as dashboard_router
from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.database.elasticsearch_client import close_es_client, get_es_client
from app.database.session import engine
from app.middleware.auth import JWTAuthMiddleware
from app.middleware.error_handler import register_exception_handlers
from app.middleware.tenant_rls import TenantRLSMiddleware
from app.middleware.gzip_request import GZipRequestMiddleware

# ── Model imports (register with SQLAlchemy metadata before Alembic / queries) ─
from app.models import code_index  # noqa: F401
from app.models import incidents  # noqa: F401
from app.models import logs  # noqa: F401
from app.models import outbox  # noqa: F401
from app.models import snapshots  # noqa: F401
from app.models import github_integration_snapshots  # noqa: F401  ← must be after snapshots

# ── Background consumers ──────────────────────────────────────────────────────
from app.queue.kafka.consumers.config_sync import ConfigSyncConsumer
from app.queue.kafka.consumers.raw_logs import RawLogConsumer

settings = get_settings()

# Module-level consumer instance — created once, started in lifespan.
_config_sync_consumer = ConfigSyncConsumer()
_raw_log_consumer = RawLogConsumer()


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

    get_es_client()

    _consumer_task = asyncio.create_task(
        _config_sync_consumer.start(),
        name="config_sync_consumer",
    )

    _raw_log_task = asyncio.create_task(
        _raw_log_consumer.start(),
        name="raw_log_consumer",
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
    await _raw_log_consumer.stop()
    logger.info("config_sync_consumer_stopped")

    if not _consumer_task.done():
        _consumer_task.cancel()
        try:
            await _consumer_task
        except asyncio.CancelledError:
            pass  # Expected during shutdown

    if not _raw_log_task.done():
        _raw_log_task.cancel()
        try:
            await _raw_log_task
        except asyncio.CancelledError:
            pass

    await close_es_client()
    logger.info("elasticsearch_client_closed")

    await engine.dispose()
    logger.info("database_engine_disposed")


# ── FastAPI application ────────────────────────────────────────────────────────

if settings.SENTRY_DSN:
    sentry_sdk.init(
        dsn=settings.SENTRY_DSN,
        integrations=[
            FastApiIntegration(),
            CeleryIntegration(),
        ],
        environment=settings.APP_ENV,
        traces_sample_rate=0.1,
        send_default_pii=False,
    )

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



Instrumentator().instrument(app).expose(app, endpoint="/metrics", include_in_schema=False)

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

# Decompress incoming SDK payloads (which are gzip compressed if >1024 bytes)
app.add_middleware(GZipRequestMiddleware)

# NOTE: Starlette middleware added LAST runs FIRST (outermost wraps innermost).
# JWTAuthMiddleware must dispatch before TenantRLSMiddleware so that
# request.state.tenant_id exists when TenantRLSMiddleware reads it.
# Registration order below is therefore: TenantRLS first (added), JWTAuth last (added).

# 2. Tenant RLS — reads tenant_id from request.state and stores it so
#    get_db() can emit SET LOCAL app.tenant_id = '...' before the first
#    ORM query. Registered before JWTAuth so it ends up as the INNER layer
#    and therefore runs AFTER JWTAuth sets tenant_id.
app.add_middleware(TenantRLSMiddleware)

# 3. JWT authentication — reads Authorization header or gateway-injected
#    headers; attaches tenant_id, user_id, user_role to request.state.
#    Registered last so it is the OUTER layer and dispatches first.
app.add_middleware(JWTAuthMiddleware)

# ── Routers ───────────────────────────────────────────────────────────────────
app.include_router(health_router)
app.include_router(incidents_router, prefix="/api/v1")
app.include_router(ingest_router, prefix="/api/v1")
app.include_router(logs_router)
app.include_router(webhooks_router, prefix="/api/v1")
app.include_router(dashboard_router)




# CI pipeline test commit - safe to ignore
# CI pipeline test commit - safe to ignore
# CI pipeline test commit - safe to ignore
