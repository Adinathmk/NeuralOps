"""
alembic/env.py

Alembic migration environment for DB-2 (FastAPI-owned PostgreSQL).

Uses the async SQLAlchemy engine so that asyncpg is used consistently
across both migrations and the application. Migrations run as a
Kubernetes Job before the new FastAPI deployment rolls out.

Architecture note: Django manages DB-1 via its own ORM migrations.
Alembic manages DB-2 here. Neither touches the other's database.
"""

import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

import app.models.code_index  # noqa: F401  ← Phase 3: CodeIndex / code_index table
import app.models.logs  # noqa: F401
import app.models.outbox  # noqa: F401
import app.models.snapshots  # noqa: F401
import app.models.incidents  # noqa: F401
from alembic import context
from app.core.config import get_settings

# ── Import our models so Alembic auto-detects schema changes ─────────────────
# These imports side-effect populate Base.metadata.
# IMPORTANT: every new model module MUST be imported here — Alembic can only
# autogenerate migrations for tables whose metadata has been registered by
# the time env.py runs.
from app.database.base import Base

# Alembic Config object (gives access to alembic.ini values)
config = context.config

# Set the SQLAlchemy URL from pydantic-settings (not from alembic.ini)
settings = get_settings()
config.set_main_option("sqlalchemy.url", settings.DATABASE_URL)

# Interpret the config file for Python logging
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


# ── Offline migrations ────────────────────────────────────────────────────────


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (generates SQL without DB connection)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


# ── Online migrations ─────────────────────────────────────────────────────────


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Run migrations in 'online' mode using the async engine."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


# ── Entry point ───────────────────────────────────────────────────────────────

if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
