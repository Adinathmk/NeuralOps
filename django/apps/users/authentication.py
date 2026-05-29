import os
import uuid as uuid_lib
from datetime import timedelta

import jwt
from django.conf import settings
from django.utils import timezone
from rest_framework.authentication import TokenAuthentication
from rest_framework.exceptions import AuthenticationFailed

from .cache import cache_manager
from .models import User, UserSession


class JWTAuthentication(TokenAuthentication):
    """
    JWT authentication with Redis-based revocation checks.

    CRITICAL SECURITY MODEL:

    JWT signature verification ALWAYS happens on every request.
    Redis blocklist is checked AFTER signature verification succeeds.
    Redis does NOT bypass cryptographic verification.

    REQUEST FLOW:
    1. Extract Bearer token
    2. Decode + verify JWT signature (HS256)
    3. Verify expiry claim
    4. Check Redis blocklist (revocation status)
    5. If not revoked, attach claims to request
    6. View processes

    This preserves stateless JWT architecture while adding
    logout capability via Redis blocklist.
    """

    @staticmethod
    def generate_tokens(user, request=None):
        """
        Generate access and refresh tokens.
        Optionally creates session record if request is provided.

        Args:
            user: User instance
            request: Django request object (for IP, user agent)
        """
        secret = settings.JWT_PRIVATE_KEY

        algorithm = settings.JWT_ALGORITHM

        access_expire = timezone.now() + timedelta(
            minutes=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES
        )
        refresh_expire = timezone.now() + timedelta(
            days=settings.JWT_REFRESH_TOKEN_EXPIRE_DAYS
        )

        # Generate unique session ID
        jti = str(uuid_lib.uuid4())

        access_payload = {
            "jti": jti,
            "iss": "neuralops-jwt-key",
            "user_id": str(user.id),
            "email": user.email,
            "tenant_id": str(user.tenant.id) if user.tenant else None,
            "tenant_name": user.tenant.name if user.tenant else "Platform Admin",
            "role": user.role,
            "is_superadmin": user.is_superadmin,
            "exp": access_expire,
            "iat": timezone.now(),
            "type": "access",
        }

        refresh_payload = {
            "jti": jti,
            "iss": "neuralops-jwt-key",
            "user_id": str(user.id),
            "tenant_id": str(user.tenant.id) if user.tenant else None,
            "exp": refresh_expire,
            "iat": timezone.now(),
            "type": "refresh",
        }

        access_token = jwt.encode(access_payload, secret, algorithm=algorithm)
        refresh_token = jwt.encode(refresh_payload, secret, algorithm=algorithm)

        # Create session record if request provided
        if request:
            ip_address = JWTAuthentication._get_client_ip(request)
            user_agent = request.META.get("HTTP_USER_AGENT", "")
            device_name = JWTAuthentication._parse_device_name(user_agent)

            UserSession.objects.create(
                session_id=jti,
                user=user,
                tenant=user.tenant,
                ip_address=ip_address,
                user_agent=user_agent,
                device_name=device_name,
                expires_at=access_expire,
            )

        return access_token, refresh_token

    @staticmethod
    def verify_token(token):
        """
        Verify JWT token signature and expiry.

        ALWAYS called for every request. Never bypassed.
        This is the cryptographic guarantee of JWT authenticity.

        Args:
            token: JWT token string

        Returns:
            dict: Decoded JWT payload (claims)

        Raises:
            AuthenticationFailed: If signature invalid or token expired
        """
        secret = settings.JWT_PUBLIC_KEY
        algorithm = settings.JWT_ALGORITHM

        try:
            # Decode AND verify signature (cryptographic verification)
            # Also validates expiry claim automatically
            payload = jwt.decode(token, secret, algorithms=[algorithm])
            return payload
        except jwt.ExpiredSignatureError:
            raise AuthenticationFailed("Token has expired")
        except jwt.InvalidTokenError as e:
            raise AuthenticationFailed(f"Invalid token: {str(e)}")

    def authenticate(self, request):
        """
        Authenticate request using Bearer token.

        CORRECT REQUEST FLOW:

        1. Extract Bearer token from Authorization header
        2. CALL verify_token() → Decode + verify signature + check expiry
           (This ALWAYS happens - never skipped)
        3. THEN check Redis blocklist for token revocation
           (This is checked AFTER cryptographic verification succeeds)
        4. If not revoked, attach claims to request
        5. View processes with authenticated context

        Args:
            request: Django request object

        Returns:
            tuple: (None, payload) for stateless JWT auth

        Raises:
            AuthenticationFailed: If token invalid, expired, or revoked
        """
        auth_header = request.META.get("HTTP_AUTHORIZATION", "")

        if not auth_header.startswith("Bearer "):
            return None  # No token provided - public endpoint

        try:
            token = auth_header.split(" ")[1]

            # STEP 1: Verify JWT signature AND expiry (ALWAYS happens)
            payload = self.verify_token(token)

            # STEP 2: Check Redis blocklist (revocation)
            # This is checked AFTER signature verification succeeds
            jti = payload.get("jti")
            if jti and cache_manager.is_token_revoked(jti):
                raise AuthenticationFailed("Token has been revoked")

            # STEP 3: Attach claims to request for view access
            request.user_id = payload.get("user_id")
            request.tenant_id = payload.get("tenant_id")
            request.user_email = payload.get("email")
            request.user_role = payload.get("role")
            request.is_superadmin = payload.get("is_superadmin", False)

            user = User.objects.get(id=payload["user_id"])
            # Return stateless auth tuple
            return (user, payload)

        except AuthenticationFailed:
            raise
        except Exception as e:
            raise AuthenticationFailed(f"Invalid token: {str(e)}")

    @staticmethod
    def _get_client_ip(request):
        """Extract client IP from request (handles proxies)."""
        x_forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
        if x_forwarded_for:
            return x_forwarded_for.split(",")[0].strip()
        return request.META.get("REMOTE_ADDR", "0.0.0.0")

    @staticmethod
    def _parse_device_name(user_agent):
        """Parse device name from user agent string."""
        try:
            # Simple parsing without external dependency
            if "Windows" in user_agent:
                os_name = "Windows"
            elif "Mac" in user_agent:
                os_name = "Mac"
            elif "Linux" in user_agent:
                os_name = "Linux"
            elif "Android" in user_agent:
                os_name = "Android"
            elif "iPhone" in user_agent or "iPad" in user_agent:
                os_name = "iOS"
            else:
                os_name = "Unknown"

            # Parse browser
            if "Chrome" in user_agent:
                browser = "Chrome"
            elif "Firefox" in user_agent:
                browser = "Firefox"
            elif "Safari" in user_agent:
                browser = "Safari"
            elif "Edge" in user_agent:
                browser = "Edge"
            else:
                browser = "Unknown"

            return f"{browser} on {os_name}"
        except:
            return "Unknown Device"
