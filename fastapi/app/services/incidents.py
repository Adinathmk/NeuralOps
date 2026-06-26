"""
fastapi/app/services/incidents.py

Phase 4 — IncidentService

All database read and write operations required by the run_agent
Celery task for:

  1. Querying for an active incident matching a fingerprint
     (deduplication lookup)

  2. Atomically recording a duplicate occurrence:
       - Increment occurrence_count
       - Append new S3 context key to occurrences[]
       - Update last_seen_at
       - Write incident.duplicate_detected outbox event

  3. Atomically persisting a brand-new incident + analysis record
     (only reached when no active duplicate exists):
       - INSERT incidents
       - INSERT analyses
       - INSERT outbox (incidents.created)
       - INSERT outbox (incidents.analyzed)

This service is instantiated once per run_agent task execution and
holds a single AsyncSession for its lifetime. All methods that write
to the database MUST be called from within an active transaction
(either the caller opens `async with session.begin()` or the method
does so internally — each method documents which pattern it uses).

Relationship to other components:
  - Called by:  fastapi/app/worker/tasks/run_agent.py
  - Uses:       fastapi/app/models/incidents.py  (Incident, Analysis, Alert)
  - Uses:       fastapi/app/models/outbox.py     (write_outbox)
  - Uses:       fastapi/app/schemas/parse_log.py (ParsedLogEvent)
"""

from __future__ import annotations

import hashlib
import uuid as _uuid_module
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.incidents import Alert, Analysis, Incident
from app.models.outbox import write_outbox
from app.schemas.parse_log import ParsedLogEvent

logger = get_logger(__name__)

import json

import aioboto3


async def _publish_to_sqs(
    tenant_id: str,
    incident_id: str,
    service_name: str,
    environment: str,
    error_type: str,
    severity: str,
) -> None:
    """
    Bypass Kafka and drop an incident.created event directly into the SQS
    Push Notifications queue. This natively triggers the push router Lambda.
    """
    queue_url = "https://sqs.ap-south-1.amazonaws.com/160823835768/neuralops-push-incidents.fifo"

    import os

    session = aioboto3.Session(
        aws_access_key_id=os.environ.get("DYNAMODB_ACCESS_KEY_ID"),
        aws_secret_access_key=os.environ.get("DYNAMODB_SECRET_ACCESS_KEY"),
        region_name=os.environ.get("SQS_REGION", "ap-south-1"),
    )

    event_id = str(_uuid_module.uuid4())
    message = {
        "event_id": event_id,
        "tenant_id": str(tenant_id),
        "incident_id": str(incident_id),
        "severity": severity,
        "error_type": error_type,
        "service_name": service_name,
        "environment": environment,
    }

    try:
        async with session.client("sqs", endpoint_url=None) as sqs_client:
            await sqs_client.send_message(
                QueueUrl=queue_url,
                MessageBody=json.dumps(message),
                MessageGroupId=str(tenant_id),
                MessageDeduplicationId=event_id,
            )
        logger.info(
            "sqs_push_notification_queued",
            tenant_id=tenant_id,
            incident_id=str(incident_id),
        )
    except Exception as exc:
        logger.error(
            "sqs_push_notification_failed",
            tenant_id=tenant_id,
            incident_id=str(incident_id),
            error=str(exc),
        )


# ---------------------------------------------------------------------------
# Fingerprint computation
# ---------------------------------------------------------------------------

# Line-number normalisation bucket size.
# Crash lines are rounded DOWN to the nearest multiple of this value before
# fingerprinting. This makes the fingerprint stable across trivial
# refactoring (e.g. adding a blank line or a comment) that shifts line
# numbers by a few positions while keeping the same logical crash location.
#
# Example with bucket size 5:
#   line 141 → 140
#   line 142 → 140
#   line 144 → 140
#   line 145 → 145   ← distinct bucket
#
# A bucket of 5 is intentionally conservative. It tolerates very minor
# edits without creating spurious new incidents, while still differentiating
# two distinct functions in the same file that are more than 5 lines apart.
FINGERPRINT_LINE_BUCKET: int = 5


