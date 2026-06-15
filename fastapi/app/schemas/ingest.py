"""
app/schemas/ingest.py

Pydantic request / response schemas for the log ingestion API.

The main schema `LogIngestRequest` mirrors the payload sent by the
NeuralOps SDK when a circular context buffer is flushed after an
error event.  It is intentionally permissive in the `context_logs`
field so that arbitrary SDK metadata is preserved without schema
enforcement at ingestion time — validation of individual log entries
is deferred to the downstream Celery parsing task.

Architecture reference: NeuralOps Technical Documentation — Section 16
(Client SDK), Section 17 (AI Agent Pipeline — Stage 1).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from uuid import UUID

from pydantic import BaseModel, Field

# ── Request ────────────────────────────────────────────────────────────────────


class LogIngestRequest(BaseModel):
    """
    Payload sent to ``POST /api/v1/ingest/logs``.

    Fields
    ------
    incident_id:
        Client-generated UUID that uniquely identifies the crash event.
        Used as the S3 object key suffix and as the Kafka message key.
        The SDK generates this before flushing; callers may also supply
        a stable idempotency key here.

    context_logs:
        The full contents of the SDK's in-process circular buffer at
        the moment the error fired.  Each element is an arbitrary dict
        (structured log entry) tagged with at minimum:
          - ``seq``      — monotonic integer for ordering.
          - ``level``    — severity string (debug / info / warning / error).
          - ``message``  — human-readable log message.
          - ``timestamp`` — ISO-8601 UTC string.
        Additional fields (service_name, metadata, stack_trace, etc.)
        are passed through opaquely and stored compressed in S3.

    service_name:
        Name of the originating service (e.g. "auth-service").
        Stored in the DB-2 metadata row for fast filtering.

    environment:
        Deployment environment label (e.g. "production", "staging").
        Stored in the DB-2 metadata row.
    """

    incident_id: UUID = Field(
        ...,
        description=(
            "Client-generated UUID uniquely identifying this crash event. "
            "Used as the S3 key suffix and as the outbox message key."
        ),
        examples=["550e8400-e29b-41d4-a716-446655440000"],
    )

    context_logs: List[Dict[str, Any]] = Field(
        ...,
        description=(
            "Ordered array of structured log entries captured by the SDK "
            "circular buffer (last N log lines before the error). "
            "Each element is a free-form dict; minimum fields are "
            "``seq``, ``level``, ``message``, and ``timestamp``."
        ),
        min_length=1,
    )

    service_name: str = Field(
        ...,
        max_length=255,
        description="Name of the originating service.",
        examples=["payment-service"],
    )

    environment: str = Field(
        ...,
        max_length=64,
        description="Deployment environment label.",
        examples=["production"],
    )

    severity: str = Field(
        default="error",
        max_length=64,
        description="Severity level of the error.",
    )
    error_type: str = Field(
        default="UnknownError",
        max_length=255,
        description="Type or class of the exception.",
    )
    file_path: Optional[str] = Field(
        default=None,
        max_length=1024,
        description="File where the error occurred.",
    )
    line_number: Optional[int] = Field(
        default=None,
        description="Line number where the error occurred.",
    )

    class Config:
        json_schema_extra = {
            "example": {
                "incident_id": "550e8400-e29b-41d4-a716-446655440000",
                "service_name": "payment-service",
                "environment": "production",
                "context_logs": [
                    {
                        "seq": 1,
                        "level": "info",
                        "message": "Processing payment for order #12345",
                        "timestamp": "2026-05-26T03:00:00Z",
                    },
                    {
                        "seq": 2,
                        "level": "error",
                        "message": "NullPointerException in ChargeService.charge()",
                        "timestamp": "2026-05-26T03:00:01Z",
                        "stack_trace": "...",
                    },
                ],
            }
        }


# ── Response ───────────────────────────────────────────────────────────────────


class LogIngestResponse(BaseModel):
    """
    Successful response body returned by ``POST /api/v1/ingest/logs``.

    Fields
    ------
    incident_id:
        Echoes the client-supplied UUID for idempotency confirmation.

    s3_path:
        Full S3 object key where the compressed context buffer was stored.
        Format: ``logs/{tenant_id}/context/{incident_id}.json.gz``

    message:
        Human-readable confirmation string.
    """

    incident_id: UUID = Field(
        ...,
        description="Echoes the client-supplied incident UUID.",
    )
    s3_path: str = Field(
        ...,
        description="S3 object key of the stored compressed context buffer.",
    )
    message: str = Field(
        default="Log context ingested successfully.",
        description="Human-readable confirmation.",
    )
