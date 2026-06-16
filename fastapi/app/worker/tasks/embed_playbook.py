import asyncio
import logging
import time

from celery import Task
from redis import Redis
from sqlalchemy import text

from app.core.config import get_settings
from app.database.pgvector import get_vector_write_session
from app.repositories.playbook_vector_repository import (
    delete_playbook_embedding,
    ensure_partial_index_for_enterprise,
    upsert_playbook_embedding,
)
from app.services.embedding_service import build_playbook_embed_text, embed_text
from app.worker.celery_app import celery_app

logger = logging.getLogger(__name__)

# Redis idempotency key: tracks last successfully embedded source_version.
_VERSION_KEY = "playbook_embed:{playbook_id}:version"


@celery_app.task(
    name="embed_playbook",
    bind=True,
    max_retries=5,
    autoretry_for=(Exception,),
    retry_backoff=True,  # Exponential backoff
    retry_backoff_max=300,  # Cap at 5 minutes — matches platform policy
    retry_jitter=True,
    reject_on_worker_lost=True,  # Re-queue if worker crashes mid-execution
    acks_late=True,
    queue="embed",  # Dedicated queue — never shares workers
    # with the agent queue
)
def embed_playbook(
    self: Task,
    playbook_id: str,
    tenant_id: str,
    plan_tier: str,
    error_pattern: str,
    instructions: str,
    source_version: int,
    deleted: bool = False,
) -> dict:
    """
    Embed a playbook and upsert the vector into playbook_embeddings (DB-2).
    If deleted=True, remove the embedding row.

    Idempotency:
      Before calling OpenAI, checks a Redis key that records the last
      successfully embedded source_version for this playbook. If the current
      source_version is already recorded, exits without calling OpenAI or
      writing to DB-2. Handles Kafka at-least-once redelivery and Celery
      retry scenarios. Matches the processed_events pattern.

    Enterprise tenant partial index:
      On first embed for an enterprise tenant, creates a partial HNSW index
      scoped to that tenant's vectors.
    """
    start = time.perf_counter()

    try:
        settings = get_settings()
        redis_client = Redis.from_url(settings.REDIS_URL, decode_responses=True)
        version_key = _VERSION_KEY.format(playbook_id=playbook_id)

        # ── Handle deletion ─────────────────────────────────────────────────
        if deleted:
            asyncio.run(delete_playbook_embedding(playbook_id))
            redis_client.delete(version_key)
            logger.info("Deleted embedding | playbook=%s", playbook_id)
            return {"status": "deleted", "playbook_id": playbook_id}

        # ── Idempotency check ───────────────────────────────────────────────
        cached_version = redis_client.get(version_key)
        if cached_version and int(cached_version) >= source_version:
            logger.debug(
                "Skipping — already at version %s | playbook=%s",
                source_version,
                playbook_id,
            )
            return {"status": "skipped", "playbook_id": playbook_id}

        # ── Build and compute embedding ─────────────────────────────────────
        embed_input = build_playbook_embed_text(error_pattern, instructions)
        vector = embed_text(embed_input)

        # ── Write to DB-2 ───────────────────────────────────────────────────
        asyncio.run(
            upsert_playbook_embedding(
                playbook_id=playbook_id,
                tenant_id=tenant_id,
                source_version=source_version,
                vector=vector,
            )
        )

        # ── Enterprise: ensure partial HNSW index ───────────────────────────
        if plan_tier == "enterprise":

            async def _ensure_idx():
                async with get_vector_write_session() as session:
                    await ensure_partial_index_for_enterprise(tenant_id, session)

            asyncio.run(_ensure_idx())

        # ── Record success ──────────────────────────────────────────────────
        redis_client.set(version_key, str(source_version))

        elapsed = time.perf_counter() - start

        logger.info(
            "Embedded playbook | playbook=%s tenant=%s version=%s %.3fs",
            playbook_id,
            tenant_id,
            source_version,
            elapsed,
        )
        return {"status": "embedded", "playbook_id": playbook_id}

    except Exception as exc:
        logger.exception(
            "embed_playbook failed | playbook=%s attempt=%d: %s",
            playbook_id,
            self.request.retries,
            exc,
        )
        raise
