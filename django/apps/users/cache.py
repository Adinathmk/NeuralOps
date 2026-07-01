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

    def blocklist_session(self, session_id, expiry_seconds):
        """
        Add session to revocation blocklist (logout).

        When user logs out or session is revoked, their session ID (sid) is added here.
        This invalidates ALL tokens (access & refresh) tied to this session instantly.

        Args:
            session_id: Session ID (sid claim / UserSession.id)
            expiry_seconds: Seconds until session would naturally expire
        """
        key = f"session:blocklist:{session_id}"
        self.redis_client.setex(key, expiry_seconds, "1")

    def is_session_revoked(self, session_id):
        """
        Check if session is in revocation blocklist.

        Called AFTER JWT signature verification succeeds.

        Args:
            session_id: Session ID (sid claim)

        Returns:
            bool: True if revoked, False otherwise
        """
        key = f"session:blocklist:{session_id}"
        return self.redis_client.exists(key) > 0

    def update_session_activity(self, session_id, timestamp_iso):
        """Update real-time session activity in Redis."""
        key = f"session:activity:{session_id}"
        # Set expiry to 7 days
        self.redis_client.setex(key, 604800, timestamp_iso)
        
    def get_session_activity(self, session_id):
        """Get real-time session activity from Redis."""
        key = f"session:activity:{session_id}"
        return self.redis_client.get(key)

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

    def get_client_ip(self, request):
        """Extract real IP; strip 1 rightmost proxy hop (Kong) from X-Forwarded-For."""
        x_forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
        if x_forwarded_for:
            proxies = [ip.strip() for ip in x_forwarded_for.split(",")]
            if len(proxies) > 1:
                return proxies[-2]
            return proxies[0]
        return request.META.get("REMOTE_ADDR")

    def _get_backoff_ttl(self, failure_count):
        """Calculate exponential backoff TTL."""
        return min(60 * (2 ** (max(1, failure_count) - 1)), 3600)

    def is_login_blocked(self, email, ip):
        """Check 3 keys in order: IP → combined → email. Returns (bool, reason)."""
        ip_count = int(self.redis_client.get(f"rl:login:ip:{ip}") or 0)
        if ip_count >= 20:
            return True, "Too many attempts from this IP. Please try again later."

        combined_count = int(
            self.redis_client.get(f"rl:login:combined:{email}:{ip}") or 0
        )
        if combined_count >= 5:
            return True, "Too many failed attempts. Please try again later."

        email_count = int(self.redis_client.get(f"rl:login:email:{email}") or 0)
        if email_count >= 50:
            return True, "Too many attempts for this account. Please try again later."

        return False, ""

    def increment_login_failure(self, email, ip):
        """INCR all 3 keys with correct TTLs."""
        key_ip = f"rl:login:ip:{ip}"
        key_combined = f"rl:login:combined:{email}:{ip}"
        key_email = f"rl:login:email:{email}"

        ip_count = self.redis_client.incr(key_ip)
        if ip_count == 1:
            self.redis_client.expire(key_ip, 60)

        combined_count = self.redis_client.incr(key_combined)
        self.redis_client.expire(key_combined, self._get_backoff_ttl(combined_count))

        email_count = self.redis_client.incr(key_email)
        if email_count == 1:
            self.redis_client.expire(key_email, 900)

        return combined_count

    def clear_login_failures(self, email, ip):
        """DELETE all 3 keys on successful login."""
        self.redis_client.delete(
            f"rl:login:ip:{ip}",
            f"rl:login:combined:{email}:{ip}",
            f"rl:login:email:{email}",
        )

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
