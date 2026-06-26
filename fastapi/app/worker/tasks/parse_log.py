"""
fastapi/app/worker/tasks/parse_log.py

Celery task: parse_log

Consumes a raw log event triggered by a Debezium-published outbox row
on the raw.logs.{tenant_id} Kafka topic.

Responsibilities:
  1. Fetch the compressed SDK context buffer from S3
     (s3_path = logs/{tenant_id}/context/{incident_id}.json.gz)
  2. Decompress (gzip) and deserialise the JSON log array
  3. Identify the triggering error log entry (highest severity level)
  4. Extract structured error metadata:
       - error_type  (exception class name via regex)
       - error_message
       - severity
       - crash_file, crash_line, crash_method (from stack trace top frame)
       - stack_frames (ordered list of parsed frames)
  5. Build a typed ParsedLogEvent Pydantic model
  6. Enqueue the run_agent Celery task with the ParsedLogEvent dict
  7. Return the ParsedLogEvent dict as the task result

Error extraction handles three stack trace formats:
  Format A: structured list of dicts
    [{"file": "...", "line": 42, "method": "...", "module": "..."}]
  Format B: Java-style plain text
    "at com.example.Service.method(Service.java:42)"
  Format C: Python-style plain text
    'File "path/to/file.py", line 42, in method_name'

Retry policy:
  - Retries on transient errors (OSError, ConnectionError, TimeoutError)
  - Does NOT retry on logic errors (invalid JSON, missing S3 object)
  - Max 5 retries with exponential backoff (base 5s)
  - Messages exhausting retries are moved to the DLQ by Celery

Architecture note:
  run_agent is enqueued DIRECTLY from this task (not via a Kafka topic)
  to avoid an unnecessary broker round-trip on the hot analysis path.
  The parsed event is also published to parsed.logs.{tenant_id} for
  audit and replay purposes ONLY (see _publish_parsed_event).
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from celery.utils.log import get_task_logger

from app.schemas.parse_log import ParsedLogEvent, StackFrame
from app.worker.celery_app import celery_app

logger = get_task_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Severity ordering used to find the highest-severity log entry.
# Higher integer = higher severity.
_SEVERITY_ORDER: Dict[str, int] = {
    "critical": 6,
    "fatal": 6,
    "error": 5,
    "err": 5,
    "warning": 4,
    "warn": 4,
    "info": 3,
    "information": 3,
    "debug": 2,
    "trace": 1,
}

# Error type extraction patterns (tried in order, first match wins).
# These cover Java, Python, JavaScript, and Go error conventions.
_ERROR_TYPE_PATTERNS: List[re.Pattern] = [
    # Java / Kotlin: com.example.SomeException: message
    re.compile(r"^(?:[\w.]+\.)?([A-Z][A-Za-z0-9]*(?:Exception|Error|Fault|Panic))"),
    # Python: ValueError: message  or  SomeError: message
    re.compile(r"^([A-Z][A-Za-z0-9]*(?:Exception|Error|Warning|Fault))"),
    # Go: panic: runtime error: ...  →  RuntimeError
    re.compile(r"^panic:\s+(?:runtime error:\s+)?([A-Za-z][A-Za-z0-9 ]+)"),
    # JavaScript: TypeError: ...
    re.compile(r"^([A-Z][A-Za-z0-9]*Error)"),
    # Generic: any PascalCase word followed by colon
    re.compile(r"^([A-Z][A-Za-z0-9]{2,})\s*:"),
]

# Java-style stack frame pattern:
# "at com.example.ClassName.methodName(FileName.java:42)"
_JAVA_FRAME_RE = re.compile(
    r"^\s*at\s+"
    r"([\w$.<>]+)"  # fully-qualified method name
    r"\("
    r"([^:)]+)"  # file name
    r"(?::(\d+))?"  # optional line number
    r"\)\s*$"
)

# Python-style stack frame pattern:
# '  File "path/to/file.py", line 42, in method_name'
_PYTHON_FRAME_RE = re.compile(
    r'^\s*File\s+"([^"]+)"'  # file path
    r",\s+line\s+(\d+)"  # line number
    r",\s+in\s+(.+)\s*$"  # method name
)

# Python "module.ClassName.method" dotted path pattern
# Used to extract a short method name from a fully-qualified Python path
_PYTHON_METHOD_RE = re.compile(r"([^.]+(?:\.[^.]+)?)$")

# Maximum number of stack frames to retain
_MAX_STACK_FRAMES = 20

# Maximum log entries to scan when looking for the trigger event
# (prevents O(n) scan on extremely large buffers)
_MAX_SCAN_ENTRIES = 500


# ---------------------------------------------------------------------------
# S3 fetch helper
# ---------------------------------------------------------------------------


async def _fetch_compressed_context(
    s3_path: str,
    bucket_name: str,
    aws_access_key_id: Optional[str],
    aws_secret_access_key: Optional[str],
    aws_region_name: str,
    aws_endpoint_url: Optional[str],
) -> bytes:
    """
    Fetch the gzip-compressed context buffer from S3.

    Returns raw compressed bytes. Raises RuntimeError if the object
    does not exist or cannot be fetched. The caller is responsible
    for decompression.

    Uses aioboto3 for non-blocking I/O. Called via asyncio.run()
    from the synchronous Celery task context.
    """
    import aioboto3
    from botocore.exceptions import ClientError

    session = aioboto3.Session(
        aws_access_key_id=aws_access_key_id,
        aws_secret_access_key=aws_secret_access_key,
        region_name=aws_region_name,
    )

    try:
        async with session.client(
            "s3",
            endpoint_url=aws_endpoint_url,
        ) as s3_client:
            response = await s3_client.get_object(
                Bucket=bucket_name,
                Key=s3_path,
            )
            compressed_bytes: bytes = await response["Body"].read()
            return compressed_bytes

    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "Unknown")
        if error_code == "NoSuchKey":
            raise FileNotFoundError(
                f"S3 object not found: s3://{bucket_name}/{s3_path}"
            ) from exc
        raise RuntimeError(
            f"S3 fetch failed for key '{s3_path}' " f"(error code: {error_code}): {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Decompression helper
# ---------------------------------------------------------------------------


def _decompress_and_parse(
    compressed_bytes: bytes, s3_path: str
) -> List[Dict[str, Any]]:
    """
    Decompress gzip bytes and parse the JSON array of log entries.

    Returns a list of log entry dicts. Raises ValueError on invalid
    gzip or JSON. Caller must handle these as non-retryable failures.
    """
    try:
        raw_json_bytes: bytes = gzip.decompress(compressed_bytes)
    except (OSError, gzip.BadGzipFile) as exc:
        raise ValueError(f"Failed to decompress S3 object '{s3_path}': {exc}") from exc

    try:
        raw_json_str: str = raw_json_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"S3 object '{s3_path}' is not valid UTF-8: {exc}") from exc

    try:
        entries = json.loads(raw_json_str)
    except json.JSONDecodeError as exc:
        raise ValueError(f"S3 object '{s3_path}' is not valid JSON: {exc}") from exc

    if not isinstance(entries, list):
        raise ValueError(
            f"S3 object '{s3_path}' must be a JSON array. "
            f"Got: {type(entries).__name__}"
        )

    return entries


# ---------------------------------------------------------------------------
# Trigger log identification
# ---------------------------------------------------------------------------


def _find_trigger_log(
    entries: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Identify the triggering error log entry from the context buffer.

    Selection strategy:
      1. Find the entry with the highest severity level.
      2. If multiple entries share the highest severity, take the LAST one
         (it is most likely the crash trigger in a time-ordered buffer).
      3. If the buffer contains no entries at all, return an empty dict.

    Only the first _MAX_SCAN_ENTRIES entries are scanned to bound cost.
    """
    if not entries:
        return {}

    scan_entries = entries[:_MAX_SCAN_ENTRIES]

    best_score = -1
    trigger: Dict[str, Any] = {}

    for entry in scan_entries:
        if not isinstance(entry, dict):
            continue
        level = str(entry.get("level", "")).lower().strip()
        score = _SEVERITY_ORDER.get(level, 0)
        if score >= best_score:
            # >= so we take the LAST occurrence of the highest level
            best_score = score
            trigger = entry

    return trigger


