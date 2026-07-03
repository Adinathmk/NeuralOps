"""
fastapi/app/agents/nodes/fix_generator.py

Fix Generator Node — Phase 4 LangGraph Agent Pipeline

Makes a second GPT-4o call focused exclusively on generating a concrete,
actionable code fix for the incident. Runs after the Analyzer node so it
can reference the confirmed root_cause in its prompt, producing a more
targeted fix than if it operated on raw error data alone.

Circuit breaker: shares the same Redis-backed breaker as the Analyzer but
under a distinct name ("openai_fix_generator") so the two breakers are
independent — a transient fix-generation failure does not trip the
analyzer circuit, and vice versa.

Fallback (circuit OPEN or API error):
  Returns a structured message instructing the engineer to apply the
  suggested_area from the Analyzer output, without hallucinating code.

Output schema (enforced via FixGeneratorOutput Pydantic model)
--------------------------------------------------------------
  {
    "suggested_fix":    str   — human-readable fix description
    "code_patch":       str   — diff or code snippet (may be empty string)
    "fix_confidence":   float — 0.0–1.0
    "fix_complexity":   str   — "trivial" | "minor" | "moderate" | "major"
  }

Inputs consumed from AgentState
--------------------------------
  parsed_event   — error_type, crash_file, crash_line, crash_method
  code_context   — assembled code snippets from CodeRetriever
  root_cause     — text produced by AnalyzerNode
  redis          — for circuit breaker state

Outputs written to AgentState
------------------------------
  suggested_fix, raw_fix_output, fix_generator_latency_ms,
  fix_fallback_used, fix_tokens
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lazy singletons
# ---------------------------------------------------------------------------

_gemini_client = None
_fix_cb = None


def _get_client():
    import google.generativeai as genai

    from app.core.config import get_settings

    genai.configure(api_key=get_settings().GEMINI_API_KEY)
    return genai.GenerativeModel(
        "models/gemini-2.5-flash",
        generation_config={
            "response_mime_type": "application/json",
            "temperature": 0.15,
            # 8192 tokens avoids mid-string JSON truncation for complex fixes.
            "max_output_tokens": 8192,
        },
    )


def _get_circuit_breaker():
    global _fix_cb
    if _fix_cb is None:
        from app.agents.circuit_breaker import CircuitBreaker

        _fix_cb = CircuitBreaker(
            name="openai_fix_generator",
            failure_threshold=5,
            success_threshold=2,
            timeout_seconds=30,
        )
    return _fix_cb


# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are NeuralOps Fix Generator, an expert software engineer who produces \
precise, actionable code fixes for production incidents.

You will receive:
1. A confirmed root cause analysis for a production error.
2. The relevant source code at the crash location.
3. The error metadata (type, message, location).

Your task:
- Produce a specific, implementable code fix.
- If you can write the exact corrected code, include it in "code_patch".
- If the fix requires broader context you do not have, describe it clearly \
  in "suggested_fix" and leave "code_patch" as an empty string.
- Do NOT hallucinate variable names or function signatures not present in \
  the provided code.
- Return ONLY a valid JSON object — no markdown, no preamble.

The JSON object MUST match this exact schema:
{
  "suggested_fix":  "<string: 1–4 sentence description of the fix>",
  "code_patch":     "<string: corrected code snippet or unified diff, or empty string>",
  "fix_confidence": <float between 0.0 and 1.0>,
  "fix_complexity": "<one of: trivial | minor | moderate | major>"
}
"""


