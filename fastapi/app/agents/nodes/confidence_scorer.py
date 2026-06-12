"""
fastapi/app/agents/nodes/confidence_scorer.py

Confidence Scorer Node — Phase 4 LangGraph Agent Pipeline

Computes a single composite confidence score in [0.0, 1.0] from three
independent sub-signals. This score is the sole input to the Action
Decision node — it determines whether the incident is published to Kafka
or stored silently as a draft.

Sub-signals and their weights
------------------------------
  retrieval_score  (weight 0.35)
      How much relevant code context was retrieved.
      0.0  — no code_index rows found (repository not indexed, or crash_file
              did not match any indexed file)
      0.5  — partial context (stack frames found, crashed function not found)
      0.75 — crashed function found; helpers not available
      1.0  — crashed function + at least one stack frame or helper found

  coherence_score  (weight 0.45)
      Proxy for GPT-4 output quality.
      - If both nodes ran without fallback: uses root_cause_confidence from
        raw_analysis_output JSON if present, else defaults to 0.80.
      - If analyzer used fallback: 0.30 (playbook-only response, low reliability)
      - If fix_generator used fallback but analyzer succeeded: 0.65

  coverage_score   (weight 0.20)
      How complete is the data we gave the model.
      +0.4 if code_context is non-empty (code was retrieved)
      +0.3 if playbook matched (domain guidance available)
      +0.3 if stack_frames is non-empty
      Capped at 1.0

Composite formula
-----------------
  confidence = (
      retrieval_score  * 0.35 +
      coherence_score  * 0.45 +
      coverage_score   * 0.20
  )
  Rounded to 4 decimal places. Clamped to [0.0, 1.0].

Inputs consumed from AgentState
--------------------------------
  code_context, code_retriever_meta
  root_cause, raw_analysis_output
  analyzer_fallback_used, fix_fallback_used
  parsed_event.stack_frames
  matched_playbook_id

Outputs written to AgentState
------------------------------
  confidence_score, retrieval_score, coherence_score, scorer_latency_ms
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class ConfidenceScorerNode:
    """
    LangGraph node: ConfidenceScorer

    Stateless — safe to instantiate once at module level. Pure computation,
    no I/O.
    """

    async def invoke(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """
        Compute composite confidence score.

        Parameters
        ----------
        state : dict
            Full AgentState.

        Returns
        -------
        dict
            Partial AgentState update:
            {confidence_score, retrieval_score, coherence_score,
             scorer_latency_ms}
        """
        start: float = time.monotonic()

        parsed: Dict[str, Any] = state.get("parsed_event") or {}
        stack_frames = parsed.get("stack_frames") or []

        code_context: str = str(state.get("code_context") or "")
        code_meta: Dict[str, Any] = state.get("code_retriever_meta") or {}
        raw_analysis: str = str(state.get("raw_analysis_output") or "")
        analyzer_fallback: bool = bool(state.get("analyzer_fallback_used", False))
        fix_fallback: bool = bool(state.get("fix_fallback_used", False))
        matched_playbook_id: Optional[str] = state.get("matched_playbook_id")

        # ── Retrieval score ────────────────────────────────────────────────────
        symbols_retrieved: int = int(code_meta.get("symbols_retrieved") or 0)
        retrieval_score: float = _compute_retrieval_score(
            symbols_retrieved=symbols_retrieved,
            has_code_context=bool(code_context.strip()),
        )

        # ── Coherence score ───────────────────────────────────────────────────
        coherence_score: float = _compute_coherence_score(
            analyzer_fallback=analyzer_fallback,
            fix_fallback=fix_fallback,
            raw_analysis_output=raw_analysis,
        )

        # ── Coverage score ────────────────────────────────────────────────────
        coverage_score: float = _compute_coverage_score(
            has_code_context=bool(code_context.strip()),
            has_playbook=matched_playbook_id is not None,
            has_stack_frames=len(stack_frames) > 0,
        )

        # ── Composite score ───────────────────────────────────────────────────
        raw_score: float = (
            retrieval_score * 0.35
            + coherence_score * 0.45
            + coverage_score * 0.20
        )
        confidence_score: float = round(max(0.0, min(1.0, raw_score)), 4)

        latency_ms: int = int((time.monotonic() - start) * 1000)

        logger.info(
            "confidence_scorer_result",
            extra={
                "confidence_score": confidence_score,
                "retrieval_score": retrieval_score,
                "coherence_score": coherence_score,
                "coverage_score": coverage_score,
                "symbols_retrieved": symbols_retrieved,
                "analyzer_fallback": analyzer_fallback,
                "fix_fallback": fix_fallback,
            },
        )

        return {
            "confidence_score": confidence_score,
            "retrieval_score": retrieval_score,
            "coherence_score": coherence_score,
            "scorer_latency_ms": latency_ms,
        }


# ---------------------------------------------------------------------------
# Sub-signal computation helpers
# ---------------------------------------------------------------------------

def _compute_retrieval_score(
    symbols_retrieved: int,
    has_code_context: bool,
) -> float:
    """
    Score how much relevant code context the CodeRetriever assembled.

    0.0  — nothing retrieved (not indexed, or file not matched)
    0.5  — context present but only 1 symbol (crash function alone)
    0.75 — 2 symbols retrieved
    1.0  — 3+ symbols (crashed function + stack frames or helpers)
    """
    if not has_code_context or symbols_retrieved == 0:
        return 0.0
    if symbols_retrieved == 1:
        return 0.5
    if symbols_retrieved == 2:
        return 0.75
    return 1.0


def _compute_coherence_score(
    analyzer_fallback: bool,
    fix_fallback: bool,
    raw_analysis_output: str,
) -> float:
    """
    Proxy for GPT-4 output quality.

    Attempt to extract root_cause_confidence from the Analyzer's raw JSON
    output when both nodes succeeded. Fall back to heuristic values when
    one or both nodes used their fallback path.
    """
    if analyzer_fallback:
        # Analyzer fell back — playbook-only or generic text, low reliability
        return 0.30

    # Analyzer succeeded — try to read its self-reported confidence
    model_confidence: Optional[float] = None
    if raw_analysis_output:
        try:
            parsed_json = json.loads(raw_analysis_output)
            raw_conf = parsed_json.get("root_cause_confidence")
            if raw_conf is not None:
                model_confidence = max(0.0, min(1.0, float(raw_conf)))
        except (json.JSONDecodeError, TypeError, ValueError):
            pass

    base: float = model_confidence if model_confidence is not None else 0.80

    # Penalise slightly if fix generation also fell back
    if fix_fallback:
        base = max(0.0, base - 0.15)

    return round(base, 4)


def _compute_coverage_score(
    has_code_context: bool,
    has_playbook: bool,
    has_stack_frames: bool,
) -> float:
    """
    Score the completeness of data available to the model.

    Additive: code context = 0.4, playbook = 0.3, stack frames = 0.3
    Capped at 1.0.
    """
    score: float = 0.0
    if has_code_context:
        score += 0.4
    if has_playbook:
        score += 0.3
    if has_stack_frames:
        score += 0.3
    return min(1.0, round(score, 4))