# ---------------------------------------------------------------------------
# Error type extraction
# ---------------------------------------------------------------------------


def _extract_error_type(message: str) -> str:
    """
    Extract the error type (exception class name) from a log message string.

    Tries each pattern in _ERROR_TYPE_PATTERNS in order.
    Returns "UnknownError" if no pattern matches.

    Examples:
      "NullPointerException: cannot invoke charge()"  → "NullPointerException"
      "com.example.PaymentError: stripe timeout"      → "PaymentError"
      "ValueError: invalid literal for int()"          → "ValueError"
      "panic: runtime error: index out of range"       → "RuntimeError"
    """
    if not message or not isinstance(message, str):
        return "UnknownError"

    message = message.strip()

    for pattern in _ERROR_TYPE_PATTERNS:
        match = pattern.match(message)
        if match:
            error_type = match.group(1).strip()
            # Cap length to match DB column constraint
            return error_type[:255] if len(error_type) > 255 else error_type

    # Fallback to the first 100 chars of the message instead of UnknownError
    first_line = message.splitlines()[0] if message else "UnknownError"
    return first_line[:100] if len(first_line) > 100 else first_line


# ---------------------------------------------------------------------------
# Stack trace parsing
# ---------------------------------------------------------------------------


def _parse_stack_frames_from_list(
    raw_frames: List[Any],
) -> List[StackFrame]:
    """
    Parse stack frames from a structured list of dicts.

    Expected format (SDK standard):
      [
        {"file": "src/ChargeService.java", "line": 142,
         "method": "ChargeService.charge", "module": "com.example"},
        ...
      ]

    Unknown keys are ignored. Missing required keys default to empty/0.
    """
    frames: List[StackFrame] = []

    for raw in raw_frames:
        if not isinstance(raw, dict):
            continue
        try:
            frame = StackFrame(
                file=str(raw.get("file", "") or ""),
                line=raw.get("line", 0),
                method=str(raw.get("method", "") or ""),
                module=str(raw.get("module", "") or ""),
            )
            frames.append(frame)
        except Exception:
            # Skip unparseable frames silently
            continue

    return frames[:_MAX_STACK_FRAMES]


