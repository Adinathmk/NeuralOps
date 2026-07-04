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

Coverage
--------
Error-type tables intentionally span multiple languages/runtimes since
tenants report from heterogeneous stacks (Python, JVM/Java/Kotlin, .NET,
Node/JS/TS, Go, Ruby, PHP, Rust) plus common infra/network/DB errors.
An error_type that isn't in any table still gets a severity via the raw
log level (Rule 2/3/5/6) and error_category falls through to "unknown" —
nothing is ever dropped, coverage gaps just lose the free severity/category
bump and fall back to raw-level-based classification.

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
from langsmith import traceable

from app.agents.trace_utils import strip_node_state

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Classification lookup tables
# ---------------------------------------------------------------------------

# Error types that always map to critical severity regardless of log level.
# These represent process-terminating, data-loss, or total-outage events.
_CRITICAL_ERROR_TYPES: frozenset[str] = frozenset(
    {
        # ── Memory / process termination ─────────────────────────────────
        "OutOfMemoryError",
        "OutOfMemoryException",
        "MemoryError",
        "StackOverflowError",
        "StackOverflowException",
        "SystemExit",
        "SystemError",
        "FatalError",
        "FatalExecutionEngineError",
        "PanicError",
        "KernelPanic",
        "RuntimePanic",
        "UnrecoverableError",
        "AssertionError",  # Python: only raised by assert; indicates invariant violation
        "Abort",
        "SIGSEGV",
        "SIGABRT",
        "SIGKILL",
        "CoreDumpError",

        # ── JVM / Java / Kotlin ──────────────────────────────────────────
        "InternalError",
        "VirtualMachineError",
        "LinkageError",
        "NoClassDefFoundError",
        "ThreadDeath",

        # ── .NET / C# ─────────────────────────────────────────────────────
        "AccessViolationException",
        "AppDomainUnloadedException",
        "ExecutionEngineException",
        "BadImageFormatException",

        # ── Go ────────────────────────────────────────────────────────────
        "GoPanic",
        "FatalGoroutineError",

        # ── Node / JS / TS ────────────────────────────────────────────────
        "RangeError",  # frequently a stack-overflow / max call stack indicator
        "UnhandledPromiseRejection",
        "FatalProcessOutOfMemoryError",

        # ── Rust ──────────────────────────────────────────────────────────
        "RustPanic",
        "UnwrapNoneError",

        # ── Data loss / corruption at the platform level ────────────────
        "DiskFullError",
        "DataLossError",
        "IrrecoverableStateError",
    }
)

