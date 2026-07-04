"""
fastapi/app/agents/nodes/_gemini_utils.py

Shared helpers for LangGraph nodes that call Gemini and expect a JSON
response. Centralizes truncation detection and the single-retry pattern
so analyzer.py, fix_generator.py, and patch_generator.py behave
consistently instead of each reimplementing (or omitting) it.
"""

from __future__ import annotations

from typing import Any

from langsmith import traceable


def looks_truncated(raw: str) -> bool:
    """
    Return True if a Gemini text response looks like it was cut off before
    the JSON object was closed.

    Heuristics (any one is sufficient):
    - The text does not end with a closing brace '}' (after stripping
      whitespace) — the most reliable signal.
    - The brace count is unbalanced even though the last char is '}' —
      handles the edge case where an earlier object was never closed.
    """
    stripped = raw.strip()
    if not stripped:
        return False
    if not stripped.endswith("}"):
        return True
    return stripped.count("{") != stripped.count("}")


@traceable(run_type="llm", name="gemini_generate")
async def generate_with_truncation_retry(
    client: Any,
    prompt: str,
    *,
    node_name: str,
    logger: Any,
) -> Any:
    """
    Call client.generate_content_async(prompt), and if the response text
    looks truncated, retry exactly once.

    Parameters
    ----------
    client : genai.GenerativeModel
        Already-configured Gemini client.
    prompt : str
        The user prompt (system_instruction should already be set on the
        client itself, not concatenated into this string).
    node_name : str
        Used only for the log line, e.g. "analyzer", "fix_generator".
    logger : logging.Logger
        The calling module's logger, so log lines carry the right module name.

    Returns
    -------
    The Gemini response object (post-retry if a retry was needed).
    """
    response = await client.generate_content_async(prompt)
    raw_output = response.text or ""

    if looks_truncated(raw_output):
        logger.warning(
            f"{node_name}_truncated_response_retry",
            extra={"raw_tail": raw_output[-80:]},
        )
        response = await client.generate_content_async(prompt)

    return response