def compute_fingerprint(
    tenant_id: str,
    service_name: str,
    error_type: str,
    crash_file: str,
    crash_line: int,
    crash_method: str,
) -> str:
    """
    Compute a deterministic 64-character hex fingerprint for incident
    deduplication.

    The fingerprint uniquely identifies a recurring error by its
    logical crash location within a tenant's service. It is designed
    to be stable across minor code edits while distinguishing genuinely
    different errors.

    Algorithm:
      1. Normalise crash_line: round down to nearest FINGERPRINT_LINE_BUCKET.
      2. Concatenate all six components with ':' separators.
      3. Encode as UTF-8 and compute SHA-256.
      4. Return the lowercase hex digest (64 chars).

    Parameters
    ----------
    tenant_id : str
        UUID string of the owning tenant. Scopes the fingerprint to one
        tenant — the same crash in two tenants is two distinct incidents.
    service_name : str
        Name of the service that produced the error. The same exception
        in two services is two distinct incidents.
    error_type : str
        Exception or error class name, e.g. 'NullPointerException'.
    crash_file : str
        Relative file path of the crash location.
    crash_line : int
        Raw line number from the stack trace top frame.
        Normalised to the nearest bucket before hashing.
    crash_method : str
        Method or function name at the crash location. Provides
        additional disambiguation when two methods are close enough
        that their crash lines fall in the same bucket.

    Returns
    -------
    str
        64-character lowercase hex SHA-256 digest.

    Examples
    --------
    >>> compute_fingerprint(
    ...     "tenant-uuid", "payment-svc", "NullPointerException",
    ...     "ChargeService.java", 142, "ChargeService.charge"
    ... )
    'a3f7b2c1d4e5f6...'  # 64 hex chars

    Notes
    -----
    - Empty strings for crash_file or crash_method produce a valid but
      less specific fingerprint. The deduplication still works correctly;
      it is simply coarser.
    - crash_line of 0 (unparseable) normalises to 0.
    """
    # Normalise line number: round down to nearest bucket
    normalised_line: int = (
        crash_line // FINGERPRINT_LINE_BUCKET
    ) * FINGERPRINT_LINE_BUCKET

    # Build the raw string from all six components
    # Use a separator that cannot appear in any component value
    raw: str = (
        f"{tenant_id}:{service_name}:{error_type}:"
        f"{crash_file}:{normalised_line}:{crash_method}"
    )

    # Compute SHA-256 and return lowercase hex
    digest: str = hashlib.sha256(raw.encode("utf-8")).hexdigest()

    logger.debug(
        "fingerprint_computed",
        extra={
            "tenant_id": tenant_id,
            "service_name": service_name,
            "error_type": error_type,
            "crash_file": crash_file,
            "crash_line_raw": crash_line,
            "crash_line_normalised": normalised_line,
            "crash_method": crash_method,
            "fingerprint_prefix": digest[:16],
        },
    )

    return digest


# ---------------------------------------------------------------------------
# IncidentService
# ---------------------------------------------------------------------------


