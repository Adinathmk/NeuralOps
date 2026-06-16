import logging
from datetime import datetime, timezone

from sqlalchemy import text
from pgvector.sqlalchemy import Vector

from app.core.config import get_settings
from app.database.pgvector import get_vector_search_session, get_vector_write_session

logger = logging.getLogger(__name__)
settings = get_settings()

async def upsert_playbook_embedding(
    playbook_id: str,
    tenant_id: str,
    source_version: int,
    vector: list[float],
) -> None:
    async with get_vector_write_session() as session:
        await session.execute(
            text("""
                INSERT INTO playbook_embeddings
                    (playbook_id, tenant_id, embedding, source_version, embedded_at)
                VALUES
                    (:pid, :tid, :emb, :sv, :ts)
                ON CONFLICT (playbook_id) DO UPDATE SET
                    embedding      = EXCLUDED.embedding,
                    source_version = EXCLUDED.source_version,
                    embedded_at    = EXCLUDED.embedded_at
                WHERE
                    playbook_embeddings.source_version < EXCLUDED.source_version
            """),
            {
                "pid": playbook_id,
                "tid": tenant_id,
                "emb": vector_to_pg(vector),      
                "sv":  source_version,
                "ts":  datetime.now(timezone.utc),
            },
        )

    logger.info(
        "Upserted playbook embedding | playbook=%s tenant=%s version=%s",
        playbook_id, tenant_id, source_version,
    )


async def delete_playbook_embedding(playbook_id: str) -> None:
    async with get_vector_write_session() as session:
        await session.execute(
            text("DELETE FROM playbook_embeddings WHERE playbook_id = :pid"),
            {"pid": playbook_id},
        )

    logger.info("Deleted playbook embedding | playbook=%s", playbook_id)


async def search_similar_playbooks(
    query_vector: list[float],
    tenant_id: str,
    top_k: int = 5,
    distance_threshold: float = 0.28,   
) -> list[dict]:
    async with get_vector_search_session() as session:
        result = await session.execute(
            text("""
                SELECT
                    playbook_id,
                    (embedding <=> CAST(:qv AS vector)) AS distance
                FROM
                    playbook_embeddings
                WHERE
                    tenant_id = :tid
                    AND (embedding <=> CAST(:qv AS vector)) < :threshold
                ORDER BY
                    distance ASC
                LIMIT :k
            """),
            {
                "qv":        str(vector_to_pg(query_vector)),
                "tid":       tenant_id,
                "threshold": distance_threshold,
                "k":         top_k,
            },
        )
        rows = result.fetchall()

    return [
        {
            "playbook_id": str(row.playbook_id),
            "distance":    float(row.distance),
            "similarity":  round(1.0 - float(row.distance), 4),
        }
        for row in rows
    ]


async def ensure_partial_index_for_enterprise(
    tenant_id: str,
    write_session,
) -> None:
    safe_id = tenant_id.replace("-", "_")
    index_name = f"playbook_embeddings_hnsw_{safe_id}_idx"

    # NOTE: CONCURRENTLY is not allowed in a transaction block. 
    # Since write_session is inside an async context manager transaction block,
    # we just create it normally (without CONCURRENTLY).
    await write_session.execute(
        text(f"""
            CREATE INDEX IF NOT EXISTS {index_name}
            ON playbook_embeddings
            USING hnsw (embedding vector_cosine_ops)
            WITH (m = 16, ef_construction = 128)
            WHERE tenant_id = '{tenant_id}'
        """)
    )
    logger.info(
        "Ensured partial HNSW index for enterprise tenant | tenant=%s index=%s",
        tenant_id, index_name,
    )


# ── Utility ───────────────────────────────────────────────────────────────────

def vector_to_pg(v: list[float]) -> str:
    """Convert a Python list[float] to PostgreSQL vector literal string."""
    return "[" + ",".join(str(x) for x in v) + "]"