# Error types that map to high severity.
# These represent data integrity, availability, security, or payment
# failures — serious but not process-terminating.
_HIGH_ERROR_TYPES: frozenset[str] = frozenset(
    {
        # ── Null / reference errors across languages ─────────────────────
        "NullPointerException",
        "NullReferenceException",
        "NullReferenceError",
        "NoneTypeError",
        "NullError",

        # ── Database / persistence ────────────────────────────────────────
        "DatabaseError",
        "OperationalError",
        "ConnectionRefusedError",
        "DeadlockError",
        "TransactionError",
        "IntegrityError",
        "DataCorruptionError",
        "PoolExhaustedError",
        "TooManyConnectionsError",
        "ReplicationLagError",
        "QueryTimeoutError",
        "LockWaitTimeoutError",
        "ConstraintViolationError",
        "ForeignKeyViolationError",
        "UniqueViolationError",
        "DuplicateKeyError",  # Mongo/SQL duplicate key
        "MongoServerError",
        "RedisConnectionError",
        "RedisTimeoutError",

        # ── Auth / payments / security-adjacent availability ─────────────
        "PaymentError",
        "PaymentDeclinedError",
        "AuthenticationError",
        "AuthorizationError",
        "PermissionError",
        "AccessDeniedException",
        "ForbiddenError",
        "UnauthorizedError",
        "TokenExpiredError",
        "InvalidTokenError",
        "JWTError",
        "JWTExpiredError",

        # ── Timeouts / network / external dependency failures ────────────
        "TimeoutError",
        "ReadTimeoutError",
        "ConnectTimeoutError",
        "WriteTimeoutError",
        "GatewayTimeoutError",
        "ServiceUnavailableError",
        "CircuitBreakerOpenError",
        "DNSResolutionError",
        "SSLCertificateError",
        "SSLHandshakeError",
        "ConnectionResetError",
        "ConnectionAbortedError",
        "BrokenPipeError",
        "SocketTimeoutError",
        "TooManyRequestsError",
        "RateLimitExceededError",

        # ── OS / process-level access failures ────────────────────────────
        "AccessViolation",
        "SegmentationFault",
        "IOException",
        "DiskIOError",
        "FileSystemError",
        "OSError",

        # ── Java/Kotlin equivalents ────────────────────────────────────────
        "SQLException",
        "DataAccessException",
        "CannotAcquireLockException",
        "OptimisticLockException",
        "ConcurrentModificationException",

        # ── Node/JS equivalents ────────────────────────────────────────────
        "ECONNREFUSED",
        "ECONNRESET",
        "ETIMEDOUT",
        "EPIPE",
        "EADDRINUSE",

        # ── Message queue / streaming ───────────────────────────────────────
        "KafkaTimeoutError",
        "MessageBrokerUnavailableError",
        "QueueFullError",
        "ConsumerGroupRebalanceError",
    }
)

# Error types that map to medium severity.
# These represent programmer errors that are typically recoverable and
# fixable via a straightforward code change.
_MEDIUM_ERROR_TYPES: frozenset[str] = frozenset(
    {
        # ── Python builtins ────────────────────────────────────────────────
        "ValueError",
        "KeyError",
        "IndexError",
        "AttributeError",
        "TypeError",
        "NotImplementedError",
        "ZeroDivisionError",
        "StopIteration",
        "RecursionError",
        "UnboundLocalError",
        "NameError",
        "ImportError",
        "ModuleNotFoundError",
        "LookupError",
        "OverflowError",
        "FileNotFoundError",
        "FileExistsError",
        "IsADirectoryError",
        "NotADirectoryError",

        # ── Generic validation / parsing / serialization ────────────────
        "InvalidArgumentError",
        "BadRequestError",
        "ValidationError",
        "SerializationError",
        "DeserializationError",
        "ParseError",
        "FormatError",
        "SchemaValidationError",
        "TypeValidationError",
        "MissingFieldError",
        "UnexpectedFieldError",
        "EncodingError",
        "DecodingError",
        "JSONDecodeError",
        "XMLParseError",
        "YAMLParseError",

        # ── Java / Kotlin ───────────────────────────────────────────────────
        "IllegalArgumentException",
        "IllegalStateException",
        "UnsupportedOperationException",
        "ClassCastException",
        "NumberFormatException",
        "StringIndexOutOfBoundsException",
        "ArrayIndexOutOfBoundsException",
        "IndexOutOfBoundsException",
        "NoSuchElementException",
        "NoSuchMethodException",
        "NoSuchFieldException",
        "ClassNotFoundException",
        "NegativeArraySizeException",
        "ArithmeticException",
        "EmptyStackException",

        # ── .NET / C# ─────────────────────────────────────────────────────
        "ArgumentException",
        "ArgumentNullException",
        "ArgumentOutOfRangeException",
        "InvalidOperationException",
        "InvalidCastException",
        "FormatException",
        "IndexOutOfRangeException",
        "KeyNotFoundException",
        "NotSupportedException",
        "DivideByZeroException",

        # ── Node / JS / TS ────────────────────────────────────────────────
        "SyntaxError",
        "ReferenceError",
        "TypeMismatchError",
        "ZodValidationError",
        "JoiValidationError",

        # ── Go ────────────────────────────────────────────────────────────
        "NilPointerDereference",
        "IndexOutOfRange",
        "TypeAssertionError",

        # ── Ruby ──────────────────────────────────────────────────────────
        "NoMethodError",
        "ArgumentError",
        "RuntimeError",
        "FrozenError",

        # ── PHP ───────────────────────────────────────────────────────────
        "UndefinedIndexError",
        "UndefinedVariableError",

        # ── Rust ──────────────────────────────────────────────────────────
        "UnwrapPanic",
        "IndexOutOfBoundsPanic",
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
        "notice",
        "silly",  # npm/winston-style log levels
        "fine",
        "finer",
        "finest",  # java.util.logging levels
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
        "NoneTypeError",
        "NullError",
        "AttributeError",
        "AssertionError",
        "ZeroDivisionError",
        "ConcurrentModificationException",
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
        "PoolExhaustedError",
        "TooManyConnectionsError",
        "ReplicationLagError",
        "QueryTimeoutError",
        "LockWaitTimeoutError",
        "ConstraintViolationError",
        "ForeignKeyViolationError",
        "UniqueViolationError",
        "DuplicateKeyError",
        "MongoServerError",
        "RedisConnectionError",
        "RedisTimeoutError",
        "SQLException",
        "DataAccessException",
        "CannotAcquireLockException",
        "OptimisticLockException",
        "NoResultFound",
        "MultipleResultsFound",
    }
)