class IncidentService:
    """
    Database operations for the Phase 4 incident analysis pipeline.

    Instantiate once per run_agent task execution:

        async with AsyncSessionLocal() as session:
            svc = IncidentService(session)
            existing = await svc.find_active_by_fingerprint(
                tenant_id=uuid.UUID(event.tenant_id),
                fingerprint=fingerprint,
            )

    All public methods are async. Methods that perform writes manage
    their own transactions internally (open and commit within the method).
    Callers do NOT need to wrap calls in `async with session.begin()`.

    The session is NOT shared between concurrent tasks. Each task has
    its own session and its own IncidentService instance.
    """

    def __init__(self, session: AsyncSession) -> None:
        """
        Parameters
        ----------
        session : AsyncSession
            An open (but not yet transactioned) async SQLAlchemy session.
            The session must be bound to DB-2 (FastAPI-owned PostgreSQL).
        """
        self._session = session

    # -----------------------------------------------------------------------
    # Deduplication query
    # -----------------------------------------------------------------------

    async def find_active_by_fingerprint(
        self,
        tenant_id: _uuid_module.UUID,
        fingerprint: str,
    ) -> Optional[Incident]:
        """
        Find an active (non-resolved, non-draft) incident for the given
        tenant that matches the fingerprint.

        "Active" means status NOT IN ('resolved', 'draft'). This mirrors
        the partial unique index definition on the incidents table, ensuring
        that the query uses the index efficiently.

        This is a READ-ONLY operation. The caller may call this method
        outside of a transaction; SQLAlchemy will use autobegin for the
        implicit SELECT.

        Parameters
        ----------
        tenant_id : uuid.UUID
            UUID of the owning tenant.
        fingerprint : str
            64-character hex fingerprint to search for.

        Returns
        -------
        Incident or None
            The active Incident row if found, else None.
        """
        # The partial unique index uq_incidents_tenant_fingerprint_active
        # covers exactly this query (tenant_id, fingerprint) WHERE status
        # != 'resolved'. PostgreSQL will use it for an
        # index scan rather than a sequential scan.
        stmt = (
            select(Incident)
            .where(
                Incident.tenant_id == tenant_id,
                Incident.fingerprint == fingerprint,
                Incident.status != "resolved",
            )
            .limit(1)
        )

        result = await self._session.execute(stmt)
        incident: Optional[Incident] = result.scalar_one_or_none()

        logger.debug(
            "dedup_db_check_result",
            extra={
                "tenant_id": str(tenant_id),
                "fingerprint_prefix": fingerprint[:16],
                "found": incident is not None,
                "existing_incident_id": str(incident.id) if incident else None,
                "existing_occurrence_count": (
                    incident.occurrence_count if incident else None
                ),
            },
        )

        return incident

    # -----------------------------------------------------------------------
    # Duplicate occurrence recording
    # -----------------------------------------------------------------------

    async def record_duplicate_occurrence(
        self,
        incident: Incident,
        new_s3_key: str,
    ) -> int:
        """
        Atomically record a new occurrence of an existing active incident.

        Performs the following within a single database transaction:
          1. UPDATE incidents SET
               occurrence_count = occurrence_count + 1,
               occurrences = array_append(occurrences, new_s3_key),
               last_seen_at = NOW(),
               updated_at = NOW()
             WHERE id = incident.id
          2. INSERT INTO outbox (topic: incidents.created,
                                 event_type: incident.duplicate_detected)

        The UPDATE uses a database-side expression (occurrence_count + 1)
        to prevent lost-update races when multiple workers process the same
        fingerprint nearly simultaneously. This is the same F() expression
        pattern used in the Django AlertRule and Playbook models.

        The outbox INSERT is in the SAME transaction so that Debezium
        cannot deliver the event if the UPDATE was rolled back.

        Parameters
        ----------
        incident : Incident
            The active Incident ORM instance returned by
            find_active_by_fingerprint().
        new_s3_key : str
            S3 object key of the new context buffer to append.
            Format: logs/{tenant_id}/context/{incident_id}.json.gz

        Returns
        -------
        int
            The new occurrence_count after incrementing.

        Raises
        ------
        sqlalchemy.exc.SQLAlchemyError
            On any database error. The transaction is rolled back.
        """
        now: datetime = datetime.now(timezone.utc)
        incident_id: _uuid_module.UUID = incident.id
        tenant_id: _uuid_module.UUID = incident.tenant_id
        event_id: _uuid_module.UUID = _uuid_module.uuid4()

        try:
            # ── Step 1: Atomic UPDATE using DB-side expression ────────────────
            # We use a raw UPDATE statement with occurrence_count + 1 rather
            # than reading the current count and adding 1 in Python.
            # This eliminates the read-modify-write race condition.
            #
            # The RETURNING clause fetches the new occurrence_count in the
            # same round-trip, avoiding a second SELECT.
            update_stmt = (
                update(Incident)
                .where(Incident.id == incident_id)
                .values(
                    occurrence_count=Incident.occurrence_count + 1,
                    # PostgreSQL array_append equivalent via || operator:
                    # occurrences = occurrences || ARRAY[new_s3_key]
                    occurrences=Incident.occurrences.op("||")([new_s3_key]),
                    last_seen_at=now,
                    updated_at=now,
                )
                .returning(Incident.occurrence_count)
            )

            result = await self._session.execute(update_stmt)
            new_count: Optional[int] = result.scalar()

            if new_count is None:
                # Edge case: The incident was deleted between the SELECT and UPDATE
                logger.warning(
                    "duplicate_update_missed",
                    incident_id=str(incident_id),
                    detail="Incident deleted before occurrence update could lock it.",
                )
                new_count = incident.occurrence_count

            # ── Step 2: Outbox event for Django analytics consumer ────────────
            # This outbox event drives metrics like "error frequency spikes".
            write_outbox(
                session=self._session,
                topic="incidents.duplicate_recorded",
                key=str(tenant_id),
                payload={
                    "event_id": str(event_id),
                    "event_type": "incident.duplicate_detected",
                    "version": 1,
                    "idempotency_key": (
                        f"tenant:{tenant_id}:"
                        f"incident:{incident_id}:"
                        f"occurrence:{new_count}"
                    ),
                    "source_version": new_count,
                    "occurred_at": now.isoformat(),
                    "payload": {
                        "incident_id": str(incident_id),
                        "tenant_id": str(tenant_id),
                        "new_occurrence_count": new_count,
                        "new_s3_key": new_s3_key,
                        "last_seen_at": now.isoformat(),
                    },
                },
            )
            await self._session.commit()
        except Exception as exc:
            await self._session.rollback()
            raise exc

        logger.info(
            "duplicate_occurrence_recorded",
            extra={
                "incident_id": str(incident_id),
                "tenant_id": str(tenant_id),
                "new_occurrence_count": new_count,
                "new_s3_key": new_s3_key,
            },
        )

        return new_count

    # -----------------------------------------------------------------------
    # New incident persistence
    # -----------------------------------------------------------------------

    async def persist_new_incident(
        self,
        tenant_id: _uuid_module.UUID,
        parsed_event: ParsedLogEvent,
        fingerprint: str,
        agent_result: Dict[str, Any],
        is_draft: bool,
    ) -> Dict[str, _uuid_module.UUID]:
        """
        Atomically persist a brand-new incident, its analysis record,
        and the required outbox events — all in a single transaction.

        This method is only called when:
          a) The Redis deduplication lock was acquired (no contention), AND
          b) No active incident matching the fingerprint was found in DB.

        Transaction contents (all-or-nothing):
          1. INSERT INTO incidents
          2. INSERT INTO analyses
          3. INSERT INTO outbox (incidents.created)   — skipped for drafts
          4. INSERT INTO outbox (incidents.analyzed)  — skipped for drafts

        Drafts are stored silently and not published to Kafka. They are
        visible in the DB for manual review and can be promoted to 'open'
        in a future admin operation.

        Parameters
        ----------
        tenant_id : uuid.UUID
            UUID of the owning tenant.
        parsed_event : ParsedLogEvent
            The fully-populated parsed log event from parse_log task.
        fingerprint : str
            64-character hex fingerprint (pre-computed by run_agent).
        agent_result : dict
            The combined output dict from the LangGraph workflow.
            Expected keys (all optional — defaults applied if missing):
              root_cause, suggested_fix, confidence_score, severity,
              code_context, analyzer_tokens, fix_tokens,
              analyzer_latency_ms, fix_generator_latency_ms,
              classifier_latency_ms, playbook_latency_ms,
              code_retriever_meta, scorer_latency_ms,
              confidence_threshold, retrieval_score, coherence_score,
              analyzer_fallback_used, fix_fallback_used,
              matched_playbook_id, raw_analysis_output, raw_fix_output,
              total_latency_ms
        is_draft : bool
            True when confidence_score < tenant threshold.
            Drafts are inserted but no Kafka events are published.

        Returns
        -------
        dict with keys:
            incident_id : uuid.UUID
            analysis_id : uuid.UUID

        Raises
        ------
        sqlalchemy.exc.IntegrityError
            If a concurrent task created an incident with the same
            fingerprint between the DB check and this INSERT.
            The partial unique index enforces this at the DB layer.
            The caller (run_agent) should catch this and treat it as
            a late-detected duplicate.
        sqlalchemy.exc.SQLAlchemyError
            On any other database error. Transaction is rolled back.
        """
        now: datetime = datetime.now(timezone.utc)
        incident_id: _uuid_module.UUID = _uuid_module.uuid4()
        analysis_id: _uuid_module.UUID = _uuid_module.uuid4()

        # ── Extract fields from agent_result with safe defaults ───────────────
        root_cause: str = agent_result.get("root_cause") or ""
        suggested_fix: str = agent_result.get("suggested_fix") or ""
        confidence_score: Optional[float] = agent_result.get("confidence_score")

        # Always promote to "open" to ensure collaboration threads sync via Kafka.
        # If the confidence was low (is_draft), flag severity as "unknown".
        if is_draft:
            severity: str = "unknown"
            is_draft = False
        else:
            severity: str = (
                agent_result.get("severity") or parsed_event.severity or "unknown"
            )

        status: str = "open"

        # Token usage (sum across analyzer + fix_generator nodes)
        analyzer_tokens: Dict[str, int] = agent_result.get("analyzer_tokens") or {}
        fix_tokens: Dict[str, int] = agent_result.get("fix_tokens") or {}
        total_tokens: int = analyzer_tokens.get("total", 0) + fix_tokens.get("total", 0)
        prompt_tokens: int = analyzer_tokens.get("prompt", 0) + fix_tokens.get(
            "prompt", 0
        )
        completion_tokens: int = analyzer_tokens.get("completion", 0) + fix_tokens.get(
            "completion", 0
        )

        # Per-node results for the analyses.node_results JSONB column
        node_results: Dict[str, Any] = _build_node_results(
            agent_result=agent_result,
            severity=severity,
            confidence_score=confidence_score,
            is_draft=is_draft,
        )

        try:
            # ── Step 1: INSERT incidents ──────────────────────────────────────
            incident = Incident(
                id=incident_id,
                tenant_id=tenant_id,
                fingerprint=fingerprint,
                occurrence_count=1,
                occurrences=[parsed_event.s3_path],
                error_type=parsed_event.error_type,
                error_message=parsed_event.error_message,
                service_name=parsed_event.service_name,
                environment=parsed_event.environment,
                crash_file=parsed_event.crash_file,
                crash_line=parsed_event.crash_line,
                crash_method=parsed_event.crash_method,
                stack_frames=(
                    [
                        f.to_dict() if hasattr(f, "to_dict") else f
                        for f in parsed_event.stack_frames
                    ]
                    if parsed_event.stack_frames
                    else []
                ),
                root_cause=root_cause,
                suggested_fix=suggested_fix,
                confidence_score=confidence_score,
                severity=severity,
                status=status,
                is_draft=is_draft,
                assigned_user_ids=[],
                source_log_id=_safe_uuid(parsed_event.incident_id),
                first_seen_at=now,
                last_seen_at=now,
                resolved_at=None,
                created_at=now,
                updated_at=now,
            )
            self._session.add(incident)

            # ── Step 2: INSERT analyses ───────────────────────────────────────
            analysis = Analysis(
                id=analysis_id,
                incident_id=incident_id,
                tenant_id=tenant_id,
                agent_version="1.0.0",
                total_tokens_used=total_tokens or None,
                prompt_tokens=prompt_tokens or None,
                completion_tokens=completion_tokens or None,
                total_latency_ms=agent_result.get("total_latency_ms"),
                node_results=node_results,
                raw_analysis_output=agent_result.get("raw_analysis_output") or None,
                raw_fix_output=agent_result.get("raw_fix_output") or None,
                code_context_snapshot=agent_result.get("code_context") or None,
                matched_playbook_id=_safe_uuid(agent_result.get("matched_playbook_id")),
                created_at=now,
            )
            self._session.add(analysis)

            # ── Steps 3 & 4: Outbox events (skipped for drafts) ───────────────
            if not is_draft:
                # incidents.created — consumed by Django snapshot consumer
                # and FastAPI WebSocket broadcaster
                write_outbox(
                    session=self._session,
                    topic="incidents.created",
                    key=str(tenant_id),
                    payload=_build_incident_created_payload(
                        incident_id=incident_id,
                        tenant_id=tenant_id,
                        fingerprint=fingerprint,
                        parsed_event=parsed_event,
                        status=status,
                        severity=severity,
                        root_cause=root_cause,
                        suggested_fix=suggested_fix,
                        confidence_score=confidence_score,
                        occurred_at=now,
                    ),
                )

                # incidents.analyzed — consumed by Django analytics consumer
                write_outbox(
                    session=self._session,
                    topic="incidents.analyzed",
                    key=str(tenant_id),
                    payload=_build_incident_analyzed_payload(
                        incident_id=incident_id,
                        analysis_id=analysis_id,
                        tenant_id=tenant_id,
                        total_tokens=total_tokens,
                        total_latency_ms=agent_result.get("total_latency_ms", 0),
                        confidence_score=confidence_score,
                        occurred_at=now,
                    ),
                )

            await self._session.commit()
        except Exception as exc:
            await self._session.rollback()
            raise exc

        logger.info(
            "new_incident_persisted",
            extra={
                "incident_id": str(incident_id),
                "analysis_id": str(analysis_id),
                "tenant_id": str(tenant_id),
                "fingerprint_prefix": fingerprint[:16],
                "status": status,
                "severity": severity,
                "confidence_score": confidence_score,
                "is_draft": is_draft,
                "total_tokens": total_tokens,
                "total_latency_ms": agent_result.get("total_latency_ms"),
            },
        )

        if not is_draft:
            await _publish_to_sqs(
                tenant_id=str(tenant_id),
                incident_id=str(incident_id),
                service_name=parsed_event.service_name,
                environment=parsed_event.environment,
                error_type=parsed_event.error_type,
                severity=severity,
            )

        return {
            "incident_id": incident_id,
            "analysis_id": analysis_id,
        }


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _safe_uuid(value: Any) -> Optional[_uuid_module.UUID]:
    """
    Convert a value to uuid.UUID, returning None if conversion fails.
    Used to safely handle optional UUID fields that may be None, empty
    strings, or already UUID objects.
    """
    if value is None:
        return None
    if isinstance(value, _uuid_module.UUID):
        return value
    try:
        return _uuid_module.UUID(str(value))
    except (ValueError, AttributeError):
        return None


