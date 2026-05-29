import json

import redis
from django.conf import settings


class CacheManager:
    """
    Redis cache for token revocation and tenant config ONLY.

    SECURITY MODEL:
    - JWT signature verification ALWAYS happens on every request
    - Redis does NOT bypass cryptographic verification
    - Redis is used for:
      1. Token revocation blocklist (logout)
      2. Rate limiting (brute force protection)
      3. Tenant config caching (performance optimization, not security-critical)

    JWT verification always happens BEFORE blocklist check.
    Cached config is used for performance, never affects authentication.
    """

    def __init__(self):
        """Initialize Redis connection."""
        self.redis_client = redis.from_url(settings.REDIS_URL, decode_responses=True)
        self.config_ttl = 3600  # 1 hour

    # ====================================================================
    # TOKEN REVOCATION BLOCKLIST (for logout)
    # ====================================================================

    def blocklist_token(self, token_jti, expiry_seconds):
        """
        Add token to revocation blocklist (logout).

        When user logs out, their token JTI is added here.
        Even though JWT signature is still valid, blocklisted tokens are rejected.

        Args:
            token_jti: JWT jti (unique ID) claim
            expiry_seconds: Seconds until token would naturally expire
        """
        key = f"token:blocklist:{token_jti}"
        self.redis_client.setex(key, expiry_seconds, "1")

    def is_token_revoked(self, token_jti):
        """
        Check if token is in revocation blocklist.

        Called AFTER JWT signature verification succeeds.

        Args:
            token_jti: JWT jti claim

        Returns:
            bool: True if revoked, False otherwise
        """
        key = f"token:blocklist:{token_jti}"
        return self.redis_client.exists(key) > 0

    # ====================================================================
    # TENANT CONFIG CACHING (performance optimization)
    # ====================================================================

    def cache_tenant_config(self, tenant_id, config_data):
        """
        Cache tenant configuration for performance.

        Reduces database queries for frequently-accessed tenant settings.
        Expires after 1 hour or on manual invalidation.

        This is NOT security-critical — just performance optimization.
        Stale config doesn't affect authentication.

        Args:
            tenant_id: Tenant UUID
            config_data: Dict with alert_confidence_threshold, log_retention_days, etc
        """
        key = f"tenant:config:{tenant_id}"
        value = json.dumps(config_data, default=str)
        self.redis_client.setex(key, self.config_ttl, value)

    def get_tenant_config(self, tenant_id):
        """
        Get cached tenant config.

        Returns cached config if available, None otherwise.
        Caller should check database if cache miss.

        Args:
            tenant_id: Tenant UUID

        Returns:
            dict: Tenant config if found in cache, None otherwise
        """
        key = f"tenant:config:{tenant_id}"
        cached = self.redis_client.get(key)
        if cached:
            return json.loads(cached)
        return None

    def invalidate_tenant_config(self, tenant_id):
        """
        Invalidate cached tenant config.

        Called when tenant settings change to ensure fresh data.

        Args:
            tenant_id: Tenant UUID
        """
        key = f"tenant:config:{tenant_id}"
        self.redis_client.delete(key)

    # ====================================================================
    # RATE LIMITING (for brute force protection)
    # ====================================================================

    def increment_failed_login(self, email):
        """
        Increment failed login counter for an email.

        Protects against brute force attacks by limiting login attempts.

        Args:
            email: User email

        Returns:
            int: Current failed attempt count
        """
        key = f"failed_login:{email}"
        count = self.redis_client.incr(key)
        if count == 1:
            self.redis_client.expire(key, 900)  # 15 minutes
        return count

    def get_failed_login_count(self, email):
        """
        Get failed login count for an email.

        Args:
            email: User email

        Returns:
            int: Number of failed attempts (0 if none)
        """
        key = f"failed_login:{email}"
        count = self.redis_client.get(key)
        return int(count) if count else 0

    def reset_failed_login(self, email):
        """
        Reset failed login counter on successful login.

        Args:
            email: User email
        """
        key = f"failed_login:{email}"
        self.redis_client.delete(key)

    def is_login_rate_limited(self, email, max_attempts=5):
        """
        Check if email is rate limited (too many failed attempts).

        Args:
            email: User email
            max_attempts: Max failed attempts before rate limit

        Returns:
            bool: True if rate limited, False otherwise
        """
        return self.get_failed_login_count(email) >= max_attempts

    def get_mfa_attempts(self, email):
        """Get MFA verification attempts count."""
        key = f"mfa_attempts:{email}"
        count = self.redis_client.get(key)
        return int(count) if count else 0

    def increment_mfa_attempts(self, email):
        """Increment MFA verification attempts."""
        key = f"mfa_attempts:{email}"
        self.redis_client.incr(key)
        self.redis_client.expire(key, 900)  # 15 minutes

    def reset_mfa_attempts(self, email):
        """Reset MFA verification attempts after success."""
        key = f"mfa_attempts:{email}"
        self.redis_client.delete(key)

    def lock_mfa_verification(self, email, minutes):
        """Lock MFA verification for N minutes."""
        key = f"mfa_locked:{email}"
        self.redis_client.setex(key, minutes * 60, "1")


# Singleton instance
cache_manager = CacheManager()


def cache_tenant_config(tenant_id, **config_data):
    """Module-level wrapper for cache_manager.cache_tenant_config."""
    cache_manager.cache_tenant_config(tenant_id, config_data)


def invalidate_tenant_config(tenant_id):
    """Module-level wrapper for cache_manager.invalidate_tenant_config."""
    cache_manager.invalidate_tenant_config(tenant_id)
