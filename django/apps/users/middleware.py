import logging

import jwt
from django.conf import settings
from django.db import connection
from django.http import JsonResponse

logger = logging.getLogger(__name__)


class TenantMiddleware:
    """
    Extract tenant context from trusted gateway headers or JWT token
    and attach to request.

    Manages PostgreSQL Row-Level Security (RLS) context via set_config
    inside a connection-scoped block for strict tenant isolation.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Initialize context
        request.tenant_id = None
        request.user_id = None
        request.user_email = None
        request.user_role = None
        request.is_superadmin = False

        # ─────────────────────────────────────────────
        # Trust pre-validated gateway headers if present
        # ─────────────────────────────────────────────
        # The API gateway validates JWT signature and injects:
        # X-Tenant-ID
        # X-User-ID
        # X-User-Role
        #
        # In local development or direct service access,
        # we fall back to JWT decoding below.
        gateway_tenant_id = request.META.get("HTTP_X_TENANT_ID")
        gateway_user_id = request.META.get("HTTP_X_USER_ID")
        gateway_user_role = request.META.get("HTTP_X_USER_ROLE")

        if gateway_user_id and gateway_tenant_id:
            # Gateway already validated the token
            request.tenant_id = gateway_tenant_id
            request.user_id = gateway_user_id
            request.user_role = gateway_user_role
            request.user_email = request.META.get("HTTP_X_USER_EMAIL", "")

            request.is_superadmin = (
                request.META.get("HTTP_X_IS_SUPERADMIN", "false").lower() == "true"
            )

            logger.debug(
                f"Gateway tenant context: "
                f"user={request.user_email}, "
                f"tenant={request.tenant_id}, "
                f"superadmin={request.is_superadmin}"
            )

        else:
            # ─────────────────────────────────────────
            # No gateway headers → decode JWT directly
            # ─────────────────────────────────────────
            auth_header = request.META.get("HTTP_AUTHORIZATION", "")

            if auth_header.startswith("Bearer "):
                try:
                    token = auth_header.split(" ")[1]
                    payload = self._verify_jwt_token(token)

                    request.tenant_id = payload.get("tenant_id")
                    request.user_id = payload.get("user_id")
                    request.user_email = payload.get("email")
                    request.user_role = payload.get("role")
                    request.is_superadmin = payload.get("is_superadmin", False)

                    logger.debug(
                        f"Tenant context: "
                        f"user={request.user_email}, "
                        f"tenant={request.tenant_id}, "
                        f"superadmin={request.is_superadmin}"
                    )

                except Exception as e:
                    logger.debug(f"JWT verification failed in middleware: {str(e)}")

        # ─────────────────────────────────────────────
        # Connection-Level RLS Context
        # ─────────────────────────────────────────────
        # We avoid transaction.atomic() to prevent long-running
        # transactions from locking Postgres.
        #
        # Instead, we set variables at the connection level
        # (is_local=false) and MUST clear them in finally.

        # RLS Chicken-and-Egg Fix:
        # Auth endpoints need to look up users across all tenants
        # to verify passwords.
        #
        # Since these views are strictly controlled and don't expose
        # tenant data, we bypass RLS for them.
        UNRESTRICTED_PATHS = [
            "/api/auth/login",
            "/api/auth/register",
            "/api/auth/forgot-password",
            "/api/auth/reset-password",
            "/api/auth/verify-email",
            "/api/auth/resend-verification",
            "/api/auth/refresh-token",
            "/api/auth/google/callback",
            "/api/auth/github/callback",
            "/api/auth/mfa/",
            "/api/invitations/",
        ]

        is_unrestricted = any(request.path.startswith(p) for p in UNRESTRICTED_PATHS)

        # ─────────────────────────────────────────────
        # Tenant Suspension Check
        # ─────────────────────────────────────────────
        # Fast O(1) Redis EXISTS — runs before any view or DB query.
        # Skipped for auth/unrestricted paths and superadmins.

        if request.tenant_id and not is_unrestricted and not request.is_superadmin:
            from users.cache import cache_manager

            suspended_key = f"tenant:{request.tenant_id}:suspended"

            try:
                if cache_manager.redis_client.exists(suspended_key):
                    return JsonResponse(
                        {
                            "success": False,
                            "message": "Your organization account is suspended.",
                            "code": "tenant_suspended",
                        },
                        status=403,
                    )

            except Exception as e:
                # Redis unavailable — fail open
                logger.warning(f"Redis suspension check failed: {e}")

        try:
            with connection.cursor() as cursor:

                if is_unrestricted or request.is_superadmin:
                    # Auth endpoint or platform admin:
                    # explicit RLS bypass
                    cursor.execute("SELECT set_config('app.bypass_rls', 'on', false)")

                    cursor.execute("SELECT set_config('app.current_tenant', '', false)")

                elif request.tenant_id:
                    # Normal tenant: strict isolation
                    cursor.execute("SELECT set_config('app.bypass_rls', 'off', false)")

                    cursor.execute(
                        "SELECT set_config('app.current_tenant', %s, false)",
                        [str(request.tenant_id)],
                    )

                else:
                    # Unauthenticated / invalid:
                    # fail closed
                    cursor.execute("SELECT set_config('app.bypass_rls', 'off', false)")

                    cursor.execute("SELECT set_config('app.current_tenant', '', false)")

            # Process request
            response = self.get_response(request)

        finally:
            # CRITICAL:
            # Clean up connection variables before the
            # connection returns to Django's pool.

            if connection.connection is not None:
                try:
                    with connection.cursor() as cursor:
                        cursor.execute(
                            "SELECT set_config('app.bypass_rls', 'off', false)"
                        )

                        cursor.execute(
                            "SELECT set_config('app.current_tenant', '', false)"
                        )

                except Exception:
                    # If connection already died,
                    # Postgres wipes session state automatically.
                    pass

        return response

    @staticmethod
    def _verify_jwt_token(token):
        """
        RS256 verification using public key.
        """

        public_key = settings.JWT_PUBLIC_KEY
        algorithm = settings.JWT_ALGORITHM

        try:
            return jwt.decode(token, public_key, algorithms=[algorithm])

        except jwt.ExpiredSignatureError:
            raise jwt.ExpiredSignatureError("Token has expired")

        except jwt.InvalidTokenError as e:
            raise jwt.InvalidTokenError(f"Invalid token: {str(e)}")
