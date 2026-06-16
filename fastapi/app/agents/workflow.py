"""
fastapi/app/agents/workflow.py

LangGraph Agent Workflow — Phase 4 NeuralOps AI Pipeline

Defines the AgentState TypedDict and wires all seven nodes into a compiled
StateGraph. The workflow is built once per run_agent task execution via
build_agent_workflow().

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
      ↓
  END

All nodes are async. LangGraph calls each node's invoke() coroutine
and merges the returned partial dict into the shared AgentState.

AgentState field ownership
---------------------------
  Inputs (set by run_agent before ainvoke):
    tenant_id, parsed_event, fingerprint, session, redis

  classifier        → severity, actionable, classifier_latency_ms
  code_retriever    → code_context, code_retriever_meta
  playbook_matcher  → matched_playbook_id, playbook_instructions,
                       playbook_latency_ms
  analyzer          → root_cause, raw_analysis_output,
                       analyzer_latency_ms, analyzer_fallback_used,
                       analyzer_tokens
  fix_generator     → suggested_fix, raw_fix_output,
                       fix_generator_latency_ms, fix_fallback_used,
                       fix_tokens
  confidence_scorer → confidence_score, retrieval_score,
                       coherence_score, scorer_latency_ms
  action_decision   → action, confidence_threshold

  total_latency_ms is set by run_agent AFTER ainvoke() returns.
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

    # ── CodeRetriever outputs ───────────────────────────────────────────────
    code_context: str  # assembled and token-capped snippets
    code_retriever_meta: Dict[str, Any]
    # keys: files_fetched, tokens, cache_hits, cache_misses,
    #       symbols_retrieved, latency_ms

    # ── PlaybookMatcher outputs ─────────────────────────────────────────────
    matched_playbook_id: Optional[str]
    playbook_instructions: Optional[str]
    playbook_latency_ms: int

    # ── Analyzer outputs ────────────────────────────────────────────────────
    root_cause: str
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

    # ── ConfidenceScorer outputs ────────────────────────────────────────────
    confidence_score: float  # composite score in [0.0, 1.0]
    retrieval_score: float
    coherence_score: float
    scorer_latency_ms: int

    # ── ActionDecision outputs ──────────────────────────────────────────────
    action: str  # "create_incident" | "store_draft"
    confidence_threshold: float

    # ── Set by run_agent after ainvoke() ───────────────────────────────────
    total_latency_ms: int


# ---------------------------------------------------------------------------
# Conditional routing
# ---------------------------------------------------------------------------


def _route_after_classifier(state: AgentState) -> str:
    """
    Conditional edge fired after the Classifier node.

    If the log event is not actionable (e.g. an INFO or DEBUG entry),
    route directly to END to skip all LLM calls and DB writes.
    Otherwise proceed to the CodeRetriever.
    """
    if not state.get("actionable", True):
        return END
    return "code_retriever"


# ---------------------------------------------------------------------------
# Workflow factory
# ---------------------------------------------------------------------------


def build_agent_workflow():
    """
    Build and compile the LangGraph StateGraph for the Phase 4 agent.

    Returns a compiled graph that can be called with:
        result_state = await graph.ainvoke(initial_state_dict)

    The compiled graph is stateless — it can be cached at module level
    and reused across multiple ainvoke() calls safely.

    Node instances are also stateless (no instance variables mutated
    during invoke), so a single ClassifierNode() etc. can be shared.
    """
    # Instantiate nodes once — they are all stateless
    classifier_node = ClassifierNode()
    code_retriever_node = CodeRetrieverNode()
    playbook_matcher_node = PlaybookMatcherNode()
    analyzer_node = AnalyzerNode()
    fix_generator_node = FixGeneratorNode()
    confidence_scorer_node = ConfidenceScorerNode()
    action_decision_node = ActionDecisionNode()

    graph: StateGraph = StateGraph(AgentState)

    # ── Register nodes ─────────────────────────────────────────────────────
    graph.add_node("classifier", classifier_node.invoke)
    graph.add_node("code_retriever", code_retriever_node.invoke)
    graph.add_node("playbook_matcher", playbook_matcher_node.invoke)
    graph.add_node("analyzer", analyzer_node.invoke)
    graph.add_node("fix_generator", fix_generator_node.invoke)
    graph.add_node("confidence_scorer", confidence_scorer_node.invoke)
    graph.add_node("action_decision", action_decision_node.invoke)

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

    # ── Linear edges for the remaining nodes ───────────────────────────────
    graph.add_edge("code_retriever", "playbook_matcher")
    graph.add_edge("playbook_matcher", "analyzer")
    graph.add_edge("analyzer", "fix_generator")
    graph.add_edge("fix_generator", "confidence_scorer")
    graph.add_edge("confidence_scorer", "action_decision")
    graph.add_edge("action_decision", END)

    return graph.compile()


# ---------------------------------------------------------------------------
# Module-level cached workflow (optional optimisation)
# ---------------------------------------------------------------------------
# Call get_agent_workflow() instead of build_agent_workflow() in hot paths
# to avoid rebuilding the graph on every task execution.

_cached_workflow = None


def get_agent_workflow():
    """
    Return the module-level cached compiled workflow.
    Builds it on first call; subsequent calls return the cached instance.

    Thread-safe for read access after first build. The build itself is
    not protected by a lock, but duplicate builds are harmless — the
    compiled graph is stateless and the GIL ensures only one assignment
    to _cached_workflow at a time.
    """
    global _cached_workflow
    if _cached_workflow is None:
        _cached_workflow = build_agent_workflow()
    return _cached_workflow
