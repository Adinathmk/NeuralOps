"""
fastapi/app/agents/nodes/code_retriever.py

Code Retriever Node — Phase 4 LangGraph Agent Pipeline

Deterministic, zero-vector-arithmetic code retrieval. All source code
context assembled for the GPT-4 prompt is fetched via indexed line ranges
from DB-2's code_index table, then sliced from the actual source files
stored in S3.

Retrieval strategy (three-step cascade)
----------------------------------------
Step 1 — Crashed function (highest priority):
  Find the CodeIndex row whose [start_line, end_line] range contains
  crash_line within crash_file. The narrowest range is preferred (method
  over class), so we ORDER BY (end_line - start_line) ASC LIMIT 1.

Step 2 — Stack trace frame functions:
  For each frame in stack_frames[], run the same location query.
  Skip frames whose symbols were already fetched in Step 1.

Step 3 — Direct helper functions called by the crashed function:
  For each name in crashed_symbol.calls[], run a name-based query.
  Skip symbols already fetched. Skip external library symbols.

Token budget: 1,500 tokens (tiktoken gpt-4o encoding).
Symbols are added in Step 1 → 2 → 3 priority order. As soon as adding
the next snippet would exceed the budget, that snippet is skipped and
lower-priority symbols are tried in case a smaller one fits.

File content cache hierarchy (all scoped to one task execution):
  1. Request-scoped Python dict (zero network, fastest)
  2. Redis L1 cache: key = code:{s3_key}, TTL = 24 hours
  3. AWS S3: code/{tenant_id}/{repo}/{commit_sha}/{file_path}

Each S3 file is fetched at most ONCE per agent execution, regardless of
how many functions are needed from it, because the request-scoped dict
deduplicates fetches by s3_key before touching Redis or S3.

Inputs consumed from AgentState
--------------------------------
  tenant_id
  parsed_event.crash_file
  parsed_event.crash_line
  parsed_event.stack_frames
  session    (AsyncSession bound to DB-2)
  redis      (aioredis.Redis)

Outputs written to AgentState
------------------------------
  code_context        : str — assembled code snippets joined by separator
  code_retriever_meta : dict — files_fetched, tokens, cache_hits,
                               cache_misses, symbols_retrieved, latency_ms
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Set, Tuple
from langsmith import traceable

from app.agents.trace_utils import strip_node_state

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Maximum total tokens allowed across all assembled code snippets.
# This cap prevents the GPT-4 context window from being exhausted by
# large codebases and keeps per-analysis cost predictable.
_TOKEN_BUDGET: int = 1500

# Redis TTL for cached source file content: 24 hours in seconds.
# Source files change infrequently compared to log events.
_REDIS_FILE_CACHE_TTL: int = 86_400

# Separator inserted between code snippets in the assembled context string.
_SNIPPET_SEPARATOR: str = "\n\n---\n\n"


def _get_encoding():
    """Lazy-load tiktoken encoding to avoid import cost at module level."""
    import tiktoken

    return tiktoken.encoding_for_model("gpt-4o")


class CodeRetrieverNode:
    """
    LangGraph node: CodeRetriever

    Stateless — safe to instantiate once at module level.
    All I/O (DB queries, Redis, S3) is performed asynchronously.

    The invoke() method is the LangGraph entry point. Because LangGraph
    calls node functions synchronously (the graph itself is not async),
    we use asyncio.run_until_complete() to bridge into async code.
    In Python 3.10+ with an already-running event loop (as in async
    Celery tasks), we use asyncio.get_event_loop().
    """

    @traceable(run_type="chain", name="code_retriever_node", process_inputs=strip_node_state)
    async def invoke(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """
        Retrieve source code context for the crashed function.

        Parameters
        ----------
        state : dict
            Full AgentState. Reads: tenant_id, parsed_event, session, redis.

        Returns
        -------
        dict
            Partial AgentState update: {code_context, code_retriever_meta}
        """
        start: float = time.monotonic()
        result = await self._retrieve(state)
        result["code_retriever_meta"]["latency_ms"] = int(
            (time.monotonic() - start) * 1000
        )
        return result

    # ------------------------------------------------------------------
    # Core retrieval logic
    # ------------------------------------------------------------------

    async def _retrieve(self, state: Dict[str, Any]) -> Dict[str, Any]:
        session = state["session"]
        redis = state["redis"]
        parsed: Dict[str, Any] = state["parsed_event"]
        tenant_id: str = state["tenant_id"]

        crash_file: str = parsed.get("crash_file") or ""
        crash_line: int = int(parsed.get("crash_line") or 0)
        stack_frames: List[Dict[str, Any]] = parsed.get("stack_frames") or []

        # Request-scoped file content cache: s3_key → list of line strings
        file_cache: Dict[str, List[str]] = {}

        assembled_snippets: List[str] = []
        total_tokens: int = 0
        files_fetched: int = 0
        cache_hits: int = 0
        cache_misses: int = 0
        fetched_symbol_ids: Set[str] = set()

        crashed_symbol = None

        # ── Step 0: Resolve explicitly mapped repository (if configured) ──────
        from sqlalchemy.future import select
        from app.models.github_integration_snapshots import ServiceRepoMappingSnapshot
        
        service_name = parsed.get("service_name")
        target_repo_url: Optional[str] = None
        
        if service_name:
            import uuid as _uuid_module
            try:
                t_uuid = _uuid_module.UUID(tenant_id)
                stmt = select(ServiceRepoMappingSnapshot).where(
                    ServiceRepoMappingSnapshot.tenant_id == t_uuid,
                    ServiceRepoMappingSnapshot.service_name == service_name
                ).order_by(ServiceRepoMappingSnapshot.synced_at.desc()).limit(1)
                res = await session.execute(stmt)
                mapping = res.scalar_one_or_none()
                if mapping:
                    target_repo_url = mapping.repo_url
                    logger.debug(
                        "code_retriever_resolved_repo_from_mapping",
                        extra={"service": service_name, "repo_url": target_repo_url}
                    )
            except Exception as e:
                logger.warning(f"Error fetching service mapping: {e}")

        # ── Step 1: Find the crashed function ─────────────────────────────────
        if crash_file and crash_line > 0:
            crashed_symbol = await self._find_symbol_by_location(
                session, tenant_id, crash_file, crash_line, target_repo_url
            )

            if crashed_symbol is not None:
                snippet, tokens, fh, ch, cm = await self._get_symbol_snippet(
                    redis, crashed_symbol, file_cache
                )
                if snippet and total_tokens + tokens <= _TOKEN_BUDGET:
                    assembled_snippets.append(snippet)
                    total_tokens += tokens
                    files_fetched += fh
                    cache_hits += ch
                    cache_misses += cm
                    fetched_symbol_ids.add(str(crashed_symbol.id))
                    logger.debug(
                        "code_retriever_crash_symbol_fetched",
                        extra={
                            "symbol": crashed_symbol.symbol_name,
                            "file": crashed_symbol.file_path,
                            "tokens": tokens,
                        },
                    )

        # ── Step 2: Stack trace frame functions ───────────────────────────────
        for frame in stack_frames:
            if total_tokens >= _TOKEN_BUDGET:
                break

            frame_file: str = str(frame.get("file") or "")
            frame_line: int = int(frame.get("line") or 0)

            if not frame_file or frame_line <= 0:
                continue

            symbol = await self._find_symbol_by_location(
                session, tenant_id, frame_file, frame_line, target_repo_url
            )

            if symbol is None or str(symbol.id) in fetched_symbol_ids:
                continue

            snippet, tokens, fh, ch, cm = await self._get_symbol_snippet(
                redis, symbol, file_cache
            )
            if snippet and total_tokens + tokens <= _TOKEN_BUDGET:
                assembled_snippets.append(snippet)
                total_tokens += tokens
                files_fetched += fh
                cache_hits += ch
                cache_misses += cm
                fetched_symbol_ids.add(str(symbol.id))
                logger.debug(
                    "code_retriever_frame_symbol_fetched",
                    extra={
                        "symbol": symbol.symbol_name,
                        "file": symbol.file_path,
                        "tokens": tokens,
                    },
                )

        # ── Step 3: Direct helper functions (calls[]) ─────────────────────────
        if crashed_symbol is not None:
            calls: List[str] = list(crashed_symbol.calls or [])
            for called_name in calls:
                if total_tokens >= _TOKEN_BUDGET:
                    break

                if not called_name or not called_name.strip():
                    continue

                helper = await self._find_symbol_by_name(
                    session, tenant_id, called_name.strip(), target_repo_url
                )

                if helper is None or str(helper.id) in fetched_symbol_ids:
                    continue

                snippet, tokens, fh, ch, cm = await self._get_symbol_snippet(
                    redis, helper, file_cache
                )
                if snippet and total_tokens + tokens <= _TOKEN_BUDGET:
                    assembled_snippets.append(snippet)
                    total_tokens += tokens
                    files_fetched += fh
                    cache_hits += ch
                    cache_misses += cm
                    fetched_symbol_ids.add(str(helper.id))
                    logger.debug(
                        "code_retriever_helper_symbol_fetched",
                        extra={
                            "symbol": helper.symbol_name,
                            "tokens": tokens,
                        },
                    )

        code_context: str = _SNIPPET_SEPARATOR.join(assembled_snippets)

        logger.info(
            "code_retriever_complete",
            extra={
                "tenant_id": tenant_id,
                "crash_file": crash_file,
                "crash_line": crash_line,
                "symbols_retrieved": len(assembled_snippets),
                "total_tokens": total_tokens,
                "files_fetched": files_fetched,
                "cache_hits": cache_hits,
                "cache_misses": cache_misses,
            },
        )

        return {
            "code_context": code_context,
            "code_retriever_meta": {
                "files_fetched": files_fetched,
                "tokens": total_tokens,
                "cache_hits": cache_hits,
                "cache_misses": cache_misses,
                "symbols_retrieved": len(assembled_snippets),
                "latency_ms": 0,  # overwritten by invoke()
            },
        }

    # ------------------------------------------------------------------
    # Database queries
    # ------------------------------------------------------------------

    async def _find_symbol_by_location(
        self,
        session: Any,
        tenant_id: str,
        file_path: str,
        line_number: int,
        repo_url: Optional[str] = None,
    ) -> Optional[Any]:
        """
        Find the CodeIndex row whose [start_line, end_line] range
        contains line_number within the given file_path.

        We normalize the SDK's file_path (which might be an absolute path
        from Windows/Docker) and extract the filename to query the DB.
        Then we verify that the DB's relative path is a valid suffix of the
        SDK's absolute path to avoid matching a different file with the same name.
        """
        import uuid as _uuid_module

        from sqlalchemy import and_, text
        from sqlalchemy.future import select

        from app.models.code_index import CodeIndex

        try:
            tenant_uuid = _uuid_module.UUID(tenant_id)
        except (ValueError, AttributeError):
            logger.warning(
                "code_retriever_invalid_tenant_uuid",
                extra={"tenant_id": tenant_id},
            )
            return None

        # Normalize the path from the SDK (handles Windows \ and POSIX /)
        normalized_path = file_path.replace("\\", "/")
        filename = normalized_path.split("/")[-1]

        try:
            if not repo_url:
                return None
            
            stmt = (
                select(CodeIndex)
                .where(
                    and_(
                        CodeIndex.tenant_id == tenant_uuid,
                        CodeIndex.repo_url == repo_url,
                        CodeIndex.file_path.ilike(f"%{filename}%"),
                        CodeIndex.start_line <= line_number,
                        CodeIndex.end_line >= line_number,
                    )
                )
            )
            stmt = stmt.order_by((CodeIndex.end_line - CodeIndex.start_line).asc())
            result = await session.execute(stmt)
            matches = result.scalars().all()
            
            # Find the best match whose DB file_path is a suffix of the SDK's path
            best_match = None
            for match in matches:
                # DB file paths use POSIX slashes (e.g. app/services/order_service.py)
                if normalized_path.endswith(match.file_path):
                    best_match = match
                    break
            
            # Since fallback is removed, we only return if there is an exact suffix match.
            return best_match

        except Exception as exc:
            logger.warning(
                "code_retriever_location_query_failed",
                extra={
                    "file_path": file_path,
                    "line_number": line_number,
                    "error": str(exc),
                },
            )
            return None

    async def _find_symbol_by_name(
        self,
        session: Any,
        tenant_id: str,
        symbol_name: str,
        repo_url: Optional[str] = None,
    ) -> Optional[Any]:
        """
        Find a CodeIndex row by exact symbol name within the tenant.

        Used to fetch helper functions referenced in crashed_symbol.calls[].
        Returns the first match (there should be at most one per tenant
        if the code index is correctly scoped to one repository version).
        """
        import uuid as _uuid_module

        from sqlalchemy import and_
        from sqlalchemy.future import select

        from app.models.code_index import CodeIndex

        try:
            tenant_uuid = _uuid_module.UUID(tenant_id)
        except (ValueError, AttributeError):
            return None

        try:
            if not repo_url:
                return None
                
            stmt = (
                select(CodeIndex)
                .where(
                    and_(
                        CodeIndex.tenant_id == tenant_uuid,
                        CodeIndex.repo_url == repo_url,
                        CodeIndex.symbol_name == symbol_name,
                    )
                )
                .limit(1)
            )
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

        except Exception as exc:
            logger.warning(
                "code_retriever_name_query_failed",
                extra={"symbol_name": symbol_name, "error": str(exc)},
            )
            return None

    # ------------------------------------------------------------------
    # File content fetch and snippet assembly
    # ------------------------------------------------------------------

    async def _get_symbol_snippet(
        self,
        redis: Any,
        symbol: Any,
        file_cache: Dict[str, List[str]],
    ) -> Tuple[str, int, int, int, int]:
        """
        Assemble a human-readable annotated code snippet for a symbol.

        1. Fetch the source file content via the three-tier cache.
        2. Slice the exact function lines [start_line, end_line].
        3. Prepend a header comment with file path, line range, and symbol name.
        4. Count tokens via tiktoken.

        Returns
        -------
        tuple: (snippet_text, token_count, files_fetched, cache_hits, cache_misses)
               Returns ("", 0, 0, 0, 0) if the file cannot be fetched.
        """
        s3_key: str = getattr(symbol, "s3_key", None) or ""
        if not s3_key:
            return "", 0, 0, 0, 0

        files_fetched: int = 0
        cache_hits: int = 0
        cache_misses: int = 0

        # ── Tier 1: Request-scoped Python dict ────────────────────────────────
        if s3_key in file_cache:
            cache_hits += 1
        else:
            # ── Tier 2: Redis L1 cache ─────────────────────────────────────
            redis_key: str = f"code:{s3_key}"
            try:
                cached_bytes = await redis.get(redis_key)
            except Exception:
                cached_bytes = None

            if cached_bytes is not None:
                content_str = (
                    cached_bytes.decode("utf-8", errors="replace")
                    if isinstance(cached_bytes, bytes)
                    else str(cached_bytes)
                )
                file_cache[s3_key] = content_str.splitlines()
                cache_hits += 1
            else:
                # ── Tier 3: S3 fetch ──────────────────────────────────────
                file_bytes = await self._fetch_from_s3(s3_key)
                if not file_bytes:
                    logger.warning(
                        "code_retriever_s3_fetch_empty",
                        extra={"s3_key": s3_key},
                    )
                    return "", 0, 0, 0, 0

                content_str = file_bytes.decode("utf-8", errors="replace")
                file_cache[s3_key] = content_str.splitlines()

                # Populate Redis L1 for subsequent agent executions
                try:
                    await redis.setex(
                        redis_key,
                        _REDIS_FILE_CACHE_TTL,
                        content_str,
                    )
                except Exception as exc:
                    logger.warning(
                        "code_retriever_redis_set_failed",
                        extra={"s3_key": s3_key, "error": str(exc)},
                    )

                cache_misses += 1
                files_fetched += 1

        # ── Slice the function lines ──────────────────────────────────────────
        lines: List[str] = file_cache[s3_key]
        start_line: int = getattr(symbol, "start_line", 1) or 1
        end_line: int = getattr(symbol, "end_line", start_line) or start_line
        file_path: str = getattr(symbol, "file_path", "") or ""
        symbol_name: str = getattr(symbol, "symbol_name", "") or ""
        chunk_type: str = getattr(symbol, "chunk_type", "function") or "function"

        # Convert 1-based line numbers to 0-based list indices
        slice_start: int = max(0, start_line - 1)
        slice_end: int = min(len(lines), end_line)
        code_slice: str = "\n".join(lines[slice_start:slice_end])

        # ── Build annotated snippet ───────────────────────────────────────────
        # This format gives GPT-4 full provenance context so it can reference
        # specific line numbers and function names in its root cause analysis.
        snippet: str = (
            f"# File: {file_path}  "
            f"Lines: {start_line}-{end_line}  "
            f"Symbol: {symbol_name} ({chunk_type})\n"
            f"{code_slice}"
        )

        # ── Count tokens ──────────────────────────────────────────────────────
        try:
            encoding = _get_encoding()
            token_count: int = len(encoding.encode(snippet))
        except Exception:
            # Fallback: rough approximation (4 chars per token)
            token_count = max(1, len(snippet) // 4)

        return snippet, token_count, files_fetched, cache_hits, cache_misses

    async def _fetch_from_s3(self, s3_key: str) -> Optional[bytes]:
        """
        Fetch raw file bytes from S3.
        Returns None on any error (ClientError, network failure, etc.).
        """
        try:
            import aioboto3
            from botocore.exceptions import ClientError

            from app.core.config import get_settings

            settings = get_settings()

            session = aioboto3.Session(
                aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
                aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
                region_name=settings.AWS_REGION_NAME,
            )

            async with session.client(
                "s3",
                endpoint_url=getattr(settings, "AWS_S3_ENDPOINT_URL", None),
            ) as s3_client:
                response = await s3_client.get_object(
                    Bucket=settings.AWS_S3_BUCKET_NAME,
                    Key=s3_key,
                )
                return await response["Body"].read()

        except Exception as exc:
            logger.warning(
                "code_retriever_s3_fetch_failed",
                extra={"s3_key": s3_key, "error": str(exc)},
            )
            return None