def _build_user_prompt(
    error_type: str,
    error_message: str,
    crash_file: str,
    crash_line: int,
    crash_method: str,
    root_cause: str,
    code_context: str,
    sdk_meta: Optional[Dict[str, Any]],
) -> str:
    parts: List[str] = []

    parts.append("## Root Cause (confirmed by Analyzer)")
    parts.append(root_cause or "(Root cause analysis unavailable.)")

    parts.append("\n## Error Details")
    parts.append(f"**Type:** {error_type}")
    parts.append(f"**Message:** {error_message or '(none)'}")
    parts.append(f"**Location:** `{crash_file}:{crash_line}` in `{crash_method}`")

    if sdk_meta:
        parts.append("\n## Execution Environment / SDK Meta")
        parts.append(json.dumps(sdk_meta, indent=2))

    if code_context:
        parts.append("\n## Source Code at Crash Location")
        parts.append(code_context)
    else:
        parts.append(
            "\n## Source Code at Crash Location\n"
            "*(Not available — generate a descriptive fix without code.)*"
        )

    parts.append("\nGenerate the fix and return the JSON object as specified.")

    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Output schema
# ---------------------------------------------------------------------------


class FixGeneratorOutput(BaseModel):
    suggested_fix: str = Field(
        ...,
        min_length=1,
        description="Human-readable description of the required fix.",
    )
    code_patch: str = Field(
        default="",
        description="Corrected code snippet or unified diff. Empty if not available.",
    )
    fix_confidence: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="Model confidence in this fix (0.0–1.0).",
    )
    fix_complexity: str = Field(
        default="minor",
        description="Estimated fix complexity: trivial | minor | moderate | major.",
    )

    @field_validator("fix_confidence", mode="before")
    @classmethod
    def clamp_confidence(cls, v: Any) -> float:
        try:
            return max(0.0, min(1.0, float(v)))
        except (TypeError, ValueError):
            return 0.5

    @field_validator("fix_complexity", mode="before")
    @classmethod
    def normalise_complexity(cls, v: Any) -> str:
        valid = {"trivial", "minor", "moderate", "major"}
        s = str(v or "minor").lower().strip()
        return s if s in valid else "minor"


# ---------------------------------------------------------------------------
# Node implementation
# ---------------------------------------------------------------------------


