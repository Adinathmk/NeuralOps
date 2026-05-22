"""
app/middleware/error_handler.py

Global exception handlers registered on the FastAPI application.

All API errors — whether raised by business logic, middleware, or
FastAPI's own request validation — are caught here and converted to a
consistent structured JSON format:

{
    "status": "error",
    "message": "<human-readable summary>",
    "code":    "<SCREAMING_SNAKE_CASE identifier>",
    "details": [...]          ← optional extra context (validation errors, etc.)
}

Registration:
    Call `register_exception_handlers(app)` once inside main.py after
    the FastAPI() instance is created.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.exceptions import NeuralOpsError
from app.core.logging import get_logger

logger = get_logger(__name__)


# ── Standard error response builder ──────────────────────────────────────────

def _error_response(
    status_code: int,
    message: str,
    code: str,
    details: Optional[List[Any]] = None,
    request_id: Optional[str] = None,
) -> JSONResponse:
    body: Dict[str, Any] = {
        "status": "error",
        "message": message,
        "code": code,
        "details": details or [],
    }
    if request_id:
        body["request_id"] = request_id

    return JSONResponse(status_code=status_code, content=body)


def _get_request_id(request: Request) -> Optional[str]:
    return request.headers.get("x-request-id") or request.state.__dict__.get(
        "request_id"
    )


# ── Handlers ──────────────────────────────────────────────────────────────────

async def neuralops_exception_handler(
    request: Request, exc: NeuralOpsError
) -> JSONResponse:
    """
    Handle all domain exceptions (subclasses of NeuralOpsError).
    These are intentional, well-typed errors raised by business logic.
    """
    logger.warning(
        "domain_error",
        code=exc.code,
        message=exc.message,
        status_code=exc.status_code,
        path=str(request.url),
    )
    return _error_response(
        status_code=exc.status_code,
        message=exc.message,
        code=exc.code,
        details=exc.details,
        request_id=_get_request_id(request),
    )


async def http_exception_handler(
    request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    """
    Handle FastAPI / Starlette HTTP exceptions (404, 405, etc.).
    """
    # Map common HTTP status codes to machine-readable error codes
    _code_map = {
        400: "BAD_REQUEST",
        401: "UNAUTHORIZED",
        403: "FORBIDDEN",
        404: "NOT_FOUND",
        405: "METHOD_NOT_ALLOWED",
        408: "REQUEST_TIMEOUT",
        409: "CONFLICT",
        413: "PAYLOAD_TOO_LARGE",
        422: "UNPROCESSABLE_ENTITY",
        429: "TOO_MANY_REQUESTS",
        500: "INTERNAL_ERROR",
        502: "BAD_GATEWAY",
        503: "SERVICE_UNAVAILABLE",
    }
    code = _code_map.get(exc.status_code, "HTTP_ERROR")

    logger.info(
        "http_exception",
        status_code=exc.status_code,
        code=code,
        detail=exc.detail,
        path=str(request.url),
    )
    return _error_response(
        status_code=exc.status_code,
        message=str(exc.detail),
        code=code,
        request_id=_get_request_id(request),
    )


async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """
    Handle Pydantic / FastAPI request validation errors (422).

    Flattens Pydantic's error list into the `details` array so clients
    receive structured field-level error information.
    """
    details = []
    for error in exc.errors():
        details.append(
            {
                "field": " → ".join(str(loc) for loc in error.get("loc", [])),
                "message": error.get("msg", ""),
                "type": error.get("type", ""),
            }
        )

    logger.info(
        "validation_error",
        path=str(request.url),
        error_count=len(details),
    )
    return _error_response(
        status_code=422,
        message="Request validation failed. Check the 'details' field for specifics.",
        code="VALIDATION_ERROR",
        details=details,
        request_id=_get_request_id(request),
    )


async def unhandled_exception_handler(
    request: Request, exc: Exception
) -> JSONResponse:
    """
    Catch-all for any exception not handled by the above handlers.
    Logs the full traceback and returns a generic 500 response.
    Never surfaces internal details to the client.
    """
    logger.exception(
        "unhandled_exception",
        path=str(request.url),
        exc_type=type(exc).__name__,
    )
    return _error_response(
        status_code=500,
        message="An unexpected internal error occurred.",
        code="INTERNAL_ERROR",
        request_id=_get_request_id(request),
    )


# ── Registration ──────────────────────────────────────────────────────────────

def register_exception_handlers(app: FastAPI) -> None:
    """
    Register all exception handlers on the FastAPI application.
    Call this once in main.py after creating the FastAPI() instance.

    Order matters: more specific handlers must be registered first
    because FastAPI uses the last matching handler.
    """
    app.add_exception_handler(NeuralOpsError, neuralops_exception_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    # Catch-all — must be last
    app.add_exception_handler(Exception, unhandled_exception_handler)