def _parse_stack_frames_from_java_text(text: str) -> List[StackFrame]:
    """
    Parse Java-style plain-text stack trace into StackFrame objects.

    Input example:
      "java.lang.NullPointerException: null\\n"
      "\\tat com.neuralops.payment.ChargeService.charge(ChargeService.java:142)\\n"
      "\\tat com.neuralops.api.PaymentController.process(PaymentController.java:78)\\n"

    Only lines matching the Java 'at' frame pattern are extracted.
    """
    frames: List[StackFrame] = []

    for line in text.splitlines():
        match = _JAVA_FRAME_RE.match(line)
        if not match:
            continue

        full_method = match.group(1)  # e.g. com.example.Service.method
        file_name = match.group(2)  # e.g. Service.java
        line_number_str = match.group(3)  # e.g. "142" or None

        line_number = int(line_number_str) if line_number_str else 0

        # Extract short method name (last two dot-separated components)
        parts = full_method.rsplit(".", 2)
        if len(parts) >= 2:
            short_method = f"{parts[-2]}.{parts[-1]}"
        else:
            short_method = full_method

        # Extract module (everything before the last two components)
        module = ".".join(full_method.split(".")[:-2]) if "." in full_method else ""

        frames.append(
            StackFrame(
                file=file_name,
                line=line_number,
                method=short_method,
                module=module,
            )
        )

        if len(frames) >= _MAX_STACK_FRAMES:
            break

    return frames


def _parse_stack_frames_from_python_text(text: str) -> List[StackFrame]:
    """
    Parse Python-style plain-text stack trace into StackFrame objects.

    Input example (from Python traceback):
      '  File "src/payment/charge.py", line 142, in charge\\n'
      '    stripe_client.charge(amount)\\n'
      '  File "src/api/views.py", line 78, in process_payment\\n'

    Only 'File "..."' lines are extracted; code lines are ignored.
    Frames are reversed so index 0 is the innermost (crash) frame,
    matching the Java convention used throughout the pipeline.
    """
    frames: List[StackFrame] = []

    for line in text.splitlines():
        match = _PYTHON_FRAME_RE.match(line)
        if not match:
            continue

        file_path = match.group(1)  # e.g. src/payment/charge.py
        line_number = int(match.group(2))
        method_name = match.group(3).strip()  # e.g. charge

        frames.append(
            StackFrame(
                file=file_path,
                line=line_number,
                method=method_name,
                module="",
            )
        )

        if len(frames) >= _MAX_STACK_FRAMES:
            break

    # Python tracebacks list outermost frame first; reverse to match Java
    # convention (index 0 = crash frame = innermost).
    frames.reverse()
    return frames


