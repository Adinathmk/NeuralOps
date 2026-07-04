"""
fastapi/app/agents/workflow.py

LangGraph Agent Workflow — NeuralOps AI Pipeline

Defines the AgentState TypedDict and wires all nodes into a compiled
StateGraph. The workflow is built once per process lifetime via
get_agent_workflow() (module-level singleton).

Node execution order (DAG)
--------------------------
  classifier
      ↓ (conditional: not actionable → END)
  code_retriever
      ↓
  playbook_matcher
      ↓
  analyzer
      ↓
  fix_generator
      ↓
  confidence_scorer
      ↓
  action_decision
      ↓ (conditional: create_incident → patch_generator, store_draft → END)
  patch_generator
      ↓
  END
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from langgraph.graph import END, StateGraph
from typing_extensions import TypedDict

from app.agents.nodes.action_decision import ActionDecisionNode
from app.agents.nodes.analyzer import AnalyzerNode
from app.agents.nodes.classifier import ClassifierNode
from app.agents.nodes.code_retriever import CodeRetrieverNode
from app.agents.nodes.confidence_scorer import ConfidenceScorerNode
from app.agents.nodes.fix_generator import FixGeneratorNode
from app.agents.nodes.patch_generator import PatchGeneratorNode
from app.agents.nodes.playbook_matcher import PlaybookMatcherNode

# ---------------------------------------------------------------------------
# Shared state schema
# ---------------------------------------------------------------------------


class AgentState(TypedDict, total=False):
    """
    Shared mutable state passed between all agent nodes.

    TypedDict with total=False so nodes can return partial dicts without
    providing every key — LangGraph merges partial updates into the
    accumulated state dict.
    """

    # ── Pipeline inputs (set by run_agent before ainvoke) ──────────────────
    tenant_id: str
    parsed_event: Dict[str, Any]
    fingerprint: str
    session: Any  # sqlalchemy.ext.asyncio.AsyncSession
    redis: Any  # redis.asyncio.Redis

    # ── Classifier outputs ──────────────────────────────────────────────────
    severity: str  # critical | high | medium | low | unknown
    actionable: bool
    classifier_latency_ms: int
    error_category: str  # code_bug | database | infra_config | external_dependency | security | unknown

    # ── CodeRetriever outputs ───────────────────────────────────────────────
    code_context: str  # assembled and token-capped snippets
    code_retriever_meta: Dict[str, Any]

    # ── PlaybookMatcher outputs ─────────────────────────────────────────────
    matched_playbook_id: Optional[str]
    playbook_instructions: Optional[str]
    playbook_latency_ms: int

    # ── Analyzer outputs ────────────────────────────────────────────────────
    root_cause: str
    root_cause_confidence: float
    raw_analysis_output: str
    analyzer_latency_ms: int
    analyzer_fallback_used: bool
    analyzer_tokens: Dict[str, int]  # prompt, completion, total

    # ── FixGenerator outputs ────────────────────────────────────────────────
    suggested_fix: str
    raw_fix_output: str
    fix_generator_latency_ms: int
    fix_fallback_used: bool
    fix_tokens: Dict[str, int]  # prompt, completion, total
    code_patch: str
    fix_confidence: float
    fix_complexity: str  # trivial | minor | moderate | major
    fix_target_file: str  # POSIX-relative path of the file that needs the patch

    # ── ConfidenceScorer outputs ────────────────────────────────────────────
    confidence_score: float  # composite score in [0.0, 1.0]
    retrieval_score: float
    coherence_score: float
    scorer_latency_ms: int

    # ── ActionDecision outputs ──────────────────────────────────────────────
    action: str  # "create_incident" | "store_draft"
    confidence_threshold: float

    # ── PatchGenerator outputs ──────────────────────────────────────────────
    structured_patch: str       # JSON string of validated patches, or ""
    patch_confidence: float
    patch_skip_reason: str
    patch_generator_latency_ms: int

    # ── Set by run_agent after ainvoke() ───────────────────────────────────
    total_latency_ms: int


# ---------------------------------------------------------------------------
# Conditional routing helpers
# ---------------------------------------------------------------------------


def _route_after_classifier(state: AgentState) -> str:
    """
    Route to code_retriever if actionable, else END.
    """
    if not state.get("actionable", True):
        return END
    return "code_retriever"


# Error categories for which the patch_generator is allowed to run.
# Infra, external-dependency, security, and unknown incidents are never
# auto-patched — they either require ops intervention or human triage.
_PATCHABLE_CATEGORIES: frozenset[str] = frozenset({"code_bug", "database"})


def _route_after_action_decision(state: AgentState) -> str:
    """
    Route to patch_generator only when the agent decided to create a full
    incident AND the error category is one we can safely auto-patch.

    Incident creation and patch generation are intentionally decoupled:
      - "create_incident" controls whether the incident record is promoted
        to open status in the database.
      - Routing to patch_generator is additionally gated on error_category
        so that infra / external-dependency / security / unknown incidents
        are never sent to the patch generator, regardless of confidence.
    """
    if (
        state.get("action") == "create_incident"
        and state.get("error_category") in _PATCHABLE_CATEGORIES
    ):
        return "patch_generator"
    return END


# ---------------------------------------------------------------------------
# Workflow factory
# ---------------------------------------------------------------------------


def build_agent_workflow():
    """
    Build and compile the LangGraph StateGraph.

    Returns a compiled graph safe to cache at module level and reused
    across multiple ainvoke() calls (nodes are all stateless).
    """
    # Instantiate nodes (all stateless — safe to share)
    classifier_node = ClassifierNode()
    code_retriever_node = CodeRetrieverNode()
    playbook_matcher_node = PlaybookMatcherNode()
    analyzer_node = AnalyzerNode()
    fix_generator_node = FixGeneratorNode()
    confidence_scorer_node = ConfidenceScorerNode()
    action_decision_node = ActionDecisionNode()
    patch_generator_node = PatchGeneratorNode()

    graph: StateGraph = StateGraph(AgentState)

    # ── Register nodes ─────────────────────────────────────────────────────
    graph.add_node("classifier", classifier_node.invoke)
    graph.add_node("code_retriever", code_retriever_node.invoke)
    graph.add_node("playbook_matcher", playbook_matcher_node.invoke)
    graph.add_node("analyzer", analyzer_node.invoke)
    graph.add_node("fix_generator", fix_generator_node.invoke)
    graph.add_node("confidence_scorer", confidence_scorer_node.invoke)
    graph.add_node("action_decision", action_decision_node.invoke)
    graph.add_node("patch_generator", patch_generator_node.invoke)

    # ── Entry point ────────────────────────────────────────────────────────
    graph.set_entry_point("classifier")

    # ── Conditional edge after classifier ──────────────────────────────────
    graph.add_conditional_edges(
        "classifier",
        _route_after_classifier,
        {
            "code_retriever": "code_retriever",
            END: END,
        },
    )

    # ── Linear edges through the analysis pipeline ─────────────────────────
    graph.add_edge("code_retriever", "playbook_matcher")
    graph.add_edge("playbook_matcher", "analyzer")
    graph.add_edge("analyzer", "fix_generator")
    graph.add_edge("fix_generator", "confidence_scorer")
    graph.add_edge("confidence_scorer", "action_decision")

    # ── Conditional edge after action_decision ──────────────────────────────
    # create_incident → patch_generator → END
    # store_draft     → END  (bypass patch generation entirely)
    graph.add_conditional_edges(
        "action_decision",
        _route_after_action_decision,
        {
            "patch_generator": "patch_generator",
            END: END,
        },
    )

    graph.add_edge("patch_generator", END)

    return graph.compile()


# ---------------------------------------------------------------------------
# Module-level cached workflow
# ---------------------------------------------------------------------------

_cached_workflow = None


def get_agent_workflow():
    """
    Return the module-level cached compiled workflow.
    Builds on first call; subsequent calls return the cached instance.

    Thread safety: duplicate builds are harmless — the compiled graph is
    stateless and the GIL ensures a single assignment to _cached_workflow.
    """
    global _cached_workflow
    if _cached_workflow is None:
        _cached_workflow = build_agent_workflow()
    return _cached_workflow


def reset_agent_workflow() -> None:
    """
    Clear the cached workflow so the next call to get_agent_workflow()
    rebuilds it from scratch.

    Call this at worker process startup after a deployment to ensure
    the new node definitions (including patch_generator) are loaded.
    """
    global _cached_workflow
    _cached_workflow = None