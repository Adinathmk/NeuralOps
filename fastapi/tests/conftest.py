import asyncio
from unittest.mock import AsyncMock, patch

# Disable background Kafka config sync consumer during test execution.
# We save the patcher objects so lifecycle tests can temporarily stop them
# and invoke the real start()/stop() implementations.
_consumer_start_patcher = patch(
    "app.queue.kafka.consumers.config_sync.ConfigSyncConsumer.start",
    new_callable=AsyncMock,
)
_consumer_stop_patcher = patch(
    "app.queue.kafka.consumers.config_sync.ConfigSyncConsumer.stop",
    new_callable=AsyncMock,
)
_consumer_start_patcher.start()
_consumer_stop_patcher.start()

import pytest
from asgi_lifespan import LifespanManager
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.database.session import engine, get_db
from main import app

settings = get_settings()


@pytest.fixture(scope="session")
def event_loop():
    """Create a session-wide event loop for the async test runner."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope="function")
async def db_conn():
    """
    Provide a connection kept in a connection-level transaction.
    All operations executed on this connection across any session are rolled back at the end,
    ensuring absolute database isolation during tests.
    """
    async with engine.connect() as conn:
        transaction = await conn.begin()
        yield conn
        await transaction.rollback()


@pytest.fixture
async def db_session(db_conn):
    """
    Provide a database session for test seeding (e.g. creating tenants/fixtures).
    Bound to the shared transactional connection.
    """
    SessionLocal = async_sessionmaker(
        bind=db_conn,
        class_=AsyncSession,
        expire_on_commit=False,
        autocommit=False,
        autoflush=False,
    )
    async with SessionLocal() as session:
        yield session


@pytest.fixture
async def client(db_conn):
    """
    Provide an asynchronous HTTP client configured with database overrides.
    Yields a fresh session instance bound to the shared transactional connection on every request,
    preventing session-level transaction conflicts ('transaction already begun').
    """

    async def override_get_db():
        SessionLocal = async_sessionmaker(
            bind=db_conn,
            class_=AsyncSession,
            expire_on_commit=False,
            autocommit=False,
            autoflush=False,
        )
        async with SessionLocal() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db

    import httpx

    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac

    app.dependency_overrides.clear()