def _parse_stack_frames(
    raw_stack_trace: Any,
) -> List[StackFrame]:
    """
    Dispatch to the correct stack trace parser based on the input type.

    Handles three formats:
      - List of dicts (SDK structured format)  → _parse_stack_frames_from_list
      - String containing 'at ' markers        → Java text parser
      - String containing 'File "' markers     → Python text parser
      - Anything else                          → empty list

    Returns an empty list on any parse error; downstream nodes tolerate
    missing stack trace data.
    """
    if not raw_stack_trace:
        return []

    # Format A: structured list
    if isinstance(raw_stack_trace, list):
        return _parse_stack_frames_from_list(raw_stack_trace)

    # Format B or C: plain text
    if isinstance(raw_stack_trace, str):
        text = raw_stack_trace.strip()
        if not text:
            return []

        # Detect format by signature strings
        if "\tat " in text or "\n\tat " in text or text.startswith("at "):
            return _parse_stack_frames_from_java_text(text)

        if 'File "' in text:
            return _parse_stack_frames_from_python_text(text)

        # Unknown text format — attempt Java parser as fallback
        frames = _parse_stack_frames_from_java_text(text)
        if frames:
            return frames

        # Final fallback: attempt Python parser
        return _parse_stack_frames_from_python_text(text)

    # Format unknown — return empty list without raising
    logger.warning(
        "parse_log_unknown_stack_trace_format",
        extra={"stack_trace_type": type(raw_stack_trace).__name__},
    )
    return []


# ---------------------------------------------------------------------------
# ParsedLogEvent construction
# ---------------------------------------------------------------------------


def _build_parsed_event(
    tenant_id: str,
    incident_id: str,
    s3_path: str,
    service_name: str,
    environment: str,
    entries: List[Dict[str, Any]],
) -> ParsedLogEvent:
    """
    Orchestrate trigger identification, field extraction, and stack trace
    parsing to produce a fully-populated ParsedLogEvent.

    This function is pure (no I/O) and fully testable in isolation.
    """
    # Step 1: Find the triggering log entry
    trigger = _find_trigger_log(entries)

    if not trigger:
        # No usable log entry found — build a minimal event
        return ParsedLogEvent(
            tenant_id=tenant_id,
            incident_id=incident_id,
            s3_path=s3_path,
            service_name=service_name,
            environment=environment,
            error_type="UnknownError",
            error_message="",
            severity="unknown",
            crash_file="",
            crash_line=0,
            crash_method="",
            stack_frames=[],
            context_log_count=len(entries),
        )

    # Step 2: Extract raw fields from trigger entry
    raw_message: str = str(trigger.get("message", "") or "")
    raw_severity: str = str(trigger.get("level", "unknown") or "unknown")
    raw_stack_trace: Any = trigger.get("stack_trace") or trigger.get("stackTrace")

    # Step 3: Extract error type from message
    error_type: str = _extract_error_type(raw_message)

    # Step 4: Parse stack frames
    stack_frames: List[StackFrame] = _parse_stack_frames(raw_stack_trace)

    # Step 5: Extract crash location from top frame
    crash_file: str = ""
    crash_line: int = 0
    crash_method: str = ""

    if stack_frames:
        top_frame = stack_frames[0]
        crash_file = top_frame.file
        crash_line = top_frame.line
        crash_method = top_frame.method

    # Step 6: Assemble and validate via Pydantic
    return ParsedLogEvent(
        tenant_id=tenant_id,
        incident_id=incident_id,
        s3_path=s3_path,
        service_name=service_name,
        environment=environment,
        error_type=error_type,
        error_message=raw_message,
        severity=raw_severity,
        crash_file=crash_file,
        crash_line=crash_line,
        crash_method=crash_method,
        stack_frames=stack_frames,
        context_log_count=len(entries),
    )


# ---------------------------------------------------------------------------
# Kafka publish helper (audit / replay path)
# ---------------------------------------------------------------------------


