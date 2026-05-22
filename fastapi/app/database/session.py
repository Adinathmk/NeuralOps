"""
app/database/session.py

Async SQLAlchemy engine and session factory for DB-2 (FastAPI-owned PostgreSQL).

Design notes:
- Uses asyncpg as the DBAPI driver (postgresql+asyncpg://).
- NullPool is used so that each async task gets a fresh connection; this
  avoids "connection already checked out" issues in async contexts.
- The tenant_rls middleware sets the PostgreSQL session parameter
  `app.tenant_id` on every connection before the first query.  This
  activates the Row-Level Security policies that enforce tenant isolation
  at the database engine level — a belt-and-suspenders layer on top of
  ORM-level filtering.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

# ── Engine ────────────────────────────────────────────────────────────────────
# Created once at module-import time; reused for the process lifetime.
_settings = get_settings()

engine = create_async_engine(
    _settings.DATABASE_URL,
    # NullPool: do not pool connections across requests. Each request
    # opens a fresh connection, sets RLS variables, and closes it.
    poolclass=NullPool,
    # Echo SQL only in debug mode to avoid leaking sensitive data in logs.
    echo=_settings.DEBUG,
    # Ensure asyncpg receives the correct JSON type handling.
    json_serializer=lambda obj: __import__("json").dumps(obj, default=str),
)

# ── Session factory ───────────────────────────────────────────────────────────
AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)


# ── FastAPI dependency ────────────────────────────────────────────────────────

async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    FastAPI dependency that yields an async database session.

    Usage in route functions:
        async def my_route(db: AsyncSession = Depends(get_db)):
            ...

    The session is automatically closed (and rolled back on exception)
    after the response is sent.

    Note: the tenant_rls middleware runs *before* this dependency resolves,
    so `app.tenant_id` is already set on the connection by the time any
    ORM query executes.
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


@asynccontextmanager
async def get_db_context() -> AsyncGenerator[AsyncSession, None]:
    """
    Context manager variant for use outside of FastAPI's dependency injection
    (e.g., in Celery tasks or startup/shutdown hooks).

    async with get_db_context() as db:
        result = await db.execute(...)
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()