"""
fastapi/tests/unit/test_parse_log.py

Unit tests for the parse_log Celery task helper functions.

All tests are pure (no network, no DB, no Redis, no S3).
They test the extraction and parsing logic in isolation by calling
the private helper functions directly.

Test coverage:
  - _extract_error_type: Java, Python, JS, Go, generic, empty
  - _find_trigger_log: highest severity, tiebreak on last, empty buffer
  - _parse_stack_frames_from_list: structured format
  - _parse_stack_frames_from_java_text: Java 'at' format
  - _parse_stack_frames_from_python_text: Python 'File' format
  - _parse_stack_frames: format dispatch
  - _build_parsed_event: end-to-end integration
  - ParsedLogEvent.to_dict / from_dict: round-trip serialisation
  - StackFrame: line coercion, cap at 20
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

import pytest

from app.schemas.parse_log import ParsedLogEvent, StackFrame
from app.worker.tasks.parse_log import (
    _MAX_STACK_FRAMES,
    _build_parsed_event,
    _decompress_and_parse,
    _extract_error_type,
    _find_trigger_log,
    _parse_stack_frames,
    _parse_stack_frames_from_java_text,
    _parse_stack_frames_from_list,
    _parse_stack_frames_from_python_text,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_log_entry(
    level: str = "info",
    message: str = "test message",
    stack_trace: Any = None,
) -> Dict[str, Any]:
    entry: Dict[str, Any] = {"level": level, "message": message, "seq": 1}
    if stack_trace is not None:
        entry["stack_trace"] = stack_trace
    return entry


def _make_java_stack_trace() -> str:
    return (
        "java.lang.NullPointerException: cannot invoke charge() on null\n"
        "\tat com.neuralops.payment.ChargeService.charge(ChargeService.java:142)\n"
        "\tat com.neuralops.api.PaymentController.processPayment(PaymentController.java:78)\n"
        "\tat com.neuralops.api.PaymentController.handleRequest(PaymentController.java:45)\n"
    )


def _make_python_stack_trace() -> str:
    return (
        "Traceback (most recent call last):\n"
        '  File "src/payment/charge.py", line 78, in process_payment\n'
        "    result = charge_service.charge(amount)\n"
        '  File "src/payment/charge_service.py", line 142, in charge\n'
        "    return self.client.charge(amount)\n"
    )


def _make_structured_frames() -> List[Dict[str, Any]]:
    return [
        {
            "file": "src/payment/ChargeService.java",
            "line": 142,
            "method": "ChargeService.charge",
            "module": "com.neuralops.payment",
        },
        {
            "file": "src/api/PaymentController.java",
            "line": 78,
            "method": "PaymentController.processPayment",
            "module": "com.neuralops.api",
        },
    ]


# ---------------------------------------------------------------------------
# _extract_error_type tests
# ---------------------------------------------------------------------------


class TestExtractErrorType:

    def test_java_exception_with_package(self):
        result = _extract_error_type(
            "com.neuralops.payment.ChargeException: stripe timeout"
        )
        assert result == "ChargeException"

    def test_java_null_pointer_exception(self):
        result = _extract_error_type(
            "NullPointerException: cannot invoke charge() on null"
        )
        assert result == "NullPointerException"

    def test_python_value_error(self):
        result = _extract_error_type(
            "ValueError: invalid literal for int() with base 10: 'abc'"
        )
        assert result == "ValueError"

    def test_python_attribute_error(self):
        result = _extract_error_type(
            "AttributeError: 'NoneType' object has no attribute 'charge'"
        )
        assert result == "AttributeError"

    def test_javascript_type_error(self):
        result = _extract_error_type(
            "TypeError: Cannot read properties of undefined (reading 'charge')"
        )
        assert result == "TypeError"

    def test_go_panic(self):
        result = _extract_error_type(
            "panic: runtime error: index out of range [5] with length 3"
        )
        # Go panics map to generic extraction
        assert result != "UnknownError"

    def test_database_error(self):
        result = _extract_error_type("DatabaseError: connection refused")
        assert result == "DatabaseError"

    def test_empty_message(self):
        assert _extract_error_type("") == "UnknownError"

    def test_none_message(self):
        assert _extract_error_type(None) == "UnknownError"

    def test_plain_message_no_exception(self):
        result = _extract_error_type("something went wrong in the system")
        assert result == "UnknownError"

    def test_result_capped_at_255_chars(self):
        # Construct an artificially long exception name
        long_name = "A" * 300 + "Exception"
        result = _extract_error_type(long_name + ": message")
        assert len(result) <= 255


# ---------------------------------------------------------------------------
# _find_trigger_log tests
# ---------------------------------------------------------------------------


class TestFindTriggerLog:

    def test_finds_error_over_info(self):
        entries = [
            _make_log_entry("info", "request started"),
            _make_log_entry("error", "NullPointerException"),
            _make_log_entry("info", "request ended"),
        ]
        result = _find_trigger_log(entries)
        assert result["level"] == "error"
        assert result["message"] == "NullPointerException"

    def test_finds_critical_over_error(self):
        entries = [
            _make_log_entry("error", "first error"),
            _make_log_entry("critical", "system crash"),
            _make_log_entry("error", "second error"),
        ]
        result = _find_trigger_log(entries)
        assert result["level"] == "critical"

    def test_tiebreak_returns_last_entry(self):
        """When multiple entries share the highest severity, return the last."""
        entries = [
            _make_log_entry("error", "first error"),
            _make_log_entry("error", "second error"),
            _make_log_entry("error", "third error"),
        ]
        result = _find_trigger_log(entries)
        assert result["message"] == "third error"

    def test_empty_entries_returns_empty_dict(self):
        assert _find_trigger_log([]) == {}

    def test_all_info_returns_last_info(self):
        entries = [
            _make_log_entry("info", "msg 1"),
            _make_log_entry("info", "msg 2"),
        ]
        result = _find_trigger_log(entries)
        assert result["message"] == "msg 2"

    def test_skips_non_dict_entries(self):
        entries = [
            "not a dict",
            None,
            _make_log_entry("error", "real error"),
        ]
        result = _find_trigger_log(entries)
        assert result["message"] == "real error"

    def test_only_scans_first_500_entries(self):
        """Verify the 500-entry cap by placing an error at position 501."""
        entries = [_make_log_entry("info", f"msg {i}") for i in range(501)]
        entries[500] = _make_log_entry("critical", "should not find this")
        result = _find_trigger_log(entries)
        assert result.get("level") != "critical"


# ---------------------------------------------------------------------------
# Stack frame parsing tests
# ---------------------------------------------------------------------------


class TestParseStackFramesFromList:

    def test_parses_structured_frames(self):
        frames = _parse_stack_frames_from_list(_make_structured_frames())
        assert len(frames) == 2
        assert frames[0].file == "src/payment/ChargeService.java"
        assert frames[0].line == 142
        assert frames[0].method == "ChargeService.charge"
        assert frames[0].module == "com.neuralops.payment"

    def test_coerces_string_line_numbers(self):
        raw = [{"file": "Test.java", "line": "99", "method": "test", "module": ""}]
        frames = _parse_stack_frames_from_list(raw)
        assert frames[0].line == 99

    def test_handles_missing_optional_fields(self):
        raw = [{"file": "Test.java"}]
        frames = _parse_stack_frames_from_list(raw)
        assert len(frames) == 1
        assert frames[0].line == 0
        assert frames[0].method == ""

    def test_caps_at_max_frames(self):
        raw = [
            {"file": f"File{i}.java", "line": i, "method": f"method{i}"}
            for i in range(30)
        ]
        frames = _parse_stack_frames_from_list(raw)
        assert len(frames) == _MAX_STACK_FRAMES

    def test_skips_non_dict_elements(self):
        raw = [
            {"file": "Good.java", "line": 1, "method": "good"},
            "bad string element",
            None,
            {"file": "AlsoGood.java", "line": 2, "method": "alsoGood"},
        ]
        frames = _parse_stack_frames_from_list(raw)
        assert len(frames) == 2

    def test_empty_list(self):
        assert _parse_stack_frames_from_list([]) == []


class TestParseStackFramesFromJavaText:

    def test_parses_java_at_frames(self):
        frames = _parse_stack_frames_from_java_text(_make_java_stack_trace())
        assert len(frames) == 3
        assert frames[0].line == 142
        assert "ChargeService" in frames[0].method
        assert "ChargeService.java" in frames[0].file

    def test_no_at_frames_returns_empty(self):
        frames = _parse_stack_frames_from_java_text("no frames here")
        assert frames == []

    def test_handles_frame_without_line_number(self):
        text = "\tat com.example.Service.method(Service.java)\n"
        frames = _parse_stack_frames_from_java_text(text)
        assert len(frames) == 1
        assert frames[0].line == 0

    def test_caps_at_max_frames(self):
        lines = [
            f"\tat com.example.Service.method{i}(Service.java:{i})\n" for i in range(30)
        ]
        frames = _parse_stack_frames_from_java_text("".join(lines))
        assert len(frames) == _MAX_STACK_FRAMES


class TestParseStackFramesFromPythonText:

    def test_parses_python_file_frames(self):
        frames = _parse_stack_frames_from_python_text(_make_python_stack_trace())
        assert len(frames) == 2
        # Python frames are reversed so index 0 is the innermost frame
        assert frames[0].line == 142
        assert frames[0].file == "src/payment/charge_service.py"

    def test_no_file_lines_returns_empty(self):
        frames = _parse_stack_frames_from_python_text("no traceback here")
        assert frames == []

    def test_reversal_makes_innermost_first(self):
        text = (
            '  File "outer.py", line 10, in outer_func\n'
            '  File "inner.py", line 99, in inner_func\n'
        )
        frames = _parse_stack_frames_from_python_text(text)
        assert frames[0].file == "inner.py"
        assert frames[0].line == 99


class TestParseStackFramesDispatch:

    def test_dispatches_list_to_list_parser(self):
        raw = _make_structured_frames()
        frames = _parse_stack_frames(raw)
        assert len(frames) == 2
        assert frames[0].file == "src/payment/ChargeService.java"

    def test_dispatches_java_text_to_java_parser(self):
        frames = _parse_stack_frames(_make_java_stack_trace())
        assert len(frames) > 0
        assert frames[0].line == 142

    def test_dispatches_python_text_to_python_parser(self):
        frames = _parse_stack_frames(_make_python_stack_trace())
        assert len(frames) > 0

    def test_none_returns_empty(self):
        assert _parse_stack_frames(None) == []

    def test_empty_string_returns_empty(self):
        assert _parse_stack_frames("") == []

    def test_empty_list_returns_empty(self):
        assert _parse_stack_frames([]) == []

    def test_unknown_type_returns_empty(self):
        assert _parse_stack_frames(42) == []


# ---------------------------------------------------------------------------
# _build_parsed_event integration tests
# ---------------------------------------------------------------------------


class TestBuildParsedEvent:

    def _base_kwargs(self) -> Dict[str, Any]:
        return {
            "tenant_id": "11111111-2222-3333-4444-555555555555",
            "incident_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "s3_path": "logs/tenant-uuid/context/incident-uuid.json.gz",
            "service_name": "payment-service",
            "environment": "production",
        }

    def test_full_event_with_structured_frames(self):
        entries = [
            _make_log_entry("info", "payment started"),
            _make_log_entry(
                "error",
                "NullPointerException: cannot invoke charge() on null",
                stack_trace=_make_structured_frames(),
            ),
        ]
        event = _build_parsed_event(**self._base_kwargs(), entries=entries)

        assert event.error_type == "NullPointerException"
        assert event.severity == "error"
        assert event.crash_file == "src/payment/ChargeService.java"
        assert event.crash_line == 142
        assert event.crash_method == "ChargeService.charge"
        assert len(event.stack_frames) == 2
        assert event.context_log_count == 2
        assert event.service_name == "payment-service"
        assert event.environment == "production"

    def test_event_with_java_text_stack_trace(self):
        entries = [
            _make_log_entry(
                "error",
                "NullPointerException: null",
                stack_trace=_make_java_stack_trace(),
            ),
        ]
        event = _build_parsed_event(**self._base_kwargs(), entries=entries)

        assert event.error_type == "NullPointerException"
        assert event.crash_line == 142

    def test_empty_entries_produces_minimal_event(self):
        event = _build_parsed_event(**self._base_kwargs(), entries=[])

        assert event.error_type == "UnknownError"
        assert event.severity == "unknown"
        assert event.crash_file == ""
        assert event.crash_line == 0
        assert event.stack_frames == []
        assert event.context_log_count == 0

    def test_no_stack_trace_in_trigger(self):
        entries = [
            _make_log_entry("error", "ValueError: bad input"),
        ]
        event = _build_parsed_event(**self._base_kwargs(), entries=entries)

        assert event.error_type == "ValueError"
        assert event.crash_file == ""
        assert event.stack_frames == []

    def test_critical_takes_priority_over_error(self):
        entries = [
            _make_log_entry("error", "ValueError: first"),
            _make_log_entry("critical", "OutOfMemoryError: heap exhausted"),
        ]
        event = _build_parsed_event(**self._base_kwargs(), entries=entries)
        assert event.severity == "critical"
        assert event.error_type == "OutOfMemoryError"


# ---------------------------------------------------------------------------
# ParsedLogEvent serialisation tests
# ---------------------------------------------------------------------------


class TestParsedLogEventSerialization:

    def _sample_event(self) -> ParsedLogEvent:
        return ParsedLogEvent(
            tenant_id="11111111-2222-3333-4444-555555555555",
            incident_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            s3_path="logs/tenant/context/incident.json.gz",
            service_name="payment-service",
            environment="production",
            error_type="NullPointerException",
            error_message="cannot invoke charge()",
            severity="error",
            crash_file="ChargeService.java",
            crash_line=142,
            crash_method="ChargeService.charge",
            stack_frames=[
                StackFrame(
                    file="ChargeService.java",
                    line=142,
                    method="ChargeService.charge",
                    module="com.neuralops",
                )
            ],
            context_log_count=50,
        )

    def test_to_dict_produces_json_serialisable_output(self):
        event = self._sample_event()
        result = event.to_dict()

        # Must be JSON-serialisable (no UUID objects, no datetime objects)
        json_str = json.dumps(result)
        assert json_str  # no exception raised

    def test_from_dict_round_trip(self):
        original = self._sample_event()
        as_dict = original.to_dict()
        restored = ParsedLogEvent.from_dict(as_dict)

        assert restored.tenant_id == original.tenant_id
        assert restored.incident_id == original.incident_id
        assert restored.error_type == original.error_type
        assert restored.severity == original.severity
        assert restored.crash_line == original.crash_line
        assert len(restored.stack_frames) == len(original.stack_frames)
        assert restored.stack_frames[0].file == original.stack_frames[0].file
        assert restored.stack_frames[0].line == original.stack_frames[0].line

    def test_severity_normalisation_on_construction(self):
        event = ParsedLogEvent(
            tenant_id="t",
            incident_id="i",
            s3_path="p",
            service_name="s",
            environment="e",
            severity="WARN",  # non-canonical → normalised
        )
        assert event.severity == "warning"

    def test_severity_unknown_for_unmapped_value(self):
        event = ParsedLogEvent(
            tenant_id="t",
            incident_id="i",
            s3_path="p",
            service_name="s",
            environment="e",
            severity="verbose",
        )
        assert event.severity == "unknown"

    def test_stack_frames_capped_at_20_on_construction(self):
        frames = [
            StackFrame(file=f"File{i}.java", line=i, method=f"method{i}")
            for i in range(30)
        ]
        event = ParsedLogEvent(
            tenant_id="t",
            incident_id="i",
            s3_path="p",
            service_name="s",
            environment="e",
            stack_frames=frames,
        )
        assert len(event.stack_frames) == 20

    def test_stack_frame_line_coercion(self):
        frame = StackFrame(file="Test.java", line="99", method="test")
        assert frame.line == 99

    def test_stack_frame_invalid_line_defaults_to_zero(self):
        frame = StackFrame(file="Test.java", line="not_a_number", method="test")
        assert frame.line == 0
