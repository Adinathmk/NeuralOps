"""
fastapi/app/agents/nodes/playbook_matcher.py

Playbook Matcher Node — Phase 4 LangGraph Agent Pipeline (pgvector RAG)

Fetches the most semantically relevant playbook for the current incident
using an HNSW nearest-neighbour search against pgvector.

Matching strategy
-----------------
1. Extracts error_type, stack_trace_summary, service_name, and file_path
   from the parsed_event.
2. Embeds the query using OpenAI text-embedding-3-small (with Redis caching).
3. Searches playbook_embeddings (ANN cosine distance) for the closest match
   below the defined distance threshold.
4. If a match is found, queries playbook_snapshots for the remediation
   instructions.

The matched playbook's instructions are injected into the Analyzer node's
prompt, giving the LLM domain-specific remediation guidance before it
writes its root cause analysis.

Result: matched_playbook_id, playbook_instructions, playbook_latency_ms

No match: both fields are None; the Analyzer proceeds without instructions.

Inputs consumed from AgentState
--------------------------------
  tenant_id
  parsed_event
  session   (AsyncSession bound to DB-2)

Outputs written to AgentState
------------------------------
  matched_playbook_id   : str | None
  playbook_instructions : str | None
  playbook_latency_ms   : int
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

from sqlalchemy.future import select

from app.core.config import get_settings
from app.models.snapshots import PlaybookSnapshot
from app.services.embedding_service import embed_text, build_query_embed_text, query_text_hash
from app.repositories.playbook_vector_repository import search_similar_playbooks
from app.database.redis import get_redis

logger = logging.getLogger(__name__)
settings = get_settings()


class PlaybookMatcherNode:
    """
    LangGraph node: PlaybookMatcher (pgvector RAG)

    Stateless — safe to instantiate once at module level.
    """

    async def invoke(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """
        Match the incident against tenant playbook vectors.

        Parameters
        ----------
        state : dict
            Full AgentState.

        Returns
        -------
        dict
            Partial AgentState update:
            {matched_playbook_id, playbook_instructions, playbook_latency_ms}
        """
        start: float = time.monotonic()
        parsed: Dict[str, Any] = state.get("parsed_event", {})
        session = state["session"]
        tenant_id: str = str(state.get("tenant_id", ""))

        error_type: str = str(parsed.get("error_type") or "")
        stack_trace_summary: str = str(parsed.get("stack_trace_summary") or "")
        service_name: str = str(parsed.get("service_name") or "")
        file_path: str = str(parsed.get("file_path") or "")

        matched_playbook_id: Optional[str] = None
        playbook_instructions: Optional[str] = None

        try:
            # 1. Build the query text
            query_text = build_query_embed_text(
                error_type=error_type,
                stack_trace_summary=stack_trace_summary,
                service_name=service_name,
                file_path=file_path,
            )

            # 2. Get embedding (with Redis caching)
            query_vector = await self._get_query_embedding(query_text)

            # 3. ANN Search in pgvector
            matches = search_similar_playbooks(
                query_vector=query_vector,
                tenant_id=tenant_id,
                top_k=1,
                distance_threshold=settings.PLAYBOOK_MATCH_SCORE_THRESHOLD,
            )

            if matches:
                best_match = matches[0]
                matched_playbook_id = best_match["playbook_id"]
                logger.info(
                    "playbook_matched_semantic",
                    extra={
                        "tenant_id": tenant_id,
                        "playbook_id": matched_playbook_id,
                        "distance": best_match["distance"],
                        "similarity": best_match["similarity"],
                    },
                )

                # 4. Fetch the actual instructions from DB-2 using AsyncSession
                import uuid as _uuid_module
                pb_uuid = _uuid_module.UUID(matched_playbook_id)
                stmt = select(PlaybookSnapshot).where(
                    PlaybookSnapshot.playbook_id == pb_uuid,
                    PlaybookSnapshot.tenant_id == _uuid_module.UUID(tenant_id),
                    PlaybookSnapshot.is_active.is_(True)
                )
                result = await session.execute(stmt)
                playbook = result.scalar_one_or_none()
                
                if playbook:
                    playbook_instructions = str(
                        getattr(playbook, "instructions", "") or ""
                    )
                else:
                    # Very rare race condition: embedded vector exists but snapshot deleted/inactive
                    matched_playbook_id = None
            else:
                logger.debug(
                    "playbook_no_match_semantic",
                    extra={
                        "tenant_id": tenant_id,
                        "error_type": error_type,
                    },
                )

        except Exception as exc:
            # Playbook matching is best-effort; failure must not block analysis
            logger.warning(
                "playbook_matcher_error",
                extra={"tenant_id": tenant_id, "error": str(exc)},
            )

        latency_ms = int((time.monotonic() - start) * 1000)

        return {
            "matched_playbook_id": matched_playbook_id,
            "playbook_instructions": playbook_instructions,
            "playbook_latency_ms": latency_ms,
        }

    async def _get_query_embedding(self, query_text: str) -> list[float]:
        """
        Retrieves the embedding for a given query text.
        Checks Redis L1 cache first to save OpenAI costs and latency on
        identical incoming errors (e.g., error spikes).
        """
        rdb = get_redis()
        cache_key = f"embed:query:{query_text_hash(query_text)}"

        cached = await rdb.get(cache_key)
        if cached:
            # Parse the string representation back into a float list
            import json
            try:
                return json.loads(cached)
            except Exception:
                pass

        # Cache miss, call OpenAI (which is sync, so we just call it directly
        # or it will block the event loop a tiny bit. Since embed_text uses httpx sync under the hood,
        # we ideally should run it in a thread pool, but for simplicity here we just call it.
        # LangGraph nodes are usually wrapped in threads if they're async calling sync block).
        # Actually, let's run it in a thread executor to avoid blocking the event loop.
        import asyncio
        loop = asyncio.get_running_loop()
        vector = await loop.run_in_executor(None, embed_text, query_text)

        # Cache the result for 24 hours
        import json
        await rdb.setex(cache_key, 86400, json.dumps(vector))
        return vector