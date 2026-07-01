"""
app/schemas/ingest.py — Updated for structured SDK payload (SDK v1.0.0)
"""
from __future__ import annotations
from typing import Any, Dict, List, Optional
from uuid import UUID
from pydantic import BaseModel, Field, field_validator


class StackFrame(BaseModel):
    file: str
    line: int
    function: str
    code: Optional[str] = Field(default=None, alias="code_context")


class StackTrace(BaseModel):
    exception_type: str
    exception_message: str
    frames: List[StackFrame]


class TriggerEvent(BaseModel):
    level: str
    message: str
    timestamp: str
    stack_trace: Optional[StackTrace] = None


class SDKMeta(BaseModel):
    sdk_version: str
    python_version: str
    hostname: Optional[str] = None
    framework: Optional[str] = None


class LogIngestRequest(BaseModel):
    incident_id: UUID = Field(..., description="Client-generated UUID")
    service_name: str = Field(..., max_length=255)
    environment: str = Field(..., max_length=64)
    severity: str = Field(default="error", max_length=64)
    error_type: str = Field(default="UnknownError", max_length=255)
    file_path: Optional[str] = Field(default=None, max_length=1024)
    line_number: Optional[int] = Field(default=None)

    @field_validator("severity")
    @classmethod
    def normalize_severity(cls, v: str) -> str:
        return v.lower() if v else "error"

    # NEW: trigger is now a first-class structured field
    trigger: Optional[TriggerEvent] = Field(
        default=None,
        description="The log record that triggered the flush. "
                    "Separate from context_logs so the AI agent finds "
                    "the crash immediately without scanning the array."
    )

    # context_logs now contains ONLY the pre-error context — NOT the trigger
    context_logs: List[Dict[str, Any]] = Field(default_factory=list)

    # NEW: SDK and runtime metadata
    sdk_meta: Optional[SDKMeta] = Field(default=None)

    class Config:
        json_schema_extra = {
            "example": {
                "incident_id": "550e8400-e29b-41d4-a716-446655440000",
                "service_name": "payment-service",
                "environment": "production",
                "severity": "error",
                "error_type": "NullPointerException",
                "file_path": "services/charge_service.py",
                "line_number": 47,
                "trigger": {
                    "level": "error",
                    "message": "NullPointerException in ChargeService.charge()",
                    "timestamp": "2026-06-25T10:00:00Z",
                    "stack_trace": {
                        "exception_type": "NullPointerException",
                        "exception_message": "charge() argument cannot be None",
                        "frames": [
                            {
                                "file": "services/charge_service.py",
                                "line": 47,
                                "function": "charge",
                                "code": "result = processor.run(amount)"
                            }
                        ]
                    }
                },
                "context_logs": [
                    {
                        "seq": 1,
                        "level": "info",
                        "message": "Processing payment for order #12345",
                        "timestamp": "2026-06-25T09:59:59Z"
                    }
                ],
                "sdk_meta": {
                    "sdk_version": "1.0.0",
                    "python_version": "3.12.1",
                    "hostname": "pod-payment-abc123",
                    "framework": "django"
                }
            }
        }


class LogIngestResponse(BaseModel):
    incident_id: UUID
    s3_path: str
    message: str = "Log context ingested successfully."
