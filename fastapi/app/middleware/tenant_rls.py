"""
app/middleware/tenant_rls.py

Tenant Row-Level Security middleware.

Responsibility
--------------
For every authenticated request, this middleware sets the PostgreSQL
session-level variable `app.tenant_id` on each database connection
*before* any ORM query executes.

PostgreSQL RLS policies on all tenant-scoped tables in DB-2 are written
as:

    USING (tenant_id::text = current_setting('app.tenant_id', true))

So even if application-level query code forgets to filter by tenant_id,
the database engine will silently exclude rows belonging to other tenants.
This is the belt-and-suspenders layer described in the NeuralOps
architecture documentation (Section 11 — Authentication & Multi-Tenancy).

How it works
------------
1. JWTAuthMiddleware runs first and attaches `request.state.tenant_id`.
2. This middleware reads that value.
3. For every SQLAlchemy async session created within the request (via
   `get_db()`), the `tenant_rls` middleware patches the session's
   `begin` event to emit:

       SET LOCAL app.tenant_id = '<uuid>'

   `SET LOCAL` scopes the variable to the current transaction, which
   is correct because NullPool gives each request a fresh connection.

4. Routes that have no tenant context (health endpoint, unauthenticated
   paths) skip the RLS setup.

Architecture reference: NeuralOps Technical Documentation — Section 11
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core.logging import get_logger

logger = get_logger(__name__)

# Paths where tenant RLS is not required
_SKIP_PATHS = frozenset(
    {
        "/health",
        "/docs",
        "/redoc",
        "/openapi.json",
    }
)


class TenantRLSMiddleware(BaseHTTPMiddleware):
    """
    Middleware that stores the current request's tenant_id in request.state
    so that the `get_db()` dependency can set `app.tenant_id` on the
    PostgreSQL connection at session-open time.

    The actual SQL `SET LOCAL` statement is executed inside the async
    session event listener defined in `apply_tenant_rls_to_session()` below.
    This function is called by the `get_db()` dependency immediately
    after creating the session.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path

        if path in _SKIP_PATHS or path.startswith("/health"):
            return await call_next(request)

        tenant_id: str = getattr(request.state, "tenant_id", "")

        if tenant_id:
            # Store for use by the session dependency
            request.state.rls_tenant_id = tenant_id
            logger.debug("tenant_rls_set", tenant_id=tenant_id, path=path)
        else:
            # Authenticated routes should always have a tenant_id by this point
            # (JWTAuthMiddleware runs before this middleware).
            # Log a warning but do not block — the RLS policy will simply
            # return no rows if the session variable is not set.
            logger.warning(
                "tenant_rls_missing_tenant_id",
                path=path,
                detail=(
                    "Request reached TenantRLSMiddleware without a tenant_id "
                    "in request.state. RLS variable will NOT be set."
                ),
            )
            request.state.rls_tenant_id = ""

        return await call_next(request)


# ── Session-level RLS helper ──────────────────────────────────────────────────


async def apply_tenant_rls_to_session(session, tenant_id: str) -> None:
    """
    Execute `SET LOCAL app.tenant_id = '<uuid>'` on the given async session.

    Call this at the very start of the `get_db()` dependency, before
    yielding the session to the route handler:

        async def get_db(request: Request):
            async with AsyncSessionLocal() as session:
                tenant_id = getattr(request.state, "rls_tenant_id", "")
                await apply_tenant_rls_to_session(session, tenant_id)
                yield session

    `SET LOCAL` is transaction-scoped. Because NullPool is used, each
    request gets a fresh connection, so there is no risk of the variable
    leaking between requests.
    """
    from sqlalchemy import text

    if not tenant_id:
        return

    await session.execute(
        text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
        {"tenant_id": str(tenant_id)},
    )
    logger.debug("tenant_rls_applied_to_session", tenant_id=tenant_id)