def _build_node_results(
    agent_result: Dict[str, Any],
    severity: str,
    confidence_score: Optional[float],
    is_draft: bool,
) -> Dict[str, Any]:
    """
    Build the node_results JSONB dict for the analyses table.

    This captures per-node execution metadata in a structure that
    can be queried later for performance analysis and agent improvement.

    For Part 3 (skeleton mode), most nodes are not yet implemented.
    Each node key will be populated with actual values in Parts 4.
    For now, we store empty/zero values for nodes not yet run.
    """
    code_retriever_meta: Dict[str, Any] = agent_result.get("code_retriever_meta") or {}

    return {
        "classifier": {
            "latency_ms": agent_result.get("classifier_latency_ms", 0),
            "severity": severity,
            "actionable": True,
        },
        "code_retriever": {
            "latency_ms": code_retriever_meta.get("latency_ms", 0),
            "files_fetched": code_retriever_meta.get("files_fetched", 0),
            "tokens": code_retriever_meta.get("tokens", 0),
            "cache_hits": code_retriever_meta.get("cache_hits", 0),
            "cache_misses": code_retriever_meta.get("cache_misses", 0),
            "symbols_retrieved": code_retriever_meta.get("symbols_retrieved", 0),
        },
        "playbook_matcher": {
            "latency_ms": agent_result.get("playbook_latency_ms", 0),
            "matched_playbook_id": (
                str(agent_result.get("matched_playbook_id"))
                if agent_result.get("matched_playbook_id")
                else None
            ),
        },
        "analyzer": {
            "latency_ms": agent_result.get("analyzer_latency_ms", 0),
            "model": "gpt-4o",
            "fallback_used": bool(agent_result.get("analyzer_fallback_used", False)),
        },
        "fix_generator": {
            "latency_ms": agent_result.get("fix_generator_latency_ms", 0),
            "model": "gpt-4o",
            "fallback_used": bool(agent_result.get("fix_fallback_used", False)),
        },
        "confidence_scorer": {
            "latency_ms": agent_result.get("scorer_latency_ms", 0),
            "score": confidence_score if confidence_score is not None else 0.0,
            "retrieval_score": agent_result.get("retrieval_score", 0.0),
            "coherence_score": agent_result.get("coherence_score", 0.0),
        },
        "action_decision": {
            "action": "store_draft" if is_draft else "create_incident",
            "threshold": agent_result.get("confidence_threshold", 0.70),
            "score": confidence_score if confidence_score is not None else 0.0,
        },
    }


