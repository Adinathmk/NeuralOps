"""
fastapi/app/agents/nodes/analyzer.py

Analyzer Node — Phase 4 LangGraph Agent Pipeline

Makes a structured GPT-4o call to produce a root-cause analysis for the
incident. Uses response_format=json_object and validates the raw JSON
output through a Pydantic model before accepting it.

Circuit breaker (shared across all worker replicas via Redis):
  - 5 failures in 60s → OPEN for 30s
  - Fallback: return matched playbook instructions as root_cause, or a
    generic message if no playbook matched. Marks analyzer_fallback_used=True.

Prompt design
-------------
  system: NeuralOps analyst persona with strict JSON output instruction
  user  : structured block containing error metadata, stack trace,
          code context (from CodeRetriever), and optional playbook instructions

Output schema (enforced via AnalyzerOutput Pydantic model)
----------------------------------------------------------
  {
    "root_cause":            str,
    "root_cause_confidence": float  (0.0–1.0),
    "reasoning_steps":       [str, ...],
    "affected_component":    str,
    "suggested_area":        str
  }

Inputs consumed from AgentState
--------------------------------
  parsed_event   — error_type, error_message, crash_file, crash_line,
                   crash_method, stack_frames
  code_context   — assembled code snippets from CodeRetriever
  playbook_instructions — optional matched runbook text
  redis          — for circuit breaker state

Outputs written to AgentState
------------------------------
  root_cause, raw_analysis_output, analyzer_latency_ms,
  analyzer_fallback_used, analyzer_tokens
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lazy singletons — initialised on first use to avoid import-time failures
# when OPENAI_API_KEY is not yet set in the environment.
# ---------------------------------------------------------------------------

_gemini_client = None
_analyzer_cb = None


def _get_client():
    import google.generativeai as genai

    from app.core.config import get_settings

    genai.configure(api_key=get_settings().GEMINI_API_KEY)
    return genai.GenerativeModel(
        "models/gemini-2.5-flash",
        generation_config={
            "response_mime_type": "application/json",
            "response_schema": AnalyzerOutput,
            "temperature": 0.1,
            "max_output_tokens": 8192,
        },
    )


def _get_circuit_breaker():
    global _analyzer_cb
    if _analyzer_cb is None:
        from app.agents.circuit_breaker import CircuitBreaker

        _analyzer_cb = CircuitBreaker(
            name="openai_analyzer",
            failure_threshold=5,
            success_threshold=2,
            timeout_seconds=30,
        )
    return _analyzer_cb


# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are NeuralOps, an expert production incident analyst embedded inside \
an automated debugging platform.

You will be given a structured error event from a production service, \
relevant source code extracted from the exact crash location, and optionally \
a runbook with remediation guidance.

Your task:
1. Identify the PRECISE root cause of the error.
2. Reference exact function names, variable names, and line numbers from \
the provided code wherever possible.
3. Return ONLY a valid JSON object — no markdown, no preamble, no explanation \
outside the JSON.

The JSON object MUST match this exact schema:
{
  "root_cause":            "<string: precise root cause in 1-3 sentences>",
  "root_cause_confidence": <float between 0.0 and 1.0>,
  "reasoning_steps":       ["<step 1>", "<step 2>", ...],
  "affected_component":    "<string: primary function/class responsible>",
  "suggested_area":        "<string: file path and function that needs fixing>"
}

If code context is absent, base your analysis on the error type and stack trace \
and reduce root_cause_confidence accordingly.
"""


def _build_user_prompt(
    error_type: str,
    error_message: str,
    service_name: str,
    environment: str,
    crash_file: str,
    crash_line: int,
    crash_method: str,
    stack_trace_str: str,
    code_context: str,
    playbook_instructions: Optional[str],
    context_log_count: int,
) -> str:
    parts: List[str] = []

    parts.append("## Error Event")
    parts.append(f"**Service:** {service_name}  **Environment:** {environment}")
    parts.append(f"**Error Type:** {error_type}")
    parts.append(f"**Error Message:** {error_message or '(none)'}")
    parts.append(f"**Crash Location:** `{crash_file}:{crash_line}` in `{crash_method}`")
    parts.append(f"**Context Log Entries:** {context_log_count}")

    if stack_trace_str:
        parts.append("\n## Stack Trace")
        parts.append(stack_trace_str)

    if code_context:
        parts.append("\n## Relevant Source Code")
        parts.append(code_context)
    else:
        parts.append(
            "\n## Relevant Source Code\n"
            "*(Not available — repository not indexed or symbol not found.)*"
        )

    if playbook_instructions:
        parts.append("\n## Runbook Instructions (Matched Playbook)")
        parts.append(playbook_instructions)

    parts.append("\nAnalyse this incident and return the JSON object as specified.")

    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Output schema
# ---------------------------------------------------------------------------


class AnalyzerOutput(BaseModel):
    root_cause: str = Field(
        ...,
        description="Precise root cause of the error.",
    )
    root_cause_confidence: float = Field(
        ...,
        description="Model confidence in this root cause (0.0–1.0).",
    )
    reasoning_steps: List[str] = Field(
        description="Step-by-step reasoning trace.",
    )
    affected_component: str = Field(
        description="Primary function or class responsible for the error.",
    )
    suggested_area: str = Field(
        description="File path and function that needs to be fixed.",
    )

    @field_validator("root_cause_confidence", mode="before")
    @classmethod
    def clamp_confidence(cls, v: Any) -> float:
        try:
            f = float(v)
            return max(0.0, min(1.0, f))
        except (TypeError, ValueError):
            return 0.5


# ---------------------------------------------------------------------------
# Node implementation
# ---------------------------------------------------------------------------


