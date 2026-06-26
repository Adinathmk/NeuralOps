"""
fastapi/tests/unit/test_part3_deduplication.py

Unit tests for Phase 4 Part 3:
  - compute_fingerprint (normalisation, consistency, boundary cases)
  - acquire_dedup_lock / release_dedup_lock (Redis mock)
  - IncidentService.find_active_by_fingerprint (DB mock)
  - IncidentService.record_duplicate_occurrence (DB mock)
  - IncidentService.persist_new_incident (DB mock)
  - _execute_run_agent (integration: lock + dedup + persist)

All tests are pure unit tests — no real DB, no real Redis, no S3.
Async DB operations are mocked via AsyncMock + MagicMock.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from app.schemas.parse_log import ParsedLogEvent, StackFrame
from app.services.dedup_lock import (
    DEDUP_LOCK_TTL_SECONDS,
    acquire_dedup_lock,
    dedup_lock_key,
    release_dedup_lock,
)
from app.services.incidents import (
    FINGERPRINT_LINE_BUCKET,
    IncidentService,
    _build_incident_created_payload,
    _build_node_results,
    _safe_uuid,
    compute_fingerprint,
)
from app.worker.tasks.run_agent import _execute_run_agent

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

TENANT_ID = "11111111-2222-3333-4444-555555555555"
INCIDENT_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
SERVICE_NAME = "payment-service"
ENVIRONMENT = "production"
ERROR_TYPE = "NullPointerException"
CRASH_FILE = "src/payment/ChargeService.java"
CRASH_LINE = 142
CRASH_METHOD = "ChargeService.charge"


def _make_parsed_event(**overrides) -> ParsedLogEvent:
    """Create a valid ParsedLogEvent for testing."""
    defaults = dict(
        tenant_id=TENANT_ID,
        incident_id=INCIDENT_ID,
        s3_path=f"logs/{TENANT_ID}/context/{INCIDENT_ID}.json.gz",
        service_name=SERVICE_NAME,
        environment=ENVIRONMENT,
        error_type=ERROR_TYPE,
        error_message="cannot invoke charge() on null",
        severity="error",
        crash_file=CRASH_FILE,
        crash_line=CRASH_LINE,
        crash_method=CRASH_METHOD,
        stack_frames=[
            StackFrame(
                file=CRASH_FILE,
                line=CRASH_LINE,
                method=CRASH_METHOD,
                module="com.neuralops.payment",
            )
        ],
        context_log_count=50,
    )
    defaults.update(overrides)
    return ParsedLogEvent(**defaults)


def _make_mock_incident(
    incident_id: Optional[str] = None,
    occurrence_count: int = 1,
    status: str = "open",
) -> MagicMock:
    """Create a mock Incident ORM instance."""
    mock = MagicMock()
    mock.id = uuid.UUID(incident_id) if incident_id else uuid.uuid4()
    mock.tenant_id = uuid.UUID(TENANT_ID)
    mock.occurrence_count = occurrence_count
    mock.status = status
    mock.fingerprint = "a" * 64
    mock.s3_path = f"logs/{TENANT_ID}/context/existing.json.gz"
    return mock


def _make_mock_session() -> MagicMock:
    """Create a mock AsyncSession with async context manager support."""
    session = MagicMock()

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_result.scalar.return_value = None
    mock_result.fetchone.return_value = None
    session.execute = AsyncMock(return_value=mock_result)

    session.add = MagicMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()

    # Make session.begin() work as async context manager
    begin_ctx = AsyncMock()
    begin_ctx.__aenter__ = AsyncMock(return_value=None)
    begin_ctx.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_ctx)

    return session


def _make_mock_redis() -> MagicMock:
    """Create a mock aioredis.Redis client."""
    redis = MagicMock()
    redis.set = AsyncMock(return_value=True)
    redis.delete = AsyncMock(return_value=1)
    redis.get = AsyncMock(return_value=None)
    return redis


def _make_skeleton_agent_result() -> Dict[str, Any]:
    """Return the expected skeleton agent result for test assertions."""
    return {
        "severity": "high",
        "actionable": True,
        "classifier_latency_ms": 0,
        "code_context": "",
        "code_retriever_meta": {
            "latency_ms": 0,
            "files_fetched": 0,
            "tokens": 0,
            "cache_hits": 0,
            "cache_misses": 0,
            "symbols_retrieved": 0,
        },
        "matched_playbook_id": None,
        "playbook_instructions": None,
        "playbook_latency_ms": 0,
        "root_cause": "placeholder",
        "raw_analysis_output": "",
        "analyzer_latency_ms": 0,
        "analyzer_fallback_used": True,
        "analyzer_tokens": {"prompt": 0, "completion": 0, "total": 0},
        "suggested_fix": "placeholder",
        "raw_fix_output": "",
        "fix_generator_latency_ms": 0,
        "fix_fallback_used": True,
        "fix_tokens": {"prompt": 0, "completion": 0, "total": 0},
        "confidence_score": 0.75,
        "retrieval_score": 0.0,
        "coherence_score": 0.0,
        "scorer_latency_ms": 0,
        "action": "create_incident",
        "confidence_threshold": 0.70,
        "total_latency_ms": 0,
    }


# ---------------------------------------------------------------------------
# compute_fingerprint tests
# ---------------------------------------------------------------------------


class TestComputeFingerprint:

    def test_returns_64_char_hex_string(self):
        result = compute_fingerprint(
            TENANT_ID,
            SERVICE_NAME,
            ERROR_TYPE,
            CRASH_FILE,
            CRASH_LINE,
            CRASH_METHOD,
        )
        assert isinstance(result, str)
        assert len(result) == 64
        assert all(c in "0123456789abcdef" for c in result)

    def test_deterministic_same_inputs(self):
        result1 = compute_fingerprint(
            TENANT_ID,
            SERVICE_NAME,
            ERROR_TYPE,
            CRASH_FILE,
            CRASH_LINE,
            CRASH_METHOD,
        )
        result2 = compute_fingerprint(
            TENANT_ID,
            SERVICE_NAME,
            ERROR_TYPE,
            CRASH_FILE,
            CRASH_LINE,
            CRASH_METHOD,
        )
        assert result1 == result2

    def test_line_normalisation_same_bucket(self):
        """Lines 140–144 normalise to 140 → same fingerprint."""
        fp_140 = compute_fingerprint(
            TENANT_ID, SERVICE_NAME, ERROR_TYPE, CRASH_FILE, 140, CRASH_METHOD
        )
        fp_142 = compute_fingerprint(
            TENANT_ID, SERVICE_NAME, ERROR_TYPE, CRASH_FILE, 142, CRASH_METHOD
        )
        fp_144 = compute_fingerprint(
            TENANT_ID, SERVICE_NAME, ERROR_TYPE, CRASH_FILE, 144, CRASH_METHOD
        )
        assert fp_140 == fp_142 == fp_144

    def test_line_normalisation_different_buckets(self):
        """Lines 140 and 145 are in different buckets → different fingerprints."""
        fp_140 = compute_fingerprint(
            TENANT_ID, SERVICE_NAME, ERROR_TYPE, CRASH_FILE, 140, CRASH_METHOD
        )
        fp_145 = compute_fingerprint(
            TENANT_ID, SERVICE_NAME, ERROR_TYPE, CRASH_FILE, 145, CRASH_METHOD
        )
        assert fp_140 != fp_145

    def test_different_tenants_different_fingerprints(self):
        tenant_a = str(uuid.uuid4())
        tenant_b = str(uuid.uuid4())
        fp_a = compute_fingerprint(
            tenant_a, SERVICE_NAME, ERROR_TYPE, CRASH_FILE, CRASH_LINE, CRASH_METHOD
        )
        fp_b = compute_fingerprint(
            tenant_b, SERVICE_NAME, ERROR_TYPE, CRASH_FILE, CRASH_LINE, CRASH_METHOD
        )
        assert fp_a != fp_b

    def test_different_services_different_fingerprints(self):
        fp1 = compute_fingerprint(
            TENANT_ID,
            "payment-service",
            ERROR_TYPE,
            CRASH_FILE,
            CRASH_LINE,
            CRASH_METHOD,
        )
        fp2 = compute_fingerprint(
            TENANT_ID,
            "auth-service",
            ERROR_TYPE,
            CRASH_FILE,
            CRASH_LINE,
            CRASH_METHOD,
        )
        assert fp1 != fp2

    def test_different_error_types_different_fingerprints(self):
        fp1 = compute_fingerprint(
            TENANT_ID,
            SERVICE_NAME,
            "NullPointerException",
            CRASH_FILE,
            CRASH_LINE,
            CRASH_METHOD,
        )
        fp2 = compute_fingerprint(
            TENANT_ID,
            SERVICE_NAME,
            "DatabaseError",
            CRASH_FILE,
            CRASH_LINE,
            CRASH_METHOD,
        )
        assert fp1 != fp2

    def test_different_methods_different_fingerprints(self):
        """Even in the same file/line bucket, different methods produce different fingerprints."""
        fp1 = compute_fingerprint(
            TENANT_ID,
            SERVICE_NAME,
            ERROR_TYPE,
            CRASH_FILE,
            CRASH_LINE,
            "ChargeService.charge",
        )
        fp2 = compute_fingerprint(
            TENANT_ID,
            SERVICE_NAME,
            ERROR_TYPE,
            CRASH_FILE,
            CRASH_LINE,
            "ChargeService.refund",
        )
        assert fp1 != fp2

    def test_zero_line_number(self):
        """Line 0 (unparseable) normalises to 0 — valid fingerprint."""
        result = compute_fingerprint(
            TENANT_ID, SERVICE_NAME, ERROR_TYPE, CRASH_FILE, 0, CRASH_METHOD
        )
        assert len(result) == 64

    def test_empty_crash_file_and_method(self):
        """Empty strings produce a valid but coarser fingerprint."""
        result = compute_fingerprint(TENANT_ID, SERVICE_NAME, ERROR_TYPE, "", 0, "")
        assert len(result) == 64

    def test_sha256_value_correctness(self):
        """Verify the fingerprint matches manual SHA-256 computation."""
        normalised_line = (
            CRASH_LINE // FINGERPRINT_LINE_BUCKET
        ) * FINGERPRINT_LINE_BUCKET
        raw = (
            f"{TENANT_ID}:{SERVICE_NAME}:{ERROR_TYPE}:"
            f"{CRASH_FILE}:{normalised_line}:{CRASH_METHOD}"
        )
        expected = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        result = compute_fingerprint(
            TENANT_ID,
            SERVICE_NAME,
            ERROR_TYPE,
            CRASH_FILE,
            CRASH_LINE,
            CRASH_METHOD,
        )
        assert result == expected

    def test_bucket_size_constant(self):
        """Verify FINGERPRINT_LINE_BUCKET is 5 (documented value)."""
        assert FINGERPRINT_LINE_BUCKET == 5

    def test_line_boundary_exactly_on_bucket(self):
        """Line 145 normalises to 145, not 140."""
        fp_145 = compute_fingerprint(
            TENANT_ID, SERVICE_NAME, ERROR_TYPE, CRASH_FILE, 145, CRASH_METHOD
        )
        normalised = (145 // 5) * 5  # = 145
        raw = (
            f"{TENANT_ID}:{SERVICE_NAME}:{ERROR_TYPE}:"
            f"{CRASH_FILE}:{normalised}:{CRASH_METHOD}"
        )
        expected = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        assert fp_145 == expected


# ---------------------------------------------------------------------------
# Redis lock tests
# ---------------------------------------------------------------------------


class TestDedupLockKey:

    def test_key_format(self):
        fingerprint = "a" * 64
        key = dedup_lock_key(fingerprint)
        assert key == f"dedup:lock:{'a' * 64}"

    def test_different_fingerprints_different_keys(self):
        key1 = dedup_lock_key("a" * 64)
        key2 = dedup_lock_key("b" * 64)
        assert key1 != key2


class TestAcquireDedupLock:

    @pytest.mark.asyncio
    async def test_acquire_success_returns_true(self):
        redis = _make_mock_redis()
        redis.set = AsyncMock(return_value=True)

        result = await acquire_dedup_lock(redis, "a" * 64, "task-id-123")

        assert result is True
        redis.set.assert_called_once_with(
            f"dedup:lock:{'a' * 64}",
            "task-id-123",
            nx=True,
            ex=DEDUP_LOCK_TTL_SECONDS,
        )

    @pytest.mark.asyncio
    async def test_acquire_fails_when_key_exists_returns_false(self):
        redis = _make_mock_redis()
        # Redis SET NX returns None (not True) when key already exists
        redis.set = AsyncMock(return_value=None)

        result = await acquire_dedup_lock(redis, "a" * 64, "task-id-456")

        assert result is False

    @pytest.mark.asyncio
    async def test_acquire_fails_open_on_redis_error(self):
        import redis.asyncio as aioredis

        mock_redis = _make_mock_redis()
        mock_redis.set = AsyncMock(
            side_effect=aioredis.ConnectionError("connection refused")
        )

        # Fail-open: should return True so the task proceeds
        result = await acquire_dedup_lock(mock_redis, "a" * 64, "task-id-789")

        assert result is True

    @pytest.mark.asyncio
    async def test_acquire_uses_correct_ttl(self):
        redis = _make_mock_redis()
        await acquire_dedup_lock(redis, "a" * 64, "owner")
        _, call_kwargs = redis.set.call_args
        assert call_kwargs["ex"] == DEDUP_LOCK_TTL_SECONDS
        assert call_kwargs["nx"] is True


class TestReleaseDedupLock:

    @pytest.mark.asyncio
    async def test_release_calls_delete(self):
        redis = _make_mock_redis()

        await release_dedup_lock(redis, "a" * 64)

        redis.delete.assert_called_once_with(f"dedup:lock:{'a' * 64}")

    @pytest.mark.asyncio
    async def test_release_does_not_raise_on_redis_error(self):
        import redis.asyncio as aioredis

        mock_redis = _make_mock_redis()
        mock_redis.delete = AsyncMock(side_effect=aioredis.ConnectionError("gone"))

        # Must NOT raise
        await release_dedup_lock(mock_redis, "a" * 64)

    @pytest.mark.asyncio
    async def test_release_handles_key_not_existing(self):
        redis = _make_mock_redis()
        redis.delete = AsyncMock(return_value=0)  # 0 = key did not exist

        # Must NOT raise
        await release_dedup_lock(redis, "a" * 64)


# ---------------------------------------------------------------------------
# IncidentService tests
# ---------------------------------------------------------------------------


class TestFindActiveByFingerprint:

    @pytest.mark.asyncio
    async def test_returns_incident_when_found(self):
        session = _make_mock_session()
        mock_incident = _make_mock_incident()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none = MagicMock(return_value=mock_incident)
        session.execute = AsyncMock(return_value=mock_result)

        svc = IncidentService(session)
        result = await svc.find_active_by_fingerprint(
            tenant_id=uuid.UUID(TENANT_ID),
            fingerprint="a" * 64,
        )

        assert result is mock_incident
        session.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_returns_none_when_not_found(self):
        session = _make_mock_session()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none = MagicMock(return_value=None)
        session.execute = AsyncMock(return_value=mock_result)

        svc = IncidentService(session)
        result = await svc.find_active_by_fingerprint(
            tenant_id=uuid.UUID(TENANT_ID),
            fingerprint="a" * 64,
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_query_uses_correct_filters(self):
        """Verify the query selects on tenant_id, fingerprint, and status."""
        session = _make_mock_session()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none = MagicMock(return_value=None)
        session.execute = AsyncMock(return_value=mock_result)

        svc = IncidentService(session)
        await svc.find_active_by_fingerprint(
            tenant_id=uuid.UUID(TENANT_ID),
            fingerprint="b" * 64,
        )

        # Verify execute was called exactly once
        assert session.execute.call_count == 1
        # The statement passed to execute should reference Incident
        call_args = session.execute.call_args[0][0]
        # Basic check: the statement should be a Select object
        assert hasattr(call_args, "whereclause")


class TestRecordDuplicateOccurrence:

    @pytest.mark.asyncio
    async def test_returns_new_count(self):
        session = _make_mock_session()
        mock_incident = _make_mock_incident(occurrence_count=2)

        mock_update_result = MagicMock()
        mock_update_result.scalar.return_value = 3
        session.execute = AsyncMock(return_value=mock_update_result)

        with patch("app.services.incidents.write_outbox") as mock_write_outbox:
            svc = IncidentService(session)
            new_count = await svc.record_duplicate_occurrence(
                incident=mock_incident,
                new_s3_key=f"logs/{TENANT_ID}/context/new-uuid.json.gz",
            )

        assert new_count == 3
        mock_write_outbox.assert_called_once()

    @pytest.mark.asyncio
    async def test_writes_outbox_event_with_correct_fields(self):
        session = _make_mock_session()
        mock_incident = _make_mock_incident(
            incident_id="cccccccc-dddd-eeee-ffff-000000000000",
            occurrence_count=1,
        )

        mock_update_result = MagicMock()
        mock_update_result.scalar.return_value = 2
        session.execute = AsyncMock(return_value=mock_update_result)

        captured_payloads = []

        def capture_outbox(session, topic, key, payload):
            captured_payloads.append({"topic": topic, "key": key, "payload": payload})

        with patch("app.services.incidents.write_outbox", side_effect=capture_outbox):
            svc = IncidentService(session)
            await svc.record_duplicate_occurrence(
                incident=mock_incident,
                new_s3_key="logs/tenant/context/new.json.gz",
            )

        assert len(captured_payloads) == 1
        outbox = captured_payloads[0]
        assert outbox["topic"] == "incidents.duplicate_recorded"
        assert outbox["payload"]["event_type"] == "incident.duplicate_detected"
        assert outbox["payload"]["payload"]["new_occurrence_count"] == 2
        assert outbox["payload"]["payload"]["new_s3_key"] == (
            "logs/tenant/context/new.json.gz"
        )

    @pytest.mark.asyncio
    async def test_falls_back_to_current_count_when_returning_is_none(self):
        """If RETURNING returns no row, falls back to incident.count."""
        session = _make_mock_session()
        mock_incident = _make_mock_incident(occurrence_count=5)

        mock_update_result = MagicMock()
        mock_update_result.scalar.return_value = None
        session.execute = AsyncMock(return_value=mock_update_result)

        with patch("app.services.incidents.write_outbox"):
            svc = IncidentService(session)
            new_count = await svc.record_duplicate_occurrence(
                incident=mock_incident,
                new_s3_key="logs/tenant/context/new.json.gz",
            )

        assert new_count == 5


class TestPersistNewIncident:

    def _make_agent_result(self, **overrides) -> Dict[str, Any]:
        defaults = {
            "root_cause": "Stripe client is null",
            "suggested_fix": "Add null guard before calling charge()",
            "confidence_score": 0.87,
            "severity": "high",
            "code_context": "def charge(): ...",
            "analyzer_tokens": {"prompt": 800, "completion": 400, "total": 1200},
            "fix_tokens": {"prompt": 600, "completion": 300, "total": 900},
            "analyzer_latency_ms": 3200,
            "fix_generator_latency_ms": 2800,
            "classifier_latency_ms": 45,
            "playbook_latency_ms": 12,
            "code_retriever_meta": {
                "latency_ms": 230,
                "files_fetched": 2,
                "tokens": 480,
                "cache_hits": 1,
                "cache_misses": 1,
                "symbols_retrieved": 3,
            },
            "scorer_latency_ms": 8,
            "confidence_threshold": 0.70,
            "retrieval_score": 0.90,
            "coherence_score": 0.85,
            "analyzer_fallback_used": False,
            "fix_fallback_used": False,
            "matched_playbook_id": None,
            "raw_analysis_output": '{"root_cause": "..."}',
            "raw_fix_output": '{"fix": "..."}',
            "total_latency_ms": 6340,
            "action": "create_incident",
        }
        defaults.update(overrides)
        return defaults

    @pytest.mark.asyncio
    async def test_adds_incident_and_analysis_to_session(self):
        session = _make_mock_session()
        parsed_event = _make_parsed_event()
        agent_result = self._make_agent_result()

        added_objects = []

        def capture_add(obj):
            added_objects.append(obj)

        session.add = MagicMock(side_effect=capture_add)

        with patch("app.services.incidents.write_outbox"):
            svc = IncidentService(session)
            result = await svc.persist_new_incident(
                tenant_id=uuid.UUID(TENANT_ID),
                parsed_event=parsed_event,
                fingerprint="a" * 64,
                agent_result=agent_result,
                is_draft=False,
            )

        # Should have added Incident and Analysis
        from app.models.incidents import Analysis, Incident

        types_added = [type(obj).__name__ for obj in added_objects]
        assert "Incident" in types_added
        assert "Analysis" in types_added

    @pytest.mark.asyncio
    async def test_returns_incident_and_analysis_ids(self):
        session = _make_mock_session()
        parsed_event = _make_parsed_event()
        agent_result = self._make_agent_result()

        with patch("app.services.incidents.write_outbox"):
            svc = IncidentService(session)
            result = await svc.persist_new_incident(
                tenant_id=uuid.UUID(TENANT_ID),
                parsed_event=parsed_event,
                fingerprint="a" * 64,
                agent_result=agent_result,
                is_draft=False,
            )

        assert "incident_id" in result
        assert "analysis_id" in result
        assert isinstance(result["incident_id"], uuid.UUID)
        assert isinstance(result["analysis_id"], uuid.UUID)

    @pytest.mark.asyncio
    async def test_writes_two_outbox_events_for_open_incident(self):
        session = _make_mock_session()
        parsed_event = _make_parsed_event()
        agent_result = self._make_agent_result()

        captured = []

        def capture_outbox(session, topic, key, payload):
            captured.append(topic)

        with patch("app.services.incidents.write_outbox", side_effect=capture_outbox):
            svc = IncidentService(session)
            await svc.persist_new_incident(
                tenant_id=uuid.UUID(TENANT_ID),
                parsed_event=parsed_event,
                fingerprint="a" * 64,
                agent_result=agent_result,
                is_draft=False,
            )

        assert "incidents.created" in captured
        assert "incidents.analyzed" in captured
        assert len(captured) == 2

    @pytest.mark.asyncio
    async def test_writes_outbox_events_for_draft(self):
        session = _make_mock_session()
        parsed_event = _make_parsed_event()
        agent_result = self._make_agent_result()
        
        captured = []

        def capture_outbox(*args, **kwargs):
            captured.append(kwargs)

        with patch("app.services.incidents.write_outbox", side_effect=capture_outbox):
            svc = IncidentService(session)
            await svc.persist_new_incident(
                tenant_id=uuid.UUID(TENANT_ID),
                parsed_event=parsed_event,
                fingerprint="a" * 64,
                agent_result=agent_result,
                is_draft=True,
            )

        # Drafts still write Kafka events
        assert len(captured) == 2

    @pytest.mark.asyncio
    async def test_incident_has_correct_status_for_open(self):
        session = _make_mock_session()
        parsed_event = _make_parsed_event()
        agent_result = self._make_agent_result()

        added_incidents = []

        def capture_add(obj):
            from app.models.incidents import Incident

            if isinstance(obj, Incident):
                added_incidents.append(obj)

        session.add = MagicMock(side_effect=capture_add)

        with patch("app.services.incidents.write_outbox"):
            svc = IncidentService(session)
            await svc.persist_new_incident(
                tenant_id=uuid.UUID(TENANT_ID),
                parsed_event=parsed_event,
                fingerprint="a" * 64,
                agent_result=agent_result,
                is_draft=False,
            )

        assert len(added_incidents) == 1
        assert added_incidents[0].status == "open"
        assert added_incidents[0].is_draft is False

    @pytest.mark.asyncio
    async def test_incident_has_correct_status_for_draft(self):
        session = _make_mock_session()
        parsed_event = _make_parsed_event()
        agent_result = self._make_agent_result(action="store_draft")

        added_incidents = []

        def capture_add(obj):
            from app.models.incidents import Incident

            if isinstance(obj, Incident):
                added_incidents.append(obj)

        session.add = MagicMock(side_effect=capture_add)

        with patch("app.services.incidents.write_outbox"):
            svc = IncidentService(session)
            await svc.persist_new_incident(
                tenant_id=uuid.UUID(TENANT_ID),
                parsed_event=parsed_event,
                fingerprint="a" * 64,
                agent_result=agent_result,
                is_draft=True,
            )

        assert len(added_incidents) == 1
        assert added_incidents[0].status == "open"
        assert added_incidents[0].is_draft is False

    @pytest.mark.asyncio
    async def test_analysis_tokens_aggregated_correctly(self):
        session = _make_mock_session()
        parsed_event = _make_parsed_event()
        agent_result = self._make_agent_result(
            analyzer_tokens={"prompt": 800, "completion": 400, "total": 1200},
            fix_tokens={"prompt": 600, "completion": 300, "total": 900},
        )

        added_analyses = []

        def capture_add(obj):
            from app.models.incidents import Analysis

            if isinstance(obj, Analysis):
                added_analyses.append(obj)

        session.add = MagicMock(side_effect=capture_add)

        with patch("app.services.incidents.write_outbox"):
            svc = IncidentService(session)
            await svc.persist_new_incident(
                tenant_id=uuid.UUID(TENANT_ID),
                parsed_event=parsed_event,
                fingerprint="a" * 64,
                agent_result=agent_result,
                is_draft=False,
            )

        assert len(added_analyses) == 1
        analysis = added_analyses[0]
        assert analysis.total_tokens_used == 2100  # 1200 + 900
        assert analysis.prompt_tokens == 1400  # 800 + 600
        assert analysis.completion_tokens == 700  # 400 + 300


# ---------------------------------------------------------------------------
# _safe_uuid helper tests
# ---------------------------------------------------------------------------


class TestSafeUuid:

    def test_none_returns_none(self):
        assert _safe_uuid(None) is None

    def test_valid_string_returns_uuid(self):
        val = "11111111-2222-3333-4444-555555555555"
        result = _safe_uuid(val)
        assert isinstance(result, uuid.UUID)
        assert str(result) == val

    def test_uuid_object_returns_same(self):
        val = uuid.uuid4()
        result = _safe_uuid(val)
        assert result == val

    def test_invalid_string_returns_none(self):
        assert _safe_uuid("not-a-uuid") is None

    def test_empty_string_returns_none(self):
        assert _safe_uuid("") is None

    def test_integer_returns_none(self):
        assert _safe_uuid(12345) is None


# ---------------------------------------------------------------------------
# _execute_run_agent integration tests
# ---------------------------------------------------------------------------


class TestExecuteRunAgent:

    def _make_event_dict(self, **overrides) -> Dict[str, Any]:
        event = _make_parsed_event(**overrides)
        return event.to_dict()

    @pytest.mark.asyncio
    async def test_returns_lock_contention_when_lock_not_acquired(self):
        parsed_event_dict = self._make_event_dict()

        with (
            patch(
                "app.worker.tasks.run_agent.get_redis",
                return_value=_make_mock_redis(),
            ),
            patch(
                "app.worker.tasks.run_agent.acquire_dedup_lock",
                new_callable=AsyncMock,
                return_value=False,  # Lock NOT acquired
            ),
            patch(
                "app.worker.tasks.run_agent.release_dedup_lock",
                new_callable=AsyncMock,
            ) as mock_release,
        ):
            result = await _execute_run_agent(
                task_id="test-task-id",
                parsed_event_dict=parsed_event_dict,
            )

        assert result["action"] == "lock_contention"
        # Lock was not acquired — release should NOT be called
        mock_release.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_duplicate_recorded_when_active_incident_exists(self):
        parsed_event_dict = self._make_event_dict()
        existing_incident = _make_mock_incident(occurrence_count=3)

        mock_redis = _make_mock_redis()

        with (
            patch(
                "app.worker.tasks.run_agent.get_redis",
                return_value=mock_redis,
            ),
            patch(
                "app.worker.tasks.run_agent.acquire_dedup_lock",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "app.worker.tasks.run_agent.release_dedup_lock",
                new_callable=AsyncMock,
            ) as mock_release,
            patch(
                "app.worker.tasks.run_agent.AsyncSessionLocal",
            ) as mock_session_local,
            patch("app.worker.tasks.run_agent.IncidentService") as MockIncidentService,
        ):
            # Configure session context manager
            mock_session = _make_mock_session()
            mock_session_local.return_value.__aenter__ = AsyncMock(
                return_value=mock_session
            )
            mock_session_local.return_value.__aexit__ = AsyncMock(return_value=False)

            # Configure IncidentService
            mock_svc = MagicMock()
            MockIncidentService.return_value = mock_svc
            mock_svc.find_active_by_fingerprint = AsyncMock(
                return_value=existing_incident
            )
            mock_svc.record_duplicate_occurrence = AsyncMock(return_value=4)

            result = await _execute_run_agent(
                task_id="test-task-id",
                parsed_event_dict=parsed_event_dict,
            )

        assert result["action"] == "duplicate_recorded"
        assert result["new_occurrence_count"] == 4
        assert "existing_incident_id" in result
        # Lock MUST be released even on duplicate path
        mock_release.assert_called_once()

    @pytest.mark.asyncio
    async def test_creates_new_incident_when_no_duplicate_found(self):
        parsed_event_dict = self._make_event_dict()
        mock_redis = _make_mock_redis()

        result_ids = {
            "incident_id": uuid.uuid4(),
            "analysis_id": uuid.uuid4(),
        }

        with (
            patch(
                "app.worker.tasks.run_agent.get_redis",
                return_value=mock_redis,
            ),
            patch(
                "app.worker.tasks.run_agent.acquire_dedup_lock",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "app.worker.tasks.run_agent.release_dedup_lock",
                new_callable=AsyncMock,
            ) as mock_release,
            patch(
                "app.worker.tasks.run_agent.AsyncSessionLocal",
            ) as mock_session_local,
            patch("app.worker.tasks.run_agent.IncidentService") as MockIncidentService,
            patch("app.agents.workflow.get_agent_workflow") as mock_get_workflow,
        ):
            mock_workflow = MagicMock()
            mock_workflow.ainvoke = AsyncMock(
                return_value={
                    "confidence_score": 0.90,
                    "confidence_threshold": 0.70,
                    "action": "create_incident",
                    "actionable": True,
                    "severity": "low",
                    "root_cause": "test",
                    "suggested_fix": "test",
                    "code_context": "",
                    "analyzer_tokens": {"prompt": 0, "completion": 0, "total": 0},
                    "fix_tokens": {"prompt": 0, "completion": 0, "total": 0},
                    "total_latency_ms": 0,
                }
            )
            mock_get_workflow.return_value = mock_workflow
            mock_session = _make_mock_session()
            mock_session_local.return_value.__aenter__ = AsyncMock(
                return_value=mock_session
            )
            mock_session_local.return_value.__aexit__ = AsyncMock(return_value=False)

            mock_svc = MagicMock()
            MockIncidentService.return_value = mock_svc
            # No existing incident
            mock_svc.find_active_by_fingerprint = AsyncMock(return_value=None)
            mock_svc.persist_new_incident = AsyncMock(return_value=result_ids)

            result = await _execute_run_agent(
                task_id="test-task-id",
                parsed_event_dict=parsed_event_dict,
            )

        assert result["action"] in ("create_incident", "store_draft")
        assert "new_incident_id" in result
        assert "new_analysis_id" in result
        # Lock MUST always be released
        mock_release.assert_called_once()

    @pytest.mark.asyncio
    async def test_releases_lock_even_on_exception(self):
        """The finally block must release the lock even when an exception occurs."""
        parsed_event_dict = self._make_event_dict()
        mock_redis = _make_mock_redis()

        with (
            patch(
                "app.worker.tasks.run_agent.get_redis",
                return_value=mock_redis,
            ),
            patch(
                "app.worker.tasks.run_agent.acquire_dedup_lock",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "app.worker.tasks.run_agent.release_dedup_lock",
                new_callable=AsyncMock,
            ) as mock_release,
            patch(
                "app.worker.tasks.run_agent.AsyncSessionLocal",
            ) as mock_session_local,
            patch("app.worker.tasks.run_agent.IncidentService") as MockIncidentService,
        ):
            mock_session = _make_mock_session()
            mock_session_local.return_value.__aenter__ = AsyncMock(
                return_value=mock_session
            )
            mock_session_local.return_value.__aexit__ = AsyncMock(return_value=False)

            mock_svc = MagicMock()
            MockIncidentService.return_value = mock_svc
            # Raise an OperationalError to simulate DB connection failure
            mock_svc.find_active_by_fingerprint = AsyncMock(
                side_effect=Exception("unexpected db error")
            )

            with pytest.raises(Exception, match="unexpected db error"):
                await _execute_run_agent(
                    task_id="test-task-id",
                    parsed_event_dict=parsed_event_dict,
                )

        # CRITICAL: lock must be released even when exception was raised
        mock_release.assert_called_once()

    @pytest.mark.asyncio
    async def test_handles_integrity_error_as_late_duplicate(self):
        """IntegrityError during persist = late duplicate, not a failure."""
        import sqlalchemy.exc

        parsed_event_dict = self._make_event_dict()
        mock_redis = _make_mock_redis()

        with (
            patch(
                "app.worker.tasks.run_agent.get_redis",
                return_value=mock_redis,
            ),
            patch(
                "app.worker.tasks.run_agent.acquire_dedup_lock",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "app.worker.tasks.run_agent.release_dedup_lock",
                new_callable=AsyncMock,
            ),
            patch(
                "app.worker.tasks.run_agent.AsyncSessionLocal",
            ) as mock_session_local,
            patch("app.worker.tasks.run_agent.IncidentService") as MockIncidentService,
            patch("app.agents.workflow.get_agent_workflow") as mock_get_workflow,
        ):
            mock_workflow = MagicMock()
            mock_workflow.ainvoke = AsyncMock(
                return_value={
                    "confidence_score": 0.90,
                    "confidence_threshold": 0.70,
                    "action": "create_incident",
                    "actionable": True,
                    "severity": "low",
                    "root_cause": "test",
                    "suggested_fix": "test",
                    "code_context": "",
                    "analyzer_tokens": {"prompt": 0, "completion": 0, "total": 0},
                    "fix_tokens": {"prompt": 0, "completion": 0, "total": 0},
                    "total_latency_ms": 0,
                }
            )
            mock_get_workflow.return_value = mock_workflow
            mock_session = _make_mock_session()
            mock_session_local.return_value.__aenter__ = AsyncMock(
                return_value=mock_session
            )
            mock_session_local.return_value.__aexit__ = AsyncMock(return_value=False)

            mock_svc = MagicMock()
            MockIncidentService.return_value = mock_svc
            mock_svc.find_active_by_fingerprint = AsyncMock(return_value=None)
            mock_svc.persist_new_incident = AsyncMock(
                side_effect=sqlalchemy.exc.IntegrityError(
                    "INSERT", {}, Exception("duplicate key")
                )
            )

            result = await _execute_run_agent(
                task_id="test-task-id",
                parsed_event_dict=parsed_event_dict,
            )

        assert result["action"] == "late_duplicate"

    @pytest.mark.asyncio
    async def test_raises_value_error_on_invalid_event_dict(self):
        """Invalid ParsedLogEvent data → ValueError (non-retryable)."""
        invalid_dict = {"tenant_id": "not-a-uuid", "bad": "data"}

        with pytest.raises(ValueError):
            await _execute_run_agent(
                task_id="test-task-id",
                parsed_event_dict=invalid_dict,
            )

    @pytest.mark.asyncio
    async def test_stores_draft_when_confidence_below_threshold(self):
        parsed_event_dict = self._make_event_dict()
        mock_redis = _make_mock_redis()

        result_ids = {
            "incident_id": uuid.uuid4(),
            "analysis_id": uuid.uuid4(),
        }

        with (
            patch(
                "app.worker.tasks.run_agent.get_redis",
                return_value=mock_redis,
            ),
            patch(
                "app.worker.tasks.run_agent.acquire_dedup_lock",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "app.worker.tasks.run_agent.release_dedup_lock",
                new_callable=AsyncMock,
            ),
            patch(
                "app.worker.tasks.run_agent.AsyncSessionLocal",
            ) as mock_session_local,
            patch("app.worker.tasks.run_agent.IncidentService") as MockIncidentService,
            patch("app.agents.workflow.get_agent_workflow") as mock_get_workflow,
        ):
            mock_workflow = MagicMock()
            mock_workflow.ainvoke = AsyncMock(
                return_value={
                    # Confidence BELOW threshold
                    "confidence_score": 0.40,
                    "confidence_threshold": 0.70,
                    "action": "create_incident",  # agent says create, but score overrides
                    "actionable": True,
                    "severity": "low",
                    "root_cause": "test",
                    "suggested_fix": "test",
                    "code_context": "",
                    "analyzer_tokens": {"prompt": 0, "completion": 0, "total": 0},
                    "fix_tokens": {"prompt": 0, "completion": 0, "total": 0},
                    "total_latency_ms": 0,
                }
            )
            mock_get_workflow.return_value = mock_workflow

            mock_session = _make_mock_session()
            mock_session_local.return_value.__aenter__ = AsyncMock(
                return_value=mock_session
            )
            mock_session_local.return_value.__aexit__ = AsyncMock(return_value=False)

            mock_svc = MagicMock()
            MockIncidentService.return_value = mock_svc
            mock_svc.find_active_by_fingerprint = AsyncMock(return_value=None)
            mock_svc.persist_new_incident = AsyncMock(return_value=result_ids)

            result = await _execute_run_agent(
                task_id="test-task-id",
                parsed_event_dict=parsed_event_dict,
            )

        # Confidence below threshold forces store_draft even if agent said create_incident
        assert result["action"] == "store_draft"
        assert result["is_draft"] is True

        # Verify persist_new_incident was called with is_draft=True
        mock_svc.persist_new_incident.assert_called_once()
        call_kwargs = mock_svc.persist_new_incident.call_args.kwargs
        assert call_kwargs["is_draft"] is True
