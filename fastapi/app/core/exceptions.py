"""
app/core/exceptions.py

Domain exception hierarchy for the FastAPI service.

All exceptions raised inside business logic (services, repositories,
middleware, dependencies) should be one of these typed classes.
The global error handler in app/middleware/error_handler.py converts
them into the structured JSON response format required by the API contract.

Error response shape (JSend-inspired):
{
    "status": "error",
    "message": "<human-readable summary>",
    "code": "<SCREAMING_SNAKE_CASE identifier>",
    "details": [...]          # optional extra context
}
"""

from __future__ import annotations

from typing import Any, List, Optional


# ── Base ─────────────────────────────────────────────────────────────────────

class NeuralOpsError(Exception):
    """
    Base class for all domain exceptions in this service.

    Attributes:
        message  — human-readable description surfaced to API consumers.
        code     — machine-readable SCREAMING_SNAKE_CASE identifier.
        status_code — HTTP status code the error handler should use.
        details  — optional list of supplementary context dicts.
    """

    message: str = "An unexpected error occurred."
    code: str = "INTERNAL_ERROR"
    status_code: int = 500
    details: List[Any] = []

    def __init__(
        self,
        message: Optional[str] = None,
        details: Optional[List[Any]] = None,
    ) -> None:
        self.message = message or self.__class__.message
        self.details = details or []
        super().__init__(self.message)


# ── Authentication & Authorisation ───────────────────────────────────────────

class TokenMissingError(NeuralOpsError):
    message = "Authentication token is missing."
    code = "TOKEN_MISSING"
    status_code = 401


class TokenExpiredError(NeuralOpsError):
    message = "Authentication token has expired."
    code = "TOKEN_EXPIRED"
    status_code = 401


class TokenInvalidError(NeuralOpsError):
    message = "Authentication token is invalid."
    code = "TOKEN_INVALID"
    status_code = 401


class PermissionDeniedError(NeuralOpsError):
    message = "You do not have permission to perform this action."
    code = "PERMISSION_DENIED"
    status_code = 403


# ── Tenant ───────────────────────────────────────────────────────────────────

class TenantNotFoundError(NeuralOpsError):
    message = "Tenant not found."
    code = "TENANT_NOT_FOUND"
    status_code = 404


class TenantSuspendedError(NeuralOpsError):
    message = "This tenant account has been suspended."
    code = "TENANT_SUSPENDED"
    status_code = 403


class TenantConfigStaleError(NeuralOpsError):
    """
    Raised when the tenant snapshot is missing from DB-2.
    Ingest continues on stale / missing data per the architecture SLO,
    but callers may choose to surface this as a warning.
    """
    message = "Tenant configuration snapshot is unavailable or stale."
    code = "TENANT_CONFIG_STALE"
    status_code = 503


# ── Database ─────────────────────────────────────────────────────────────────

class DatabaseError(NeuralOpsError):
    message = "A database error occurred."
    code = "DATABASE_ERROR"
    status_code = 503


# ── Validation ───────────────────────────────────────────────────────────────

class ValidationError(NeuralOpsError):
    message = "Request validation failed."
    code = "VALIDATION_ERROR"
    status_code = 422


# ── Not Found ────────────────────────────────────────────────────────────────

class NotFoundError(NeuralOpsError):
    message = "The requested resource was not found."
    code = "NOT_FOUND"
    status_code = 404