class FixGeneratorNode:
    """
    LangGraph node: FixGenerator

    Makes a GPT-4o call to produce a concrete code fix, with circuit
    breaker protection and graceful fallback.
    """

    async def invoke(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """
        Generate a code fix for the incident.

        Parameters
        ----------
        state : dict
            Full AgentState.

        Returns
        -------
        dict
            Partial AgentState update:
            {suggested_fix, raw_fix_output, fix_generator_latency_ms,
             fix_fallback_used, fix_tokens}
        """
        start: float = time.monotonic()
        parsed: Dict[str, Any] = state["parsed_event"]
        redis = state["redis"]

        error_type: str = str(parsed.get("error_type") or "UnknownError")
        error_message: str = str(parsed.get("error_message") or "")
        crash_file: str = str(parsed.get("crash_file") or "")
        crash_line: int = int(parsed.get("crash_line") or 0)
        crash_method: str = str(parsed.get("crash_method") or "")
        sdk_meta: Optional[Dict[str, Any]] = parsed.get("sdk_meta")

        code_context: str = str(state.get("code_context") or "")
        root_cause: str = str(state.get("root_cause") or "")

        cb = _get_circuit_breaker()
        fallback_used: bool = False
        raw_output: str = ""
        suggested_fix: str = ""
        tokens: Dict[str, int] = {"prompt": 0, "completion": 0, "total": 0}

        try:
            # ── Circuit breaker check ─────────────────────────────────────────
            if not await cb.can_execute(redis):
                raise RuntimeError(
                    f"Circuit breaker OPEN for '{cb.name}' — request blocked."
                )

            # ── Build prompt ──────────────────────────────────────────────────
            user_prompt = _build_user_prompt(
                error_type=error_type,
                error_message=error_message,
                crash_file=crash_file,
                crash_line=crash_line,
                crash_method=crash_method,
                root_cause=root_cause,
                code_context=code_context,
                sdk_meta=sdk_meta,
            )

            # ── Gemini call ───────────────────────────────────────────────
            client = _get_client()
            full_prompt = f"{_SYSTEM_PROMPT}\n\n{user_prompt}"
            response = await client.generate_content_async(full_prompt)

            raw_output = response.text or ""
            usage = getattr(response, "usage_metadata", None)

            # ── Detect truncated JSON and retry once ──────────────────────
            # Gemini sometimes returns a JSON string cut off before the
            # closing brace when the response length approaches the token
            # limit. Detect this early and retry rather than letting Pydantic
            # raise a ValidationError that triggers the fallback path.
            if _looks_truncated(raw_output):
                logger.warning(
                    "fix_generator_truncated_response_retry",
                    extra={"error_type": error_type, "raw_tail": raw_output[-80:]},
                )
                response = await client.generate_content_async(full_prompt)
                raw_output = response.text or ""
                usage = getattr(response, "usage_metadata", None)

            # ── Pydantic validation ───────────────────────────────────────
            # Strip markdown fences (```json ... ```) that the model sometimes
            # wraps around its JSON response despite being asked not to.
            json_match = re.search(r"\{.*\}", raw_output, re.DOTALL)
            clean_json = json_match.group(0) if json_match else raw_output
            output = FixGeneratorOutput.model_validate_json(clean_json)
            suggested_fix = output.suggested_fix

            tokens = {
                "prompt": usage.prompt_token_count if usage else 0,
                "completion": usage.candidates_token_count if usage else 0,
                "total": usage.total_token_count if usage else 0,
            }

            await cb.record_success(redis)

            logger.info(
                "fix_generator_success",
                extra={
                    "error_type": error_type,
                    "fix_complexity": output.fix_complexity,
                    "fix_confidence": output.fix_confidence,
                    "prompt_tokens": tokens["prompt"],
                    "completion_tokens": tokens["completion"],
                },
            )

        except Exception as exc:
            error_name = type(exc).__name__
            if "ValidationError" not in error_name:
                await cb.record_failure(redis)

            fallback_used = True
            suggested_fix = _build_fallback_fix(
                exc=exc,
                root_cause=root_cause,
                crash_file=crash_file,
                crash_method=crash_method,
            )

            logger.warning(
                f"fix_generator_fallback_used: {error_name} - {str(exc)[:300]}",
                extra={
                    "error_type": error_type,
                    "exception_type": error_name,
                    "exception": str(exc)[:300],
                },
            )

        latency_ms: int = int((time.monotonic() - start) * 1000)

        return {
            "suggested_fix": suggested_fix,
            "raw_fix_output": raw_output,
            "fix_generator_latency_ms": latency_ms,
            "fix_fallback_used": fallback_used,
            "fix_tokens": tokens,
        }


# ---------------------------------------------------------------------------
# Fallback helper
# ---------------------------------------------------------------------------


def _build_fallback_fix(
    exc: Exception,
    root_cause: str,
    crash_file: str,
    crash_method: str,
) -> str:
    """
    Build a fallback fix description when GPT-4 is unavailable.
    References the root_cause from the Analyzer to provide maximum
    actionable guidance without hallucinating code.
    """
    location = f"`{crash_method}`" if crash_method else "the crash location"
    if crash_file:
        location += f" in `{crash_file}`"

    if root_cause and root_cause.strip():
        return (
            f"[Fix Generation Unavailable — {type(exc).__name__}]\n\n"
            f"Based on the root cause analysis, address {location}. "
            f"Root cause: {root_cause.strip()}"
        )

    return (
        f"[Fix Generation Unavailable — {type(exc).__name__}]\n\n"
        f"Review {location} and apply a fix based on the error details "
        f"in the stack trace above."
    )


def _looks_truncated(raw: str) -> bool:
    """
    Return True if the Gemini response looks like it was cut off before
    the JSON object was closed.

    Heuristics (any one is sufficient):
    - The text does not end with a closing brace '}' (after stripping
      whitespace) — the most reliable signal.
    - The text ends mid-string (last non-whitespace char is a double-quote
      without a preceding backslash, yet the overall brace count is unbalanced).
    """
    stripped = raw.strip()
    if not stripped:
        return False
    if not stripped.endswith("}"):
        return True
    # Brace balance check — handles the edge case where the last char is '}'
    # but an earlier object was never closed.
    return stripped.count("{") != stripped.count("}")

