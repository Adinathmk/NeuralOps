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

Later phases will add:
  - Log ingestion endpoints (Phase 2+)
  - AI agent pipeline (Phase 4+)
  - WebSocket real-time updates (Phase 5+)
"""

from __future__ import annotations

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

settings = get_settings()


# ── Lifespan (startup / shutdown) ─────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application lifespan context manager.

    Startup:
      1. Configure structured logging.
      2. Create all DB-2 tables that do not yet exist.
         (In production, Alembic migrations handle this; create_all is
          kept here as a convenience for local development.)

    Shutdown:
      1. Dispose the async engine connection pool.
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


    logger.info("database_tables_ready", database_url=settings.DATABASE_URL.split("@")[-1])

    yield

    # ── Shutdown ──────────────────────────────────────────────────────────────
    logger.info("service_shutting_down", service=settings.APP_NAME)
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