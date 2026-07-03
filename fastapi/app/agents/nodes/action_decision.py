"""
fastapi/app/agents/nodes/action_decision.py

Action Decision Node — Phase 4 LangGraph Agent Pipeline

The final decision node before patch generation. Compares confidence_score
against the tenant's configured confidence_threshold (read from
alert_rule_snapshots or a default of 0.70) and sets the action field.

Additionally applies a hard category override: security incidents always
require human review and are forced to store_draft regardless of confidence.

Decision logic (evaluated in order)
--------------------------------------
  error_category == "security"   →  action = "store_draft"  (hard block)
  confidence_score >= threshold  →  action = "create_incident"
  confidence_score <  threshold  →  action = "store_draft"

Threshold source (in priority order)
-------------------------------------
  1. The tenant's minimum confidence_threshold from alert_rule_snapshots
     (lowest threshold across all active alert rules — the most permissive
     rule wins, so we match as many actionable incidents as possible).
  2. If no alert rules are configured: DEFAULT_THRESHOLD = 0.70.

No LLM call is made in this node. It is pure logic.

Inputs consumed from AgentState
--------------------------------
  tenant_id
  confidence_score
  error_category   (code_bug | database | infra_config | external_dependency | security | unknown)
  session  (AsyncSession — used to read alert_rule_snapshots)

Outputs written to AgentState
------------------------------
  action               : "create_incident" | "store_draft"
  confidence_threshold : float  (the threshold that was applied)
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Default confidence threshold applied when no alert rule is configured.
DEFAULT_THRESHOLD: float = 0.70


class ActionDecisionNode:
    """
    LangGraph node: ActionDecision

    Stateless — safe to instantiate once at module level.
    """

    async def invoke(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """
        Decide whether to create an incident or store a draft.

        Parameters
        ----------
        state : dict
            Full AgentState.

        Returns
        -------
        dict
            Partial AgentState update:
            {action, confidence_threshold}
        """
        start: float = time.monotonic()

        confidence_score: float = float(state.get("confidence_score") or 0.0)
        tenant_id: str = str(state.get("tenant_id") or "")
        error_category: str = str(state.get("error_category") or "unknown")
        session = state.get("session")

        threshold: float = await _fetch_tenant_threshold(session, tenant_id)

        if error_category == "security":
            # Security incidents always require human review before promotion,
            # regardless of model confidence.
            action = "store_draft"
        elif confidence_score >= threshold:
            action = "create_incident"
        else:
            action = "store_draft"

        latency_ms = int((time.monotonic() - start) * 1000)

        logger.info(
            "action_decision_result",
            extra={
                "action": action,
                "confidence_score": confidence_score,
                "threshold": threshold,
                "error_category": error_category,
                "tenant_id": tenant_id,
                "latency_ms": latency_ms,
            },
        )

        return {
            "action": action,
            "confidence_threshold": threshold,
        }


# ---------------------------------------------------------------------------
# Threshold fetch helper
# ---------------------------------------------------------------------------


async def _fetch_tenant_threshold(
    session: Any,
    tenant_id: str,
) -> float:
    """
    Read the minimum confidence_threshold from the tenant's active alert rules.

    We take the MINIMUM threshold across all active rules (most permissive)
    so that an incident cleared by any rule is promoted to open status.

    Returns DEFAULT_THRESHOLD (0.70) on any error or when no rules exist.
    """
    if session is None or not tenant_id:
        return DEFAULT_THRESHOLD

    import uuid as _uuid_module

    from sqlalchemy import and_, func
    from sqlalchemy.future import select

    from app.models.snapshots import AlertRuleSnapshot

    try:
        tenant_uuid = _uuid_module.UUID(tenant_id)
    except (ValueError, AttributeError):
        return DEFAULT_THRESHOLD

    try:
        stmt = select(func.min(AlertRuleSnapshot.confidence_threshold)).where(
            and_(
                AlertRuleSnapshot.tenant_id == tenant_uuid,
                AlertRuleSnapshot.enabled.is_(True),
                AlertRuleSnapshot.confidence_threshold.is_not(None),
            )
        )
        result = await session.execute(stmt)
        min_threshold = result.scalar_one_or_none()

        if min_threshold is None:
            return DEFAULT_THRESHOLD

        threshold = float(min_threshold)
        # Guard against misconfigured thresholds outside [0, 1]
        return max(0.0, min(1.0, threshold))

    except Exception as exc:
        logger.warning(
            "action_decision_threshold_fetch_failed",
            extra={"tenant_id": tenant_id, "error": str(exc)},
        )
        return DEFAULT_THRESHOLD
