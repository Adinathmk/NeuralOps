"""
fastapi/app/worker/tasks/run_agent.py

Phase 4 Part 3 — run_agent Celery Task: Deduplication + Agent Skeleton

This is the central Celery task of the NeuralOps AI pipeline.
It receives a ParsedLogEvent dict from parse_log, runs the two-layer
deduplication engine, and — if this is a genuinely new error — invokes
the LangGraph agent workflow (implemented in Part 4).

In Part 3, the LangGraph call is replaced with a SKELETON that returns
a minimal agent_result dict. This allows the full deduplication engine,
database persistence, and outbox event publishing to be tested end-to-end
before the LangGraph nodes are built.

Part 3 responsibilities:
  ┌──────────────────────────────────────────────────────────────────┐
  │  1. Compute SHA-256 fingerprint from parsed error identity       │
  │  2. Acquire Redis SETNX deduplication lock (Layer 1)             │
  │     └─ NOT acquired → lock_contention → return immediately       │
  │  3. Query DB-2 for active incident matching fingerprint (Layer 2)│
  │     └─ FOUND → record_duplicate_occurrence → return              │
  │  4. Invoke LangGraph agent (SKELETON in Part 3)                  │
  │  5. Persist new Incident + Analysis + Outbox events              │
  │  6. Release Redis lock (always — in finally block)               │
  └──────────────────────────────────────────────────────────────────┘

Part 4 will replace step 4 with the full LangGraph agent implementation.

Retry policy
------------
autoretry_for covers transient infrastructure errors only:
  - OSError, ConnectionError, TimeoutError: network/file system failures
  - aioredis.RedisError: Redis connection failures
  - sqlalchemy.exc.OperationalError: DB connection failures

Logic errors are NOT retried:
  - ValueError: invalid ParsedLogEvent data
  - sqlalchemy.exc.IntegrityError: late-detected duplicate (handled inline)

Task idempotency
----------------
The task is idempotent for duplicate executions of the same incident_id:
  - Layer 1 (Redis NX lock): prevents concurrent duplicates
  - Layer 2 (DB partial unique index): catches any that slip through
  - IntegrityError handler: treats late-detected duplicates gracefully
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid as _uuid_module
from typing import Any, Dict, Optional

import redis.asyncio as aioredis
import sqlalchemy.exc
from celery.utils.log import get_task_logger

from app.database.redis import get_redis
from app.database.session import AsyncSessionLocal
from app.schemas.parse_log import ParsedLogEvent
from app.services.dedup_lock import (
    DEDUP_LOCK_TTL_SECONDS,
    acquire_dedup_lock,
    release_dedup_lock,
)
from app.services.incidents import IncidentService, compute_fingerprint
from app.worker.celery_app import celery_app

logger = get_task_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Soft time limit for run_agent in seconds.
# Must be less than DEDUP_LOCK_TTL_SECONDS (30s) × safety_factor.
# Full GPT-4 analysis (Part 4) target: < 15s at p50, < 30s at p99.
# We set 480s (8 minutes) to allow for GPT-4 slowdowns under load.
_AGENT_SOFT_TIME_LIMIT: int = 480

# Hard kill limit in seconds (must be > soft limit)
_AGENT_HARD_TIME_LIMIT: int = 600


# ---------------------------------------------------------------------------
# Skeleton agent workflow (Part 3 placeholder)
# ---------------------------------------------------------------------------

async def _run_agent_workflow_skeleton(
    tenant_id: str,
    parsed_event: ParsedLogEvent,
    fingerprint: str,
    session: Any,
    redis: aioredis.Redis,
) -> Dict[str, Any]:
    """
    SKELETON: Simulates the LangGraph agent workflow output.

    Part 4 will replace this function with a call to:
        from app.agents.workflow import build_agent_workflow
        workflow = build_agent_workflow()
        result = await workflow.ainvoke({...})

    For Part 3, this skeleton:
      - Returns a minimal agent_result dict with placeholder values
      - Marks action="create_incident" so the full DB persistence path
        is exercised and testable
      - Uses a confidence_score of 0.75 (above typical threshold of 0.70)
        so incidents are created as 'open', not 'draft'

    This allows end-to-end testing of:
      - Fingerprint computation
      - Redis lock acquire/release
      - DB deduplication query
      - Incident + Analysis INSERT
      - Outbox event publication
      - Django snapshot consumer (consumes the Kafka event)

    Parameters
    ----------
    tenant_id : str
        Tenant UUID string.
    parsed_event : ParsedLogEvent
        Parsed log event from parse_log task.
    fingerprint : str
        Pre-computed fingerprint (available if needed by nodes).
    session : AsyncSession
        Open async SQLAlchemy session (for code_index queries in Part 4).
    redis : aioredis.Redis
        Open async Redis connection (for L1 cache in Part 4).

    Returns
    -------
    dict
        Minimal agent_result compatible with IncidentService.persist_new_incident().
    """
    # Simulate a minimal execution time so tests are realistic
    await asyncio.sleep(0)

    logger.info(
        "agent_workflow_skeleton_invoked",
        extra={
            "tenant_id": tenant_id,
            "incident_id": parsed_event.incident_id,
            "error_type": parsed_event.error_type,
            "note": "SKELETON — replace with LangGraph workflow in Part 4",
        },
    )

    # Placeholder confidence above the default 0.70 threshold
    # so the incident is created as 'open' (not 'draft') in tests
    placeholder_confidence: float = 0.75

    return {
        # ── Classification output (Classifier Node — Part 4) ──────────────
        "severity": parsed_event.severity if parsed_event.severity != "unknown" else "high",
        "actionable": True,
        "classifier_latency_ms": 0,

        # ── Code retrieval output (CodeRetriever Node — Part 4) ───────────
        "code_context": "",
        "code_retriever_meta": {
            "latency_ms": 0,
            "files_fetched": 0,
            "tokens": 0,
            "cache_hits": 0,
            "cache_misses": 0,
            "symbols_retrieved": 0,
        },

        # ── Playbook matching output (PlaybookMatcher Node — Part 4) ──────
        "matched_playbook_id": None,
        "playbook_instructions": None,
        "playbook_latency_ms": 0,

        # ── GPT-4 analysis output (Analyzer Node — Part 4) ────────────────
        "root_cause": (
            f"[SKELETON] {parsed_event.error_type} occurred in "
            f"{parsed_event.crash_method} at "
            f"{parsed_event.crash_file}:{parsed_event.crash_line}. "
            f"Full analysis will be provided in Part 4 (LangGraph nodes)."
        ),
        "raw_analysis_output": "",
        "analyzer_latency_ms": 0,
        "analyzer_fallback_used": True,
        "analyzer_tokens": {"prompt": 0, "completion": 0, "total": 0},

        # ── Fix generation output (FixGenerator Node — Part 4) ────────────
        "suggested_fix": (
            "[SKELETON] Fix generation will be provided in Part 4."
        ),
        "raw_fix_output": "",
        "fix_generator_latency_ms": 0,
        "fix_fallback_used": True,
        "fix_tokens": {"prompt": 0, "completion": 0, "total": 0},

        # ── Confidence scoring output (ConfidenceScorer Node — Part 4) ────
        "confidence_score": placeholder_confidence,
        "retrieval_score": 0.0,
        "coherence_score": 0.0,
        "scorer_latency_ms": 0,

        # ── Action decision output (ActionDecision Node — Part 4) ─────────
        "action": "create_incident",
        "confidence_threshold": 0.70,

        # ── Total timing (populated by run_agent after workflow returns) ───
        "total_latency_ms": 0,
    }


# ---------------------------------------------------------------------------
# Core async logic
# ---------------------------------------------------------------------------

async def _execute_run_agent(
    task_id: str,
    parsed_event_dict: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Core async coroutine for the run_agent task.

    Called via asyncio.run() from the synchronous Celery task function.
    All async I/O (Redis, DB, LangGraph, S3) happens here.

    Returns a result dict describing what action was taken:
      {"action": "lock_contention", ...}
      {"action": "duplicate_recorded", "incident_id": "...", ...}
      {"action": "create_incident", "incident_id": "...", "analysis_id": "..."}
      {"action": "store_draft", "incident_id": "...", "analysis_id": "..."}
      {"action": "not_actionable", ...}

    Parameters
    ----------
    task_id : str
        Celery task ID (used as Redis lock owner_id for debugging).
    parsed_event_dict : dict
        Serialised ParsedLogEvent from parse_log task.

    Raises
    ------
    ValueError
        On invalid ParsedLogEvent data (non-retryable).
    sqlalchemy.exc.OperationalError
        On DB connection failure (retryable by Celery).
    aioredis.RedisError
        On Redis failure during lock release (non-fatal; logged only).
    """
    # ── Step 0: Deserialise and validate ParsedLogEvent ───────────────────────
    try:
        parsed_event: ParsedLogEvent = ParsedLogEvent.from_dict(parsed_event_dict)
    except Exception as exc:
        raise ValueError(
            f"Invalid ParsedLogEvent dict — cannot deserialise: {exc}"
        ) from exc

    tenant_id_str: str = parsed_event.tenant_id
    incident_id_str: str = parsed_event.incident_id

    try:
        tenant_uuid: _uuid_module.UUID = _uuid_module.UUID(tenant_id_str)
    except ValueError as exc:
        raise ValueError(
            f"Invalid tenant_id UUID: '{tenant_id_str}': {exc}"
        ) from exc

    # ── Step 1: Compute fingerprint ───────────────────────────────────────────
    fingerprint: str = compute_fingerprint(
        tenant_id=tenant_id_str,
        service_name=parsed_event.service_name,
        error_type=parsed_event.error_type,
        crash_file=parsed_event.crash_file,
        crash_line=parsed_event.crash_line,
        crash_method=parsed_event.crash_method,
    )

    logger.info(
        "run_agent_started",
        extra={
            "tenant_id": tenant_id_str,
            "incident_id": incident_id_str,
            "error_type": parsed_event.error_type,
            "severity": parsed_event.severity,
            "fingerprint_prefix": fingerprint[:16],
            "task_id": task_id,
        },
    )

    # ── Step 2: Acquire Redis deduplication lock (Layer 1) ────────────────────
    redis: aioredis.Redis = get_redis()

    lock_acquired: bool = await acquire_dedup_lock(
        redis=redis,
        fingerprint=fingerprint,
        owner_id=task_id,
    )

    if not lock_acquired:
        # Another worker is currently processing the same fingerprint.
        # Return immediately — do NOT query the DB or run the agent.
        # The worker that DID acquire the lock will handle this incident.
        logger.info(
            "run_agent_lock_contention",
            extra={
                "tenant_id": tenant_id_str,
                "incident_id": incident_id_str,
                "fingerprint_prefix": fingerprint[:16],
                "task_id": task_id,
                "action": "lock_contention",
            },
        )
        return {
            "action": "lock_contention",
            "tenant_id": tenant_id_str,
            "incident_id": incident_id_str,
            "fingerprint": fingerprint,
        }

    # Lock acquired. Always release in the finally block below.
    agent_start_time: float = time.monotonic()

    try:
        # Open a single async session for this task execution.
        # The session is passed to IncidentService and (in Part 4) to
        # the LangGraph agent nodes that need DB-2 access (code_index).
        async with AsyncSessionLocal() as session:

            # ── Step 3: DB deduplication check (Layer 2) ─────────────────────
            incident_service = IncidentService(session)

            existing_incident = await incident_service.find_active_by_fingerprint(
                tenant_id=tenant_uuid,
                fingerprint=fingerprint,
            )

            if existing_incident is not None:
                # Active duplicate found: record the new occurrence and
                # publish the duplicate_detected outbox event.
                # The LangGraph pipeline is completely bypassed.
                new_count: int = await incident_service.record_duplicate_occurrence(
                    incident=existing_incident,
                    new_s3_key=parsed_event.s3_path,
                )

                logger.info(
                    "run_agent_duplicate_recorded",
                    extra={
                        "tenant_id": tenant_id_str,
                        "incident_id": incident_id_str,
                        "existing_incident_id": str(existing_incident.id),
                        "new_occurrence_count": new_count,
                        "fingerprint_prefix": fingerprint[:16],
                        "task_id": task_id,
                        "action": "duplicate_recorded",
                    },
                )

                return {
                    "action": "duplicate_recorded",
                    "tenant_id": tenant_id_str,
                    "incident_id": incident_id_str,
                    "existing_incident_id": str(existing_incident.id),
                    "new_occurrence_count": new_count,
                    "fingerprint": fingerprint,
                }

            # ── Step 4: Invoke LangGraph agent workflow ───────────────────────
            # In Part 3: calls the skeleton that returns placeholder values.
            # In Part 4: calls build_agent_workflow().ainvoke({...}).
            agent_result: Dict[str, Any] = await _run_agent_workflow_skeleton(
                tenant_id=tenant_id_str,
                parsed_event=parsed_event,
                fingerprint=fingerprint,
                session=session,
                redis=redis,
            )

            # Record total wall-clock time from task start to DB write
            total_latency_ms: int = int(
                (time.monotonic() - agent_start_time) * 1000
            )
            agent_result["total_latency_ms"] = total_latency_ms

            # ── Step 5: Determine action from agent result ────────────────────
            action: str = agent_result.get("action", "store_draft")
            confidence_score: Optional[float] = agent_result.get("confidence_score")
            confidence_threshold: float = agent_result.get(
                "confidence_threshold", 0.70
            )

            # Sanity check: enforce is_draft based on confidence_score
            # even if the agent_result action field claims "create_incident"
            # (guards against skeleton always returning create_incident when
            # a real threshold check should apply)
            if confidence_score is not None and confidence_score < confidence_threshold:
                action = "store_draft"
            is_draft: bool = (action == "store_draft")

            # Check if classifier marked the event as non-actionable
            # (only possible in Part 4; skeleton always returns actionable=True)
            if not agent_result.get("actionable", True):
                logger.info(
                    "run_agent_not_actionable",
                    extra={
                        "tenant_id": tenant_id_str,
                        "incident_id": incident_id_str,
                        "error_type": parsed_event.error_type,
                        "task_id": task_id,
                    },
                )
                return {
                    "action": "not_actionable",
                    "tenant_id": tenant_id_str,
                    "incident_id": incident_id_str,
                }

            # ── Step 6: Persist new Incident + Analysis + Outbox ─────────────
            try:
                persistence_result: Dict[str, _uuid_module.UUID] = (
                    await incident_service.persist_new_incident(
                        tenant_id=tenant_uuid,
                        parsed_event=parsed_event,
                        fingerprint=fingerprint,
                        agent_result=agent_result,
                        is_draft=is_draft,
                    )
                )

            except sqlalchemy.exc.IntegrityError as exc:
                # IntegrityError on the partial unique index means a concurrent
                # worker created an incident with the same fingerprint between
                # our DB check (Step 3) and this INSERT (Step 6).
                #
                # This is a very rare race — it can only happen if:
                #   a) The Redis lock was not acquired (fail-open path), OR
                #   b) Two workers acquired the lock in the same millisecond
                #      window (theoretically impossible with NX, but we defend anyway)
                #
                # Treatment: log as a late-detected duplicate and return.
                # Do NOT retry — the incident already exists.
                logger.warning(
                    "run_agent_late_duplicate_detected",
                    extra={
                        "tenant_id": tenant_id_str,
                        "incident_id": incident_id_str,
                        "fingerprint_prefix": fingerprint[:16],
                        "error": str(exc),
                        "task_id": task_id,
                        "action": "late_duplicate",
                    },
                )
                return {
                    "action": "late_duplicate",
                    "tenant_id": tenant_id_str,
                    "incident_id": incident_id_str,
                    "fingerprint": fingerprint,
                    "note": "IntegrityError — concurrent worker created incident first",
                }

    finally:
        # ── Step 7: ALWAYS release the Redis lock ─────────────────────────────
        # This runs whether the code above succeeded, raised, or returned early.
        # release_dedup_lock() catches and logs its own errors, so it is safe
        # to call unconditionally here without masking any exception.
        await release_dedup_lock(redis=redis, fingerprint=fingerprint)

    # ── Return success result ─────────────────────────────────────────────────
    new_incident_id: str = str(persistence_result["incident_id"])
    new_analysis_id: str = str(persistence_result["analysis_id"])

    logger.info(
        "run_agent_completed",
        extra={
            "tenant_id": tenant_id_str,
            "incident_id": incident_id_str,
            "new_incident_id": new_incident_id,
            "new_analysis_id": new_analysis_id,
            "fingerprint_prefix": fingerprint[:16],
            "action": action,
            "is_draft": is_draft,
            "confidence_score": confidence_score,
            "total_latency_ms": total_latency_ms,
            "task_id": task_id,
        },
    )

    return {
        "action": action,
        "tenant_id": tenant_id_str,
        "incident_id": incident_id_str,
        "new_incident_id": new_incident_id,
        "new_analysis_id": new_analysis_id,
        "fingerprint": fingerprint,
        "is_draft": is_draft,
        "confidence_score": confidence_score,
        "total_latency_ms": total_latency_ms,
    }