async def _publish_parsed_event_to_kafka(
    parsed_event: ParsedLogEvent,
) -> None:
    """
    Publish the ParsedLogEvent to the parsed.logs.{tenant_id} Kafka topic.

    This is an audit / replay path only. The run_agent task is enqueued
    DIRECTLY by parse_log (not triggered by this Kafka message) to avoid
    an unnecessary broker round-trip on the hot analysis path.

    This publish writes to the DB-2 outbox table within an independent
    async session so that a Kafka infrastructure failure does NOT prevent
    run_agent from being enqueued.

    Failures here are logged and swallowed; they do not fail the task.
    """
    import uuid as _uuid_module
    from datetime import datetime as _dt
    from datetime import timezone as _tz

    from app.database.session import AsyncSessionLocal
    from app.models.outbox import write_outbox

    try:
        async with AsyncSessionLocal() as session:
            async with session.begin():
                write_outbox(
                    session=session,
                    topic=f"parsed.logs.{parsed_event.tenant_id}",
                    key=parsed_event.incident_id,
                    payload={
                        "event_id": str(_uuid_module.uuid4()),
                        "event_type": "log.parsed",
                        "version": 1,
                        "idempotency_key": (
                            f"tenant:{parsed_event.tenant_id}"
                            f":parsed:{parsed_event.incident_id}"
                        ),
                        "source_version": 1,
                        "occurred_at": _dt.now(_tz.utc).isoformat(),
                        "payload": parsed_event.to_dict(),
                    },
                )
    except Exception as exc:
        # Non-fatal: log and continue. run_agent is already enqueued.
        logger.warning(
            "parse_log_kafka_publish_failed",
            extra={
                "tenant_id": parsed_event.tenant_id,
                "incident_id": parsed_event.incident_id,
                "error": str(exc),
            },
        )


# ---------------------------------------------------------------------------
# Celery task
# ---------------------------------------------------------------------------


