"""
app/core/security.py

RS256 JWT verification logic.

Django (Service 1) holds the RSA PRIVATE key and is the sole token issuer.
FastAPI (Service 2) holds ONLY the RSA PUBLIC key and uses it exclusively
for token VERIFICATION — it never signs tokens.

A compromise of this service cannot expose the signing key.
"""

from __future__ import annotations

from typing import Any, Dict

from jose import JWTError, jwt
from jose.exceptions import ExpiredSignatureError

from app.core.config import get_settings
from app.core.exceptions import (
    TokenExpiredError,
    TokenInvalidError,
)

# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def decode_token(token: str) -> Dict[str, Any]:
    """
    Validate a JWT bearer token using the RS256 public key.

    Returns the decoded payload (claims dict) on success.
    Raises:
        TokenExpiredError  — token has passed its `exp` claim.
        TokenInvalidError  — signature invalid, wrong algorithm, malformed, etc.
    """
    settings = get_settings()

    try:
        payload = jwt.decode(
            token,
            settings.JWT_PUBLIC_KEY,
            algorithms=[settings.JWT_ALGORITHM],
            options={
                "require_exp": True,
                "require_sub": False,   # Django embeds user_id, not sub
                "verify_exp": True,
            },
        )
    except ExpiredSignatureError as exc:
        raise TokenExpiredError("Access token has expired.") from exc
    except JWTError as exc:
        raise TokenInvalidError(f"Token validation failed: {exc}") from exc

    return payload


def extract_tenant_id(payload: Dict[str, Any]) -> str:
    """
    Pull the tenant_id claim from a decoded JWT payload.

    Django embeds `tenant_id` as a top-level claim when issuing tokens.
    Raises TokenInvalidError if the claim is absent.
    """
    tenant_id: str | None = payload.get("tenant_id")
    if not tenant_id:
        raise TokenInvalidError("JWT payload is missing the 'tenant_id' claim.")
    return tenant_id


def extract_user_id(payload: Dict[str, Any]) -> str:
    """Pull the user_id claim from a decoded JWT payload."""
    user_id: str | None = payload.get("user_id")
    if not user_id:
        raise TokenInvalidError("JWT payload is missing the 'user_id' claim.")
    return user_id


def extract_user_role(payload: Dict[str, Any]) -> str:
    """Pull the role claim from a decoded JWT payload."""
    role: str | None = payload.get("role")
    if not role:
        raise TokenInvalidError("JWT payload is missing the 'role' claim.")
    return role