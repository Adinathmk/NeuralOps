"""
fastapi/app/agents/nodes/patch_generator.py

Patch Generator Node — LangGraph Agent Pipeline

Positioned after action_decision, only if action == "create_incident".
Calls Gemini to produce a structured search/replace patch for the
crashed file, validates each patch against the actual file content
fetched from S3 (via the same three-tier cache as CodeRetrieverNode),
and writes the result to AgentState.

NEVER fails the pipeline — all exceptions are caught and result in
empty structured_patch + skip_reason set.

Inputs consumed from AgentState
--------------------------------
  tenant_id
  parsed_event     — crash_file, crash_line
  code_context     — assembled snippets from CodeRetriever
  root_cause       — from AnalyzerNode
  suggested_fix    — from FixGeneratorNode
  session          — AsyncSession bound to DB-2
  redis            — redis.asyncio.Redis

Outputs written to AgentState
------------------------------
  structured_patch          : str   (JSON string of validated patches, or "")
  patch_confidence          : float
  patch_skip_reason         : str
  patch_generator_latency_ms: int
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Redis TTL for file content cache (mirrors code_retriever.py)
# ---------------------------------------------------------------------------
_REDIS_FILE_CACHE_TTL: int = 86_400  # 24 hours

# ---------------------------------------------------------------------------
# Lazy singletons
# ---------------------------------------------------------------------------

_patch_cb = None


def _get_client():
    import google.generativeai as genai

    from app.core.config import get_settings

    genai.configure(api_key=get_settings().GEMINI_API_KEY)
    return genai.GenerativeModel(
        "models/gemini-2.5-flash",
        generation_config={
            "response_mime_type": "application/json",
            "temperature": 0.10,
            "max_output_tokens": 4096,
        },
    )


def _get_circuit_breaker():
    global _patch_cb
    if _patch_cb is None:
        from app.agents.circuit_breaker import CircuitBreaker

        _patch_cb = CircuitBreaker(
            name="gemini_patch_generator",
            failure_threshold=5,
            success_threshold=2,
            timeout_seconds=30,
        )
    return _patch_cb


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are NeuralOps Patch Generator, an expert software engineer.
You will receive a root cause analysis, a suggested fix, and the full \
source code of the file that contains the crash.

Your task:
- Produce a list of exact search/replace patches that implement the fix.
- Each patch must contain a "search" string that appears VERBATIM in \
  the provided source file, and a "replace" string that is the corrected \
  version.
- Keep patches minimal — change only what is required.
- If the fix is safe and self-contained, set patch_confidence high (0.8–1.0).
- If the fix requires changes across multiple files, external libraries, \
  or you cannot produce a safe patch, set skip_reason to a short explanation \
  and return an empty patches array.
- Return ONLY a valid JSON object — no markdown, no preamble.

Schema:
{
  "patches": [
    {
      "file": "<relative file path as stored in the code index>",
      "search": "<exact verbatim multi-line block to find in file>",
      "replace": "<exact multi-line block to replace it with>"
    }
  ],
  "patch_confidence": 0.85,
  "skip_reason": ""
}
"""


def _build_user_prompt(
    crash_file: str,
    crash_line: int,
    root_cause: str,
    suggested_fix: str,
    code_context: str,
    full_file_content: str,
) -> str:
    parts: List[str] = []

    parts.append("## Root Cause")
    parts.append(root_cause or "(unavailable)")

    parts.append("\n## Suggested Fix")
    parts.append(suggested_fix or "(unavailable)")

    parts.append(f"\n## Crash Location\nFile: `{crash_file}`  Line: {crash_line}")

    if code_context:
        parts.append("\n## Relevant Code Context (from agent)")
        parts.append(code_context)

    parts.append("\n## Full Source File (use this for the search/replace strings)")
    parts.append(f"```\n{full_file_content}\n```")

    parts.append(
        "\nProduce the patch JSON. "
        "The 'search' value MUST be copied VERBATIM from the source file above."
    )

    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# S3 fetch (mirrors code_retriever._fetch_from_s3)
# ---------------------------------------------------------------------------


async def _fetch_from_s3(s3_key: str) -> Optional[bytes]:
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
            "patch_generator_s3_fetch_failed",
            extra={"s3_key": s3_key, "error": str(exc)},
        )
        return None


# ---------------------------------------------------------------------------
# DB query — identical pattern to code_retriever._find_symbol_by_location
# ---------------------------------------------------------------------------