class AnalyzerNode:
    """
    LangGraph node: Analyzer

    Makes a GPT-4o call with structured JSON output, validates via Pydantic,
    and falls back to playbook instructions on circuit-open or API error.
    """

    async def invoke(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """
        Run root-cause analysis via GPT-4o.

        Parameters
        ----------
        state : dict
            Full AgentState.

        Returns
        -------
        dict
            Partial AgentState update:
            {root_cause, raw_analysis_output, analyzer_latency_ms,
             analyzer_fallback_used, analyzer_tokens}
        """
        start: float = time.monotonic()
        parsed: Dict[str, Any] = state["parsed_event"]
        redis = state["redis"]

        error_type: str = str(parsed.get("error_type") or "UnknownError")
        error_message: str = str(parsed.get("error_message") or "")
        service_name: str = str(parsed.get("service_name") or "")
        environment: str = str(parsed.get("environment") or "")
        crash_file: str = str(parsed.get("crash_file") or "")
        crash_line: int = int(parsed.get("crash_line") or 0)
        crash_method: str = str(parsed.get("crash_method") or "")
        context_log_count: int = int(parsed.get("context_log_count") or 0)
        stack_frames: List[Dict[str, Any]] = parsed.get("stack_frames") or []

        code_context: str = str(state.get("code_context") or "")
        playbook_instructions: Optional[str] = state.get("playbook_instructions")

        # Format stack trace for the prompt
        stack_trace_str: str = "\n".join(
            f"  at {f.get('method', '?')} "
            f"({f.get('file', '?')}:{f.get('line', '?')})"
            for f in stack_frames
        )

        cb = _get_circuit_breaker()
        fallback_used: bool = False
        raw_output: str = ""
        root_cause: str = ""
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
                service_name=service_name,
                environment=environment,
                crash_file=crash_file,
                crash_line=crash_line,
                crash_method=crash_method,
                stack_trace_str=stack_trace_str,
                code_context=code_context,
                playbook_instructions=playbook_instructions,
                context_log_count=context_log_count,
            )

            # ── Gemini call ───────────────────────────────────────────────────
            client = _get_client()
            full_prompt = f"{_SYSTEM_PROMPT}\n\n{user_prompt}"
            response = await client.generate_content_async(full_prompt)

            raw_output = response.text or ""
            usage = getattr(response, "usage_metadata", None)

            # ── Lenient JSON parsing ───────────────────────────────────────────
            cleaned_output = raw_output.strip()
            if cleaned_output.startswith("```json"):
                cleaned_output = cleaned_output[7:]
            elif cleaned_output.startswith("```"):
                cleaned_output = cleaned_output[3:]
            if cleaned_output.endswith("```"):
                cleaned_output = cleaned_output[:-3]
            cleaned_output = cleaned_output.strip()

            try:
                output_dict = json.loads(cleaned_output)
            except json.JSONDecodeError:
                output_dict = {}

            root_cause = output_dict.get(
                "root_cause",
                "Automated analysis completed, but root cause extraction failed. Please check raw output.",
            )
            root_cause_confidence = output_dict.get("root_cause_confidence", 0.5)
            try:
                root_cause_confidence = float(root_cause_confidence)
            except (ValueError, TypeError):
                root_cause_confidence = 0.5

            tokens = {
                "prompt": usage.prompt_token_count if usage else 0,
                "completion": usage.candidates_token_count if usage else 0,
                "total": usage.total_token_count if usage else 0,
            }

            await cb.record_success(redis)

            logger.info(
                "analyzer_success",
                extra={
                    "error_type": error_type,
                    "prompt_tokens": tokens["prompt"],
                    "completion_tokens": tokens["completion"],
                    "root_cause_confidence": root_cause_confidence,
                },
            )

        except Exception as exc:
            # Record failure on the circuit breaker only for API/network errors,
            # not for Pydantic validation failures (those are transient GPT quirks)
            error_name = type(exc).__name__
            if "ValidationError" not in error_name:
                await cb.record_failure(redis)

            fallback_used = True
            root_cause = _build_fallback_root_cause(
                exc=exc,
                playbook_instructions=playbook_instructions,
                error_type=error_type,
                crash_method=crash_method,
                crash_line=crash_line,
            )

            logger.warning(
                f"analyzer_fallback_used: {error_name} - {str(exc)[:300]}",
                extra={
                    "error_type": error_type,
                    "exception_type": error_name,
                    "exception": str(exc)[:300],
                    "has_playbook": playbook_instructions is not None,
                },
            )

        latency_ms: int = int((time.monotonic() - start) * 1000)

        return {
            "root_cause": root_cause,
            "raw_analysis_output": raw_output,
            "analyzer_latency_ms": latency_ms,
            "analyzer_fallback_used": fallback_used,
            "analyzer_tokens": tokens,
        }


# ---------------------------------------------------------------------------
# Fallback helper
# ---------------------------------------------------------------------------


def _build_fallback_root_cause(
    exc: Exception,
    playbook_instructions: Optional[str],
    error_type: str,
    crash_method: str,
    crash_line: int,
) -> str:
    """
    Build a human-readable fallback root cause when GPT-4 is unavailable.

    Prefers matched playbook instructions when present (they contain
    domain-specific guidance). Falls back to a generic structured message.
    """
    if playbook_instructions and playbook_instructions.strip():
        return (
            f"[GPT-4 Unavailable — Playbook Guidance]\n\n"
            f"{playbook_instructions.strip()}"
        )

    return (
        f"Automated analysis unavailable (GPT-4 unreachable: "
        f"{type(exc).__name__}). "
        f"The error '{error_type}' occurred in `{crash_method}` "
        f"at line {crash_line}. "
        f"Please review the stack trace and code context manually."
    )