# Infrastructure / memory / disk / process errors — require ops
# intervention, not code patches.
_INFRA_ERROR_TYPES: frozenset[str] = frozenset(
    {
        "OutOfMemoryError",
        "MemoryError",
        "SystemError",
        "SystemExit",
        "OutOfMemoryException",
        "KernelPanic",
        "RuntimePanic",
        "StackOverflowError",
        "StackOverflowException",
        "FatalError",
        "FatalExecutionEngineError",
        "InternalError",
        "VirtualMachineError",
        "ExecutionEngineException",
        "DiskFullError",
        "DiskIOError",
        "FileSystemError",
        "OSError",
        "GoPanic",
        "FatalGoroutineError",
        "RustPanic",
        "FatalProcessOutOfMemoryError",
        "CoreDumpError",
        "AppDomainUnloadedException",
    }
)

# External service / network / third-party errors — flaky dependency;
# patching source code rarely helps.
_EXTERNAL_DEPENDENCY_ERROR_TYPES: frozenset[str] = frozenset(
    {
        "TimeoutError",
        "ReadTimeoutError",
        "ConnectTimeoutError",
        "WriteTimeoutError",
        "GatewayTimeoutError",
        "ConnectionRefusedError",
        "ServiceUnavailableError",
        "CircuitBreakerOpenError",
        "DNSResolutionError",
        "SSLCertificateError",
        "SSLHandshakeError",
        "ConnectionResetError",
        "ConnectionAbortedError",
        "BrokenPipeError",
        "SocketTimeoutError",
        "TooManyRequestsError",
        "RateLimitExceededError",
        "ECONNREFUSED",
        "ECONNRESET",
        "ETIMEDOUT",
        "EPIPE",
        "EADDRINUSE",
        "KafkaTimeoutError",
        "MessageBrokerUnavailableError",
        "QueueFullError",
        "ConsumerGroupRebalanceError",
    }
)

# Security errors — must always require human triage regardless of confidence.
_SECURITY_ERROR_TYPES: frozenset[str] = frozenset(
    {
        "AuthenticationError",
        "AuthorizationError",
        "PermissionError",
        "AccessDeniedException",
        "ForbiddenError",
        "UnauthorizedError",
        "TokenExpiredError",
        "InvalidTokenError",
        "JWTError",
        "JWTExpiredError",
        "PaymentDeclinedError",
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

    @traceable(run_type="chain", name="classifier_node", process_inputs=strip_node_state)
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