def _build_incident_created_payload(
    incident_id: _uuid_module.UUID,
    tenant_id: _uuid_module.UUID,
    fingerprint: str,
    parsed_event: ParsedLogEvent,
    status: str,
    severity: str,
    root_cause: str,
    suggested_fix: str,
    confidence_score: Optional[float],
    occurred_at: datetime,
) -> Dict[str, Any]:
    """
    Build the outbox payload for the incidents.created Kafka event.

    This payload is consumed by:
      - Django's consume_incidents management command (snapshot upsert)
      - FastAPI's WebSocket broadcaster (real-time incident stream)

    The payload mirrors the IncidentSnapshot fields in DB-1 so the
    consumer can upsert directly without additional DB-2 queries.
    """
    return {
        "event_id": str(_uuid_module.uuid4()),
        "event_type": "incident.created",
        "version": 1,
        "idempotency_key": (f"tenant:{tenant_id}:incident:{incident_id}"),
        "source_version": 1,
        "occurred_at": occurred_at.isoformat(),
        "payload": {
            "incident_id": str(incident_id),
            "tenant_id": str(tenant_id),
            "fingerprint": fingerprint,
            "status": status,
            "severity": severity,
            "is_draft": False,
            "error_type": parsed_event.error_type,
            "error_message": parsed_event.error_message,
            "service_name": parsed_event.service_name,
            "environment": parsed_event.environment,
            "crash_file": parsed_event.crash_file,
            "crash_line": parsed_event.crash_line,
            "crash_method": parsed_event.crash_method,
            "root_cause": root_cause,
            "suggested_fix": suggested_fix,
            "confidence_score": confidence_score,
            "occurrence_count": 1,
            "assigned_user_id": None,
            "first_seen_at": occurred_at.isoformat(),
            "last_seen_at": occurred_at.isoformat(),
            "created_at": occurred_at.isoformat(),
        },
    }


def _build_incident_analyzed_payload(
    incident_id: _uuid_module.UUID,
    analysis_id: _uuid_module.UUID,
    tenant_id: _uuid_module.UUID,
    total_tokens: int,
    total_latency_ms: int,
    confidence_score: Optional[float],
    occurred_at: datetime,
) -> Dict[str, Any]:
    """
    Build the outbox payload for the incidents.analyzed Kafka event.

    This payload is consumed by Django's analytics consumer to update
    aggregate metrics (token usage, latency, confidence distribution).
    """
    return {
        "event_id": str(_uuid_module.uuid4()),
        "event_type": "incident.analyzed",
        "version": 1,
        "idempotency_key": (f"tenant:{tenant_id}:analysis:{analysis_id}"),
        "source_version": 1,
        "occurred_at": occurred_at.isoformat(),
        "payload": {
            "incident_id": str(incident_id),
            "analysis_id": str(analysis_id),
            "tenant_id": str(tenant_id),
            "total_tokens_used": total_tokens,
            "total_latency_ms": total_latency_ms,
            "confidence_score": confidence_score,
            "agent_version": "1.0.0",
        },
    }
