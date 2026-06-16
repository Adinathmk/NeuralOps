import jwt
from channels.db import database_sync_to_async
from channels.middleware import BaseMiddleware
from django.conf import settings
from django.contrib.auth.models import AnonymousUser
from users.models import User


@database_sync_to_async
def get_user_from_token(token_str):
    try:
        payload = jwt.decode(
            token_str, settings.JWT_PUBLIC_KEY, algorithms=[settings.JWT_ALGORITHM]
        )
        user = User.objects.get(id=payload["user_id"])
        user.tenant_id_from_token = payload.get("tenant_id")
        user.role_from_token = payload.get("role")
        return user
    except Exception:
        return AnonymousUser()


class JWTAuthMiddleware(BaseMiddleware):
    """
    Extracts JWT from query string (?token=...) or
    from gateway-injected headers (X-User-ID / X-Tenant-ID).
    """

    async def __call__(self, scope, receive, send):
        # Kong path: gateway already validated, trust headers
        headers = dict(scope.get("headers", []))

        gateway_user_id = headers.get(b"x-user-id", b"").decode()
        gateway_tenant_id = headers.get(b"x-tenant-id", b"").decode()

        if gateway_user_id and gateway_tenant_id:
            scope["user_id"] = gateway_user_id
            scope["tenant_id"] = gateway_tenant_id
            scope["user_role"] = headers.get(b"x-user-role", b"").decode()
        else:
            # Direct connection (dev): extract from query string ?token=...
            from urllib.parse import parse_qs

            query_string = scope.get("query_string", b"").decode()
            params = parse_qs(query_string)
            token_list = params.get("jwt", []) or params.get("token", [])

            if token_list:
                user = await get_user_from_token(token_list[0])
                scope["user"] = user
                scope["user_id"] = str(user.id) if not user.is_anonymous else None
                scope["tenant_id"] = getattr(user, "tenant_id_from_token", None)
                scope["user_role"] = getattr(user, "role_from_token", None)
            else:
                scope["user_id"] = None
                scope["tenant_id"] = None

        return await super().__call__(scope, receive, send)


def JWTAuthMiddlewareStack(inner):
    return JWTAuthMiddleware(inner)
