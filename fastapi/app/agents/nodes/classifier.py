"""
fastapi/app/agents/nodes/classifier.py

Classifier Node — Phase 4 LangGraph Agent Pipeline

Determines whether a parsed log event represents an actionable production
incident and classifies its severity level.

This node runs FIRST in the pipeline. A non-actionable result causes
the conditional edge in workflow.py to route directly to END, bypassing
all LLM calls and saving 100% of downstream costs for noise events.

Classification rules (evaluated in precedence order)
-----------------------------------------------------
1. Log level is 'debug' or 'info'
   → not actionable; return immediately (no LLM cost)

2. error_type matches a known CRITICAL pattern OR log level is 'critical'
   → severity = critical, actionable = True

3. error_type matches a known HIGH pattern OR log level is 'error'
   → severity = high, actionable = True

4. error_type matches a known MEDIUM pattern
   → severity = medium, actionable = True

5. Log level is 'warning' without a known error type
   → severity = low, actionable = True

6. All others
   → severity = low, actionable = True
   (Unclassified errors proceed to the agent; the Analyzer node can refine severity)

Inputs consumed from AgentState
--------------------------------
  parsed_event.severity   : raw severity string from the SDK log entry
  parsed_event.error_type : extracted exception class name

Outputs written to AgentState
------------------------------
  severity              : str — one of critical | high | medium | low
  actionable            : bool
  error_category        : str — one of code_bug | database | infra_config |
                               external_dependency | security | unknown
  classifier_latency_ms : int
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Classification lookup tables
# ---------------------------------------------------------------------------

# Error types that always map to critical severity regardless of log level.
# These represent process-terminating or data-loss events.
_CRITICAL_ERROR_TYPES: frozenset[str] = frozenset(
    {
        "OutOfMemoryError",
        "StackOverflowError",
        "SystemExit",
        "FatalError",
        "PanicError",
        "KernelPanic",
        "OutOfMemoryException",
        "MemoryError",
        "SystemError",
        "AssertionError",  # Python: only raised by assert; indicates invariant violation
        "RuntimePanic",
        "UnrecoverableError",
    }
)

# Error types that map to high severity.
# These represent data integrity or availability failures.
_HIGH_ERROR_TYPES: frozenset[str] = frozenset(
    {
        "NullPointerException",
        "NullReferenceException",
        "DatabaseError",
        "OperationalError",
        "ConnectionRefusedError",
        "DeadlockError",
        "TransactionError",
        "IntegrityError",
        "DataCorruptionError",
        "PaymentError",
        "AuthenticationError",
        "AuthorizationError",
        "PermissionError",
        "TimeoutError",
        "ReadTimeoutError",
        "ConnectTimeoutError",
        "ServiceUnavailableError",
        "CircuitBreakerOpenError",
        "NullReferenceError",
        "AccessViolation",
        "SegmentationFault",
        "IOException",
    }
)

# Error types that map to medium severity.
# These represent programmer errors that are typically recoverable.
_MEDIUM_ERROR_TYPES: frozenset[str] = frozenset(
    {
        "ValueError",
        "KeyError",
        "IndexError",
        "AttributeError",
        "TypeError",
        "NotImplementedError",
        "InvalidArgumentError",
        "BadRequestError",
        "ValidationError",
        "SerializationError",
        "DeserializationError",
        "ParseError",
        "FormatError",
        "IllegalArgumentException",
        "IllegalStateException",
        "UnsupportedOperationException",
        "ClassCastException",
        "NumberFormatException",
        "StringIndexOutOfBoundsException",
        "ArrayIndexOutOfBoundsException",
    }
)

# Raw severity strings that are considered non-actionable.
_NON_ACTIONABLE_LEVELS: frozenset[str] = frozenset(
    {
        "debug",
        "trace",
        "info",
        "information",
        "verbose",
    }
)


# ---------------------------------------------------------------------------
# Category lookup tables (purely additive — do not affect severity logic)
# ---------------------------------------------------------------------------

# Programmer / logic errors that can realistically be auto-patched.
_CODE_BUG_ERROR_TYPES: frozenset[str] = frozenset(
    _MEDIUM_ERROR_TYPES
    | {
        "NullPointerException",
        "NullReferenceException",
        "NullReferenceError",
        "AttributeError",
        "AssertionError",
        "ZeroDivisionError",
    }
)

# Database-layer errors — safe to create an incident but patch is rarely a
# simple code change; keep as a separate bucket so routing can decide.
_DATABASE_ERROR_TYPES: frozenset[str] = frozenset(
    {
        "DatabaseError",
        "OperationalError",
        "IntegrityError",
        "TransactionError",
        "DeadlockError",
        "DataCorruptionError",
    }
)

# Infrastructure / memory errors — require ops intervention, not code patches.
_INFRA_ERROR_TYPES: frozenset[str] = frozenset(
    {
        "OutOfMemoryError",
        "MemoryError",
        "SystemError",
        "SystemExit",
        "OutOfMemoryException",
        "KernelPanic",
        "RuntimePanic",
    }
)

# External service / network errors — flaky third-party; patching source code
# rarely helps.
_EXTERNAL_DEPENDENCY_ERROR_TYPES: frozenset[str] = frozenset(
    {
        "TimeoutError",
        "ReadTimeoutError",
        "ConnectTimeoutError",
        "ConnectionRefusedError",
        "ServiceUnavailableError",
        "CircuitBreakerOpenError",
    }
)

# Security errors — must always require human triage regardless of confidence.
_SECURITY_ERROR_TYPES: frozenset[str] = frozenset(
    {
        "AuthenticationError",
        "AuthorizationError",
        "PermissionError",
    }
)


def _classify_category(error_type: str) -> str:
    """
    Return a best-effort error category for the given error_type string.

    Evaluated in security-first precedence order so that an error type
    that appears in multiple tables is always assigned the highest-risk
    category.  This is a cheap lookup-table operation — no LLM call.

    The Analyzer node may later refine this via its root-cause output,
    but that is backlog; for now the category is fixed at classifier time.

    Returns one of:
        "security" | "database" | "infra_config" |
        "external_dependency" | "code_bug" | "unknown"
    """
    if error_type in _SECURITY_ERROR_TYPES:
        return "security"
    if error_type in _DATABASE_ERROR_TYPES:
        return "database"
    if error_type in _INFRA_ERROR_TYPES:
        return "infra_config"
    if error_type in _EXTERNAL_DEPENDENCY_ERROR_TYPES:
        return "external_dependency"
    if error_type in _CODE_BUG_ERROR_TYPES:
        return "code_bug"
    return "unknown"


class ClassifierNode:
    """
    LangGraph node: Classifier

    Stateless — can be instantiated once and shared across invocations.
    All inputs are read from the AgentState dict; outputs are returned
    as a partial state update dict.
    """

    async def invoke(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """
        Classify the parsed log event.

        Parameters
        ----------
        state : dict
            Full AgentState. Reads: parsed_event.severity, parsed_event.error_type

        Returns
        -------
        dict
            Partial AgentState update:
            {severity, actionable, error_category, classifier_latency_ms}
        """
        start: float = time.monotonic()
        parsed: Dict[str, Any] = state["parsed_event"]

        raw_severity: str = str(parsed.get("severity") or "low").lower().strip()
        error_type: str = str(parsed.get("error_type") or "").strip()
        service_name: str = str(parsed.get("service_name") or "")
        environment: str = str(parsed.get("environment") or "")

        # ── Category (computed once, reused in both return paths) ─────────────
        error_category: str = _classify_category(error_type)

        # ── Rule 1: Non-actionable log levels ─────────────────────────────────
        if raw_severity in _NON_ACTIONABLE_LEVELS:
            latency_ms = int((time.monotonic() - start) * 1000)
            logger.info(
                "classifier_not_actionable",
                extra={
                    "reason": "non_actionable_level",
                    "raw_severity": raw_severity,
                    "error_type": error_type,
                    "error_category": error_category,
                    "service_name": service_name,
                    "environment": environment,
                    "latency_ms": latency_ms,
                },
            )
            return {
                "actionable": False,
                "severity": raw_severity,
                "error_category": error_category,
                "classifier_latency_ms": latency_ms,
            }

        # ── Rule 2: Critical ──────────────────────────────────────────────────
        if error_type in _CRITICAL_ERROR_TYPES or raw_severity == "critical":
            severity = "critical"

        # ── Rule 3: High ──────────────────────────────────────────────────────
        elif error_type in _HIGH_ERROR_TYPES or raw_severity == "error":
            severity = "high"

        # ── Rule 4: Medium ────────────────────────────────────────────────────
        elif error_type in _MEDIUM_ERROR_TYPES:
            severity = "medium"

        # ── Rule 5: Warning → low ─────────────────────────────────────────────
        elif raw_severity in ("warning", "warn"):
            severity = "low"

        # ── Rule 6: Fallback to low ───────────────────────────────────────────
        else:
            severity = "low"

        latency_ms = int((time.monotonic() - start) * 1000)

        logger.info(
            "classifier_result",
            extra={
                "actionable": True,
                "severity": severity,
                "error_category": error_category,
                "raw_severity": raw_severity,
                "error_type": error_type,
                "service_name": service_name,
                "environment": environment,
                "latency_ms": latency_ms,
            },
        )

        return {
            "actionable": True,
            "severity": severity,
            "error_category": error_category,
            "classifier_latency_ms": latency_ms,
        }
