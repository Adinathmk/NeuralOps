"""
core/quotas.py  — Plan Tier Quota Enforcement

Add this new file to your project. Import QuotaService wherever you need to
enforce plan limits before allowing a resource-creating action.

Current quota dimensions:
  - Max active users per tenant
  - Max API keys per tenant
  - Log retention days (used by TenantConfiguration default logic)

Add more dimensions (e.g., max pipelines, max alert rules) as your product grows.
"""

from dataclasses import dataclass
from rest_framework.exceptions import PermissionDenied


# ---------------------------------------------------------------------------
# Plan definitions
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PlanQuota:
    max_users: int          # -1 = unlimited
    max_api_keys: int       # -1 = unlimited
    max_retention_days: int
    rate_limit: str         # DRF throttle rate format (e.g. '100/minute')


PLAN_QUOTAS: dict[str, PlanQuota] = {
    "free": PlanQuota(
        max_users=3,
        max_api_keys=2,
        max_retention_days=30,
        rate_limit='100/minute',
    ),
    "pro": PlanQuota(
        max_users=25,
        max_api_keys=10,
        max_retention_days=90,
        rate_limit='500/minute',
    ),
    "enterprise": PlanQuota(
        max_users=-1,       # unlimited
        max_api_keys=-1,    # unlimited
        max_retention_days=365,
        rate_limit='2000/minute',
    ),
}

_DEFAULT_QUOTA = PLAN_QUOTAS["free"]   # safe fallback for unknown plan tiers


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class QuotaService:
    """
    Static methods for enforcing plan-based resource limits.

    All methods raise rest_framework.exceptions.PermissionDenied if the limit
    is exceeded, which DRF translates to HTTP 403 with a clear message.
    Call these BEFORE creating the resource (invitation, API key, etc.).
    """

    @staticmethod
    def _get_quota(tenant) -> PlanQuota:
        return PLAN_QUOTAS.get(tenant.plan_tier, _DEFAULT_QUOTA)

    @staticmethod
    def get_rate_limit(tenant) -> str:
        """Return the rate limit string for the tenant's plan."""
        return QuotaService._get_quota(tenant).rate_limit

    # ------------------------------------------------------------------
    # User / seat limit
    # ------------------------------------------------------------------

    @staticmethod
    def check_user_limit(tenant) -> None:
        """
        Raise PermissionDenied if adding one more active user would exceed
        the tenant's plan user limit.

        Call in InviteEngineerView before creating the UserInvitation.
        """
        from users.models import User   # local import to avoid circular deps

        quota = QuotaService._get_quota(tenant)
        if quota.max_users == -1:
            return  # unlimited

        current_count = User.objects.filter(
            tenant=tenant, is_active=True
        ).count()

        if current_count >= quota.max_users:
            raise PermissionDenied(
                f"Your {tenant.plan_tier} plan allows a maximum of "
                f"{quota.max_users} active users. "
                "Please upgrade your plan to invite more team members."
            )

    # ------------------------------------------------------------------
    # API key limit
    # ------------------------------------------------------------------

    @staticmethod
    def check_api_key_limit(tenant) -> None:
        """
        Raise PermissionDenied if adding one more API key would exceed the
        tenant's plan limit.

        Call in APIKeyCreateView before creating the key.
        """
        from users.models import APIKey   # adjust import path if needed

        quota = QuotaService._get_quota(tenant)
        if quota.max_api_keys == -1:
            return  # unlimited

        current_count = APIKey.objects.filter(
            tenant=tenant, is_active=True
        ).count()

        if current_count >= quota.max_api_keys:
            raise PermissionDenied(
                f"Your {tenant.plan_tier} plan allows a maximum of "
                f"{quota.max_api_keys} active API keys. "
                "Please upgrade your plan or revoke unused keys."
            )

    # ------------------------------------------------------------------
    # Retention days (informational — use in TenantConfiguration validation)
    # ------------------------------------------------------------------

    @staticmethod
    def max_retention_days(tenant) -> int:
        """Return the maximum allowed log retention days for this tenant's plan."""
        return QuotaService._get_quota(tenant).max_retention_days

    @staticmethod
    def clamp_retention_days(tenant, requested_days: int) -> int:
        """
        Return the minimum of requested_days and the plan maximum.
        Use in TenantConfigView.patch() to silently clamp the value,
        or raise PermissionDenied if you prefer a strict approach.
        """
        plan_max = QuotaService.max_retention_days(tenant)
        return min(requested_days, plan_max)

    # ------------------------------------------------------------------
    # Summary (useful for /api/tenant/usage/ endpoint)
    # ------------------------------------------------------------------

    @staticmethod
    def usage_summary(tenant) -> dict:
        """
        Return current usage vs limits. Useful for a billing/usage endpoint.
        """
        from users.models import User, APIKey

        quota = QuotaService._get_quota(tenant)

        user_count = User.objects.filter(tenant=tenant, is_active=True).count()
        key_count = APIKey.objects.filter(tenant=tenant, is_active=True).count()

        def fmt(current, maximum):
            return {
                "current": current,
                "limit": maximum if maximum != -1 else None,   # None = unlimited
                "at_limit": maximum != -1 and current >= maximum,
            }

        return {
            "plan_tier": tenant.plan_tier,
            "users": fmt(user_count, quota.max_users),
            "api_keys": fmt(key_count, quota.max_api_keys),
            "log_retention_days": {
                "limit": quota.max_retention_days,
            },
        }