@celery_app.task(
    name="app.worker.tasks.parse_log.parse_log",
    bind=True,
    # Ensure the task is not lost if the worker crashes mid-execution.
    acks_late=True,
    reject_on_worker_lost=True,
    # Retry only on transient infrastructure errors.
    # Logic errors (bad JSON, missing S3 key) are NOT retried because
    # retrying will not fix them and will only delay DLQ placement.
    autoretry_for=(
        OSError,
        ConnectionError,
        TimeoutError,
    ),
    max_retries=5,
    # Celery applies exponential backoff when autoretry_for is used:
    # attempt 1: 5s, attempt 2: 10s, attempt 3: 20s, attempt 4: 40s, attempt 5: 80s
    default_retry_delay=5,
    soft_time_limit=60,  # Raise SoftTimeLimitExceeded after 60s
    time_limit=120,  # Hard kill after 120s
)
def parse_log(
    self,
    *,
    tenant_id: str,
    incident_id: str,
    s3_path: str,
    service_name: str,
    environment: str,
) -> Dict[str, Any]:
    """
    Parse a raw log context buffer from S3 and enqueue the run_agent task.

    Parameters
    ----------
    tenant_id : str
        UUID string of the owning tenant.
    incident_id : str
        UUID string of the ingested_log_metadata row (= S3 key suffix).
    s3_path : str
        Full S3 object key of the compressed context buffer.
        Format: logs/{tenant_id}/context/{incident_id}.json.gz
    service_name : str
        Name of the originating service (from the ingest payload).
    environment : str
        Deployment environment label (from the ingest payload).

    Returns
    -------
    dict
        Serialised ParsedLogEvent dict, also used as the run_agent argument.

    Raises
    ------
    FileNotFoundError
        If the S3 object does not exist. NOT retried (permanent failure).
    ValueError
        If the S3 content is not valid gzip or JSON. NOT retried.
    OSError / ConnectionError / TimeoutError
        Transient infrastructure failures. Retried with exponential backoff.
    """
    from app.core.config import get_settings

    settings = get_settings()

    logger.info(
        "parse_log_started",
        extra={
            "tenant_id": tenant_id,
            "incident_id": incident_id,
            "s3_path": s3_path,
            "service_name": service_name,
            "environment": environment,
            "task_id": self.request.id,
            "attempt": self.request.retries + 1,
        },
    )

    # ── Step 1: Fetch compressed context buffer from S3 ───────────────────────
    try:
        compressed_bytes: bytes = asyncio.run(
            _fetch_compressed_context(
                s3_path=s3_path,
                bucket_name=settings.AWS_S3_BUCKET_NAME,
                aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
                aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
                aws_region_name=settings.AWS_REGION_NAME,
                aws_endpoint_url=settings.AWS_S3_ENDPOINT_URL,
            )
        )
    except FileNotFoundError as exc:
        # S3 object missing — this is a permanent failure, do not retry.
        logger.error(
            "parse_log_s3_object_not_found",
            extra={
                "tenant_id": tenant_id,
                "incident_id": incident_id,
                "s3_path": s3_path,
                "error": str(exc),
                "task_id": self.request.id,
            },
        )
        # Re-raise as a non-retryable exception by NOT including it in
        # autoretry_for. This causes the task to move to the DLQ immediately.
        raise

    logger.debug(
        "parse_log_s3_fetch_success",
        extra={
            "tenant_id": tenant_id,
            "incident_id": incident_id,
            "compressed_bytes": len(compressed_bytes),
        },
    )

    # ── Step 2: Decompress and parse JSON ─────────────────────────────────────
    try:
        entries: List[Dict[str, Any]] = _decompress_and_parse(compressed_bytes, s3_path)
    except ValueError as exc:
        # Invalid gzip or JSON — permanent failure, do not retry.
        logger.error(
            "parse_log_decompress_failed",
            extra={
                "tenant_id": tenant_id,
                "incident_id": incident_id,
                "s3_path": s3_path,
                "error": str(exc),
                "task_id": self.request.id,
            },
        )
        raise

    logger.debug(
        "parse_log_decompressed",
        extra={
            "tenant_id": tenant_id,
            "incident_id": incident_id,
            "entry_count": len(entries),
        },
    )

    # ── Step 3: Build ParsedLogEvent ──────────────────────────────────────────
    try:
        parsed_event: ParsedLogEvent = _build_parsed_event(
            tenant_id=tenant_id,
            incident_id=incident_id,
            s3_path=s3_path,
            service_name=service_name,
            environment=environment,
            entries=entries,
        )
    except Exception as exc:
        # Pydantic validation or unexpected extraction error.
        # Treat as permanent — bad input data will not improve on retry.
        logger.error(
            "parse_log_build_event_failed",
            extra={
                "tenant_id": tenant_id,
                "incident_id": incident_id,
                "error": str(exc),
                "task_id": self.request.id,
            },
            exc_info=True,
        )
        raise ValueError(
            f"Failed to build ParsedLogEvent for incident {incident_id}: {exc}"
        ) from exc

    logger.info(
        "parse_log_extraction_complete",
        extra={
            "tenant_id": tenant_id,
            "incident_id": incident_id,
            "error_type": parsed_event.error_type,
            "severity": parsed_event.severity,
            "crash_file": parsed_event.crash_file,
            "crash_line": parsed_event.crash_line,
            "crash_method": parsed_event.crash_method,
            "stack_frame_count": len(parsed_event.stack_frames),
            "context_log_count": parsed_event.context_log_count,
            "task_id": self.request.id,
        },
    )

    # ── Step 4: Serialise to dict for Celery task argument ────────────────────
    parsed_event_dict: Dict[str, Any] = parsed_event.to_dict()

    # ── Step 5: Publish to parsed.logs Kafka topic (audit/replay — non-fatal) ─
    try:
        asyncio.run(_publish_parsed_event_to_kafka(parsed_event))
    except Exception as exc:
        # This is intentionally caught and swallowed here because the
        # Kafka publish is an audit path, not required for correctness.
        # The run_agent task is enqueued directly below regardless.
        logger.warning(
            "parse_log_audit_publish_error",
            extra={
                "tenant_id": tenant_id,
                "incident_id": incident_id,
                "error": str(exc),
            },
        )

    # ── Step 6: Enqueue run_agent task (DIRECT — not via Kafka) ──────────────
    # Import here to avoid circular import at module load time.
    # run_agent is registered in the same Celery app; the import is safe.
    try:
        from app.worker.tasks.run_agent import run_agent  # noqa: PLC0415

        run_agent.delay(parsed_event=parsed_event_dict)

        logger.info(
            "parse_log_run_agent_enqueued",
            extra={
                "tenant_id": tenant_id,
                "incident_id": incident_id,
                "error_type": parsed_event.error_type,
                "task_id": self.request.id,
            },
        )
    except Exception as exc:
        # run_agent enqueue failure IS fatal — we cannot proceed without it.
        # Raise as a retryable error so the task retries and re-enqueues.
        logger.error(
            "parse_log_enqueue_run_agent_failed",
            extra={
                "tenant_id": tenant_id,
                "incident_id": incident_id,
                "error": str(exc),
                "task_id": self.request.id,
            },
            exc_info=True,
        )
        raise ConnectionError(
            f"Failed to enqueue run_agent for incident {incident_id}: {exc}"
        ) from exc

    # ── Step 7: Return the serialised event as task result ────────────────────
    return parsed_event_dict