async def _find_symbol_by_location(
    session: Any,
    tenant_id: str,
    file_path: str,
    line_number: int,
) -> Optional[Any]:
    import uuid as _uuid_module

    from sqlalchemy import and_
    from sqlalchemy.future import select

    from app.models.code_index import CodeIndex

    try:
        tenant_uuid = _uuid_module.UUID(tenant_id)
    except (ValueError, AttributeError):
        logger.warning(
            "patch_generator_invalid_tenant_uuid",
            extra={"tenant_id": tenant_id},
        )
        return None

    try:
        stmt = (
            select(CodeIndex)
            .where(
                and_(
                    CodeIndex.tenant_id == tenant_uuid,
                    CodeIndex.file_path.ilike(f"%{file_path}%"),
                    CodeIndex.start_line <= line_number,
                    CodeIndex.end_line >= line_number,
                )
            )
            .order_by((CodeIndex.end_line - CodeIndex.start_line).asc())
            .limit(1)
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()
    except Exception as exc:
        logger.warning(
            "patch_generator_location_query_failed",
            extra={
                "file_path": file_path,
                "line_number": line_number,
                "error": str(exc),
            },
        )
        return None


# ---------------------------------------------------------------------------
# Three-tier file fetch (request dict → Redis → S3)
# ---------------------------------------------------------------------------


async def _fetch_full_file(
    redis: Any,
    s3_key: str,
    request_cache: Dict[str, str],
) -> Optional[str]:
    """
    Fetch full source file content as a string.
    Tier 1: request-scoped dict
    Tier 2: Redis L1 cache (key = code:{s3_key}, TTL 24h)
    Tier 3: S3
    """
    if s3_key in request_cache:
        return request_cache[s3_key]

    redis_key = f"code:{s3_key}"
    try:
        cached_bytes = await redis.get(redis_key)
    except Exception:
        cached_bytes = None

    if cached_bytes is not None:
        content = (
            cached_bytes.decode("utf-8", errors="replace")
            if isinstance(cached_bytes, bytes)
            else str(cached_bytes)
        )
        request_cache[s3_key] = content
        return content

    # S3 fetch
    file_bytes = await _fetch_from_s3(s3_key)
    if not file_bytes:
        return None

    content = file_bytes.decode("utf-8", errors="replace")
    request_cache[s3_key] = content

    # Populate Redis L1
    try:
        await redis.setex(redis_key, _REDIS_FILE_CACHE_TTL, content)
    except Exception as exc:
        logger.warning(
            "patch_generator_redis_set_failed",
            extra={"s3_key": s3_key, "error": str(exc)},
        )

    return content


# ---------------------------------------------------------------------------
# Node implementation
# ---------------------------------------------------------------------------


class PatchGeneratorNode:
    """
    LangGraph node: PatchGenerator

    Generates a structured search/replace patch for the crashed file
    using Gemini, with circuit breaker protection. Never fails the pipeline.
    """

    async def invoke(self, state: Dict[str, Any]) -> Dict[str, Any]:
        start: float = time.monotonic()

        parsed: Dict[str, Any] = state.get("parsed_event") or {}
        crash_file: str = str(parsed.get("crash_file") or "")
        crash_line: int = int(parsed.get("crash_line") or 0)
        root_cause: str = str(state.get("root_cause") or "")
        suggested_fix: str = str(state.get("suggested_fix") or "")
        code_context: str = str(state.get("code_context") or "")
        tenant_id: str = str(state.get("tenant_id") or "")
        session = state.get("session")
        redis = state.get("redis")

        structured_patch: str = ""
        patch_confidence: float = 0.0
        patch_skip_reason: str = ""

        try:
            # ── 0. Guard: abort if fix_generator reported a failure ───────────
            # When fix_generator.py cannot produce a valid JSON response it
            # writes a human-readable sentinel string starting with
            # "[Fix Generation Unavailable".  If we receive that, the patch
            # generator has nothing reliable to base a code change on, so we
            # skip immediately rather than letting Gemini hallucinate a patch
            # (which previously caused it to delete the crashing line entirely).
            if "[Fix Generation Unavailable" in suggested_fix:
                patch_skip_reason = (
                    "fix_generator reported unavailability — patch generation skipped "
                    "to prevent hallucinated code changes."
                )
                latency_ms = int((time.monotonic() - start) * 1000)
                logger.info(
                    "patch_generator_skipped_fix_unavailable",
                    extra={"tenant_id": tenant_id, "crash_file": crash_file},
                )
                return _empty_result(patch_skip_reason, latency_ms)

            # ── 1. Find the s3_key for the crashed file ───────────────────────
            if not crash_file or not session:
                patch_skip_reason = "Missing crash_file or DB session — cannot retrieve source."
                latency_ms = int((time.monotonic() - start) * 1000)
                return _empty_result(patch_skip_reason, latency_ms)

            symbol = await _find_symbol_by_location(
                session, tenant_id, crash_file, crash_line
            )
            if symbol is None:
                patch_skip_reason = (
                    f"No code_index entry found for {crash_file}:{crash_line}."
                )
                latency_ms = int((time.monotonic() - start) * 1000)
                return _empty_result(patch_skip_reason, latency_ms)

            s3_key: str = getattr(symbol, "s3_key", None) or ""
            file_path_from_index: str = getattr(symbol, "file_path", crash_file) or crash_file

            if not s3_key:
                patch_skip_reason = "CodeIndex row has no s3_key."
                latency_ms = int((time.monotonic() - start) * 1000)
                return _empty_result(patch_skip_reason, latency_ms)

            # ── 2. Fetch entire file content (three-tier cache) ───────────────
            request_cache: Dict[str, str] = {}
            full_file_content = await _fetch_full_file(redis, s3_key, request_cache)

            if not full_file_content:
                patch_skip_reason = f"Could not fetch file content from S3 (key={s3_key})."
                latency_ms = int((time.monotonic() - start) * 1000)
                return _empty_result(patch_skip_reason, latency_ms)

            # ── 3. Circuit breaker check ──────────────────────────────────────
            cb = _get_circuit_breaker()
            if not await cb.can_execute(redis):
                patch_skip_reason = (
                    f"Circuit breaker OPEN for '{cb.name}' — patch generation skipped."
                )
                latency_ms = int((time.monotonic() - start) * 1000)
                return _empty_result(patch_skip_reason, latency_ms)

            # ── 4. Gemini call ────────────────────────────────────────────────
            user_prompt = _build_user_prompt(
                crash_file=file_path_from_index,
                crash_line=crash_line,
                root_cause=root_cause,
                suggested_fix=suggested_fix,
                code_context=code_context,
                full_file_content=full_file_content,
            )
            full_prompt = f"{_SYSTEM_PROMPT}\n\n{user_prompt}"

            client = _get_client()
            response = await client.generate_content_async(full_prompt)
            raw_output: str = response.text or ""

            # ── 5. Parse and validate JSON response ───────────────────────────
            import re

            json_match = re.search(r"\{.*\}", raw_output, re.DOTALL)
            clean_json = json_match.group(0) if json_match else raw_output

            try:
                parsed_response: Dict[str, Any] = json.loads(clean_json)
            except json.JSONDecodeError as exc:
                await cb.record_failure(redis)
                patch_skip_reason = f"Gemini returned invalid JSON: {exc}"
                latency_ms = int((time.monotonic() - start) * 1000)
                logger.warning(
                    "patch_generator_json_parse_failed",
                    extra={"error": str(exc), "raw": raw_output[:300]},
                )
                return _empty_result(patch_skip_reason, latency_ms)

            patches: List[Dict[str, str]] = parsed_response.get("patches") or []
            patch_confidence = float(parsed_response.get("patch_confidence") or 0.0)
            patch_confidence = max(0.0, min(1.0, patch_confidence))
            patch_skip_reason = str(parsed_response.get("skip_reason") or "")

            # ── 6. Validate each patch against file content ───────────────────
            validated_patches: List[Dict[str, str]] = []
            for patch in patches:
                search_str = patch.get("search", "")
                replace_str = patch.get("replace", "")
                file_in_patch = patch.get("file", file_path_from_index)

                if not search_str:
                    logger.warning(
                        "patch_generator_empty_search",
                        extra={"file": file_in_patch},
                    )
                    continue

                if search_str in full_file_content:
                    validated_patches.append(
                        {
                            "file": file_in_patch,
                            "search": search_str,
                            "replace": replace_str,
                        }
                    )
                else:
                    logger.warning(
                        "patch_generator_search_not_found",
                        extra={
                            "file": file_in_patch,
                            "search_preview": search_str[:120],
                        },
                    )

            if not validated_patches:
                if not patch_skip_reason:
                    patch_skip_reason = (
                        "No patches passed verbatim validation against file content."
                    )
                patch_confidence = 0.0
                structured_patch = ""
            else:
                structured_patch = json.dumps(
                    {
                        "patches": validated_patches,
                        "patch_confidence": patch_confidence,
                        "skip_reason": patch_skip_reason,
                    }
                )

            await cb.record_success(redis)

            logger.info(
                "patch_generator_success",
                extra={
                    "tenant_id": tenant_id,
                    "crash_file": crash_file,
                    "patches_validated": len(validated_patches),
                    "patch_confidence": patch_confidence,
                    "skip_reason": patch_skip_reason or None,
                },
            )

        except Exception as exc:
            patch_skip_reason = (
                f"patch_generator unhandled exception: {type(exc).__name__}: {str(exc)[:200]}"
            )
            structured_patch = ""
            patch_confidence = 0.0
            logger.exception(
                "patch_generator_unhandled_exception",
                extra={"tenant_id": tenant_id, "crash_file": crash_file, "error": str(exc)},
            )
            # Attempt to record circuit breaker failure
            try:
                cb = _get_circuit_breaker()
                if redis:
                    await cb.record_failure(redis)
            except Exception:
                pass

        latency_ms = int((time.monotonic() - start) * 1000)
        return {
            "structured_patch": structured_patch,
            "patch_confidence": patch_confidence,
            "patch_skip_reason": patch_skip_reason,
            "patch_generator_latency_ms": latency_ms,
        }


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _empty_result(skip_reason: str, latency_ms: int) -> Dict[str, Any]:
    logger.info(
        "patch_generator_skipped",
        extra={"skip_reason": skip_reason, "latency_ms": latency_ms},
    )
    return {
        "structured_patch": "",
        "patch_confidence": 0.0,
        "patch_skip_reason": skip_reason,
        "patch_generator_latency_ms": latency_ms,
    }