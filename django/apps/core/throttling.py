from core.quotas import QuotaService
from rest_framework.throttling import SimpleRateThrottle
from tenants.models import Tenant


class TenantRateThrottle(SimpleRateThrottle):
    """
    Dynamic rate limit based on tenant's plan_tier.
    Falls back to IP-based rate limit for unauthenticated users.
    """

    scope = "tenant"

    def get_cache_key(self, request, view):
        # request.tenant_id is set by TenantMiddleware
        tenant_id = getattr(request, "tenant_id", None)
        if tenant_id:
            return f"throttle_tenant_{tenant_id}"

        # Fallback to IP address for unauthenticated requests
        return self.get_ident(request)

    def allow_request(self, request, view):
        tenant_id = getattr(request, "tenant_id", None)

        if tenant_id:
            # Check cache first to avoid DB lookup on every request
            cache_key = f"rate_limit_{tenant_id}"
            from django.core.cache import cache

            rate = cache.get(cache_key)

            if not rate:
                try:
                    tenant = Tenant.objects.get(id=tenant_id)
                    rate = QuotaService.get_rate_limit(tenant)
                    # Cache the rate limit for 10 minutes (600 seconds)
                    cache.set(cache_key, rate, timeout=600)
                except Tenant.DoesNotExist:
                    rate = None

            if rate:
                self.rate = rate
                self.num_requests, self.duration = self.parse_rate(self.rate)

        return super().allow_request(request, view)
