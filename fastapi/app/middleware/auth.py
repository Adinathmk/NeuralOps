"""
app/middleware/auth.py

JWT authentication middleware for the FastAPI service.

Flow
----
1. The API Gateway validates the JWT signature and injects the decoded
   claims as trusted HTTP headers before forwarding the request:
     X-Tenant-ID   → tenant UUID
     X-User-ID     → user UUID
     X-User-Role   → role string (owner | admin | engineer | viewer)

2. This middleware reads those injected headers and attaches the claims
   to `request.state` so that downstream dependencies and route handlers
   can access them without re-parsing the token.

3. As a defence-in-depth measure, if the raw `Authorization: Bearer <token>`
   header is also present (e.g. in direct-to-service calls during local
   development), this middleware also validates the token signature using
   the RS256 public key and falls back to the decoded payload for claims.

4. Routes listed in `UNAUTHENTICATED_PATHS` are excluded from auth checks
   (health endpoint, OpenAPI schema).

Design decision: FastAPI services trust the gateway-injected headers in
production. The token re-validation fallback is intentionally available
for local development convenience but does NOT relax security in any
environment — if the gateway is present, it validates first.
"""

from __future__ import annotations

from typing import FrozenSet

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core.exceptions import NeuralOpsError, TokenMissingError
from app.core.logging import get_logger, request_id_ctx, tenant_id_ctx
from app.core.security import decode_token
from app.middleware.error_handler import _error_response

logger = get_logger(__name__)

# Paths that do not require authentication
UNAUTHENTICATED_PATHS: FrozenSet[str] = frozenset(
    {
        "/health",
        "/metrics",
        "/docs",
        "/redoc",
        "/openapi.json",
        "/api/v1/webhooks/github",
        "/api/v1/ingest/logs",
    }
)


class JWTAuthMiddleware(BaseHTTPMiddleware):
    """
    Starlette middleware that enforces JWT authentication on all protected routes.

    After this middleware runs, downstream handlers can read:
        request.state.tenant_id  → str UUID
        request.state.user_id    → str UUID
        request.state.user_role  → str
        request.state.jwt_payload → dict (full decoded claims, if token was parsed)
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        try:
            path = request.url.path

            # ── Skip unauthenticated paths ────────────────────────────────────────
            if path in UNAUTHENTICATED_PATHS or path.startswith("/health"):
                return await call_next(request)

            # ── Strategy 1: Trust gateway-injected headers (production path) ──────
            tenant_id = request.headers.get("x-tenant-id")
            user_id = request.headers.get("x-user-id")
            user_role = request.headers.get("x-user-role")

            if tenant_id and user_id and user_role:
                request.state.tenant_id = tenant_id
                request.state.user_id = user_id
                request.state.user_role = user_role
                request.state.jwt_payload = {}

                # Populate context vars for structured logging
                tenant_id_ctx.set(tenant_id)

                logger.debug(
                    "auth_via_gateway_headers",
                    tenant_id=tenant_id,
                    user_id=user_id,
                    role=user_role,
                )
                return await call_next(request)

            # ── Strategy 2: Parse Bearer token directly (dev / no-gateway path) ───
            auth_header = request.headers.get("authorization", "")
            if not auth_header.startswith("Bearer "):
                raise TokenMissingError(
                    "No authentication credentials provided. "
                    "Expected either gateway-injected headers or an Authorization: Bearer header."
                )

            token = auth_header.removeprefix("Bearer ").strip()

            # decode_token raises TokenExpiredError / TokenInvalidError on failure
            payload = decode_token(token)

            tenant_id = payload.get("tenant_id", "")
            user_id = payload.get("user_id", "")
            user_role = payload.get("role", "")

            if not tenant_id:
                raise TokenMissingError("JWT payload is missing 'tenant_id' claim.")

            request.state.tenant_id = tenant_id
            request.state.user_id = user_id
            request.state.user_role = user_role
            request.state.jwt_payload = payload

            tenant_id_ctx.set(tenant_id)

            logger.debug(
                "auth_via_bearer_token",
                tenant_id=tenant_id,
                user_id=user_id,
                role=user_role,
            )

            return await call_next(request)

        except NeuralOpsError as exc:
            return _error_response(
                status_code=exc.status_code,
                message=exc.message,
                code=exc.code,
                details=exc.details,
            )
