import logging
from contextlib import asynccontextmanager
from sqlalchemy import text

from app.core.config import get_settings
from app.database.session import get_db_context

logger = logging.getLogger(__name__)

settings = get_settings()


@asynccontextmanager
async def get_vector_search_session():
    """
    Returns a read-replica DB session configured for HNSW vector search.
    """
    async with get_db_context() as session:
        await session.execute(
            text(f"SET LOCAL hnsw.ef_search = {settings.PLAYBOOK_HNSW_EF_SEARCH}")
        )
        await session.execute(text("SET LOCAL enable_seqscan = off"))
        yield session


@asynccontextmanager
async def get_vector_write_session():
    """
    Returns a primary write session for embedding upserts.
    """
    async with get_db_context() as session:
        yield session


async def verify_pgvector_extension(session) -> bool:
    """
    Check that the vector extension is installed and reachable.
    """
    try:
        result = await session.execute(
            text("SELECT extname FROM pg_extension WHERE extname = 'vector'")
        )
        if result.fetchone():
            logger.info("pgvector extension is available on DB-2")
            return True
        else:
            logger.warning(
                "pgvector extension NOT found on DB-2 — "
                "playbook semantic matching will yield no results"
            )
            return False
    except Exception as exc:
        logger.warning("Could not verify pgvector extension: %s", exc)
        return False