# ---------------------------------------------------------------------------
# Celery task entry point
# ---------------------------------------------------------------------------

@celery_app.task(
    name="app.worker.tasks.run_agent.run_agent",
    bind=True,
    # Requeue the task if the worker is killed mid-execution.
    acks_late=True,
    reject_on_worker_lost=True,
    # Retry ONLY on transient infrastructure errors.
    # Logic errors (ValueError, IntegrityError) are handled inline
    # and do NOT trigger autoretry.
    autoretry_for=(
        OSError,
        ConnectionError,
        TimeoutError,
        # Redis connection failures (not RedisError subclass that is
        # logic-level, but connection-level failures)
        aioredis.ConnectionError,
        # SQLAlchemy DB connection failures (not IntegrityError)
        sqlalchemy.exc.OperationalError,
        sqlalchemy.exc.DisconnectionError,
    ),
    max_retries=5,
    # Celery applies exponential backoff with autoretry_for:
    # attempt 1: 10s, attempt 2: 20s, attempt 3: 40s, attempt 4: 80s, attempt 5: 160s
    default_retry_delay=10,
    soft_time_limit=_AGENT_SOFT_TIME_LIMIT,
    time_limit=_AGENT_HARD_TIME_LIMIT,
)
def run_agent(
    self,
    *,
    parsed_event: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Phase 4 run_agent Celery task — Deduplication + Agent Skeleton.

    Receives a ParsedLogEvent dict from the parse_log task and:
      1. Computes a SHA-256 fingerprint of the error identity
      2. Acquires a Redis SETNX deduplication lock
      3. Queries DB-2 for an active incident with the same fingerprint
      4. If duplicate found: records new occurrence + publishes event
      5. If new error: invokes agent workflow (skeleton in Part 3)
      6. Persists Incident + Analysis + Outbox events atomically
      7. Releases the Redis lock (always, in finally block)

    Parameters
    ----------
    parsed_event : dict
        Serialised ParsedLogEvent dict produced by parse_log task.
        Must contain all fields defined in ParsedLogEvent schema.

    Returns
    -------
    dict
        Action result dict. Possible actions:
          - lock_contention   : Redis lock not acquired; skipped
          - duplicate_recorded: Active duplicate found; counter incremented
          - create_incident   : New incident created successfully
          - store_draft       : Low-confidence incident stored as draft
          - not_actionable    : Classifier determined non-actionable log
          - late_duplicate    : IntegrityError — concurrent duplicate
    """
    task_id: str = self.request.id or str(_uuid_module.uuid4())
    tenant_id: str = parsed_event.get("tenant_id", "unknown")
    incident_id: str = parsed_event.get("incident_id", "unknown")

    logger.info(
        "run_agent_task_received",
        extra={
            "tenant_id": tenant_id,
            "incident_id": incident_id,
            "error_type": parsed_event.get("error_type", "unknown"),
            "task_id": task_id,
            "attempt": self.request.retries + 1,
            "max_retries": self.max_retries,
        },
    )

    try:
        result: Dict[str, Any] = asyncio.run(
            _execute_run_agent(
                task_id=task_id,
                parsed_event_dict=parsed_event,
            )
        )
    except ValueError as exc:
        # Non-retryable logic error — bad input data will not improve on retry.
        logger.error(
            "run_agent_validation_error",
            extra={
                "tenant_id": tenant_id,
                "incident_id": incident_id,
                "error": str(exc),
                "task_id": task_id,
            },
        )
        # Re-raise without triggering autoretry (ValueError not in autoretry_for)
        raise
    except Exception as exc:
        # Retryable or unexpected error.
        logger.error(
            "run_agent_error",
            extra={
                "tenant_id": tenant_id,
                "incident_id": incident_id,
                "error_type": type(exc).__name__,
                "error": str(exc),
                "task_id": task_id,
                "attempt": self.request.retries + 1,
            },
            exc_info=True,
        )
        # Re-raise so Celery's autoretry_for mechanism can evaluate it.
        raise

    logger.info(
        "run_agent_task_complete",
        extra={
            "tenant_id": tenant_id,
            "incident_id": incident_id,
            "action": result.get("action"),
            "task_id": task_id,
        },
    )

    return result