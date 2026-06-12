"""
fastapi/app/agents/nodes/playbook_matcher.py

Playbook Matcher Node — Phase 4 LangGraph Agent Pipeline

Fetches all active playbook_snapshots for the tenant and finds the first
one whose trigger_pattern regex matches the incident's error_type,
error_message, or crash_method.

Matching strategy (tried in order, first match wins)
------------------------------------------------------
1. error_type   — exact or regex match against playbook.trigger_pattern
2. error_message — full-text regex search against playbook.trigger_pattern
3. crash_method  — regex search against playbook.trigger_pattern

The matched playbook's instructions are injected into the Analyzer node's
prompt, giving the LLM domain-specific remediation guidance before it
writes its root cause analysis. The fix_generator is not directly affected
by playbook instructions but can see them in the analysis context.

Playbook priority
-----------------
Playbooks are queried in ORDER BY priority DESC, is_active=True, so a
tenant can assign higher priority numbers to more specific playbooks.
The first matching playbook wins.

Result: matched_playbook_id, playbook_instructions, playbook_latency_ms

No match: both fields are None; the Analyzer proceeds without instructions.

DB query
--------
SELECT * FROM playbook_snapshots
WHERE tenant_id = $tenant_id
  AND is_active = TRUE
ORDER BY priority DESC;

(The full set is fetched once and pattern-matched in Python to avoid
 per-pattern round-trips. Playbook counts per tenant are expected to be
 small, typically < 50.)

Inputs consumed from AgentState
--------------------------------
  tenant_id
  parsed_event.error_type
  parsed_event.error_message
  parsed_event.crash_method
  session   (AsyncSession bound to DB-2)

Outputs written to AgentState
------------------------------
  matched_playbook_id   : str | None
  playbook_instructions : str | None
  playbook_latency_ms   : int
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class PlaybookMatcherNode:
    """
    LangGraph node: PlaybookMatcher

    Stateless — safe to instantiate once at module level.
    """

    async def invoke(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """
        Match the incident against tenant playbook patterns.

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
        parsed: Dict[str, Any] = state["parsed_event"]
        session = state["session"]
        tenant_id: str = state["tenant_id"]

        error_type: str = str(parsed.get("error_type") or "")
        error_message: str = str(parsed.get("error_message") or "")
        crash_method: str = str(parsed.get("crash_method") or "")

        matched_playbook_id: Optional[str] = None
        playbook_instructions: Optional[str] = None

        try:
            playbooks = await self._fetch_active_playbooks(session, tenant_id)

            for playbook in playbooks:
                trigger_pattern: str = str(
                    getattr(playbook, "trigger_pattern", "") or ""
                ).strip()

                if not trigger_pattern:
                    continue

                if self._matches(trigger_pattern, error_type, error_message, crash_method):
                    matched_playbook_id = str(playbook.rule_id)
                    playbook_instructions = str(
                        getattr(playbook, "instructions", "") or ""
                    )
                    logger.info(
                        "playbook_matched",
                        extra={
                            "tenant_id": tenant_id,
                            "playbook_id": matched_playbook_id,
                            "trigger_pattern": trigger_pattern[:100],
                            "error_type": error_type,
                        },
                    )
                    break  # First match wins

        except Exception as exc:
            # Playbook matching is best-effort; failure must not block analysis
            logger.warning(
                "playbook_matcher_error",
                extra={"tenant_id": tenant_id, "error": str(exc)},
            )

        if matched_playbook_id is None:
            logger.debug(
                "playbook_no_match",
                extra={
                    "tenant_id": tenant_id,
                    "error_type": error_type,
                },
            )

        latency_ms = int((time.monotonic() - start) * 1000)

        return {
            "matched_playbook_id": matched_playbook_id,
            "playbook_instructions": playbook_instructions,
            "playbook_latency_ms": latency_ms,
        }

    # ------------------------------------------------------------------
    # Database query
    # ------------------------------------------------------------------

    async def _fetch_active_playbooks(
        self,
        session: Any,
        tenant_id: str,
    ) -> List[Any]:
        """
        Fetch all active playbooks for the tenant, ordered by priority.

        Returns an empty list on any DB error to ensure the pipeline
        continues without playbook guidance rather than failing entirely.
        """
        import uuid as _uuid_module

        from sqlalchemy import and_
        from sqlalchemy.future import select

        from app.models.snapshots import PlaybookSnapshot

        try:
            tenant_uuid = _uuid_module.UUID(tenant_id)
        except (ValueError, AttributeError):
            logger.warning(
                "playbook_matcher_invalid_tenant_uuid",
                extra={"tenant_id": tenant_id},
            )
            return []

        try:
            stmt = (
                select(PlaybookSnapshot)
                .where(
                    and_(
                        PlaybookSnapshot.tenant_id == tenant_uuid,
                        PlaybookSnapshot.is_active.is_(True),
                    )
                )
                .order_by(PlaybookSnapshot.priority.desc())
            )
            result = await session.execute(stmt)
            return list(result.scalars().all())

        except Exception as exc:
            logger.warning(
                "playbook_matcher_db_error",
                extra={"tenant_id": tenant_id, "error": str(exc)},
            )
            return []

    # ------------------------------------------------------------------
    # Pattern matching
    # ------------------------------------------------------------------

    def _matches(
        self,
        pattern: str,
        error_type: str,
        error_message: str,
        crash_method: str,
    ) -> bool:
        """
        Check if the trigger_pattern matches any of the three candidate strings.

        The pattern is treated as a Python regex. If it is not a valid
        regex, it falls back to a simple substring check. This ensures
        that playbooks with plain-text trigger_pattern values still work.

        Matching is case-insensitive for all three candidates.

        Returns True on the first candidate that matches.
        """
        try:
            compiled = re.compile(pattern, re.IGNORECASE)
            use_regex = True
        except re.error:
            use_regex = False

        candidates = [error_type, error_message, crash_method]

        for candidate in candidates:
            if not candidate:
                continue
            if use_regex:
                if compiled.search(candidate):
                    return True
            else:
                # Plain substring match as fallback
                if pattern.lower() in candidate.lower():
                    return True

        return False