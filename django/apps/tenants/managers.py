"""
tenants/managers.py

TenantQuerySet and TenantManager provide automatic queryset-level tenant
isolation. Attach TenantManager as the default manager on any model that has
a `tenant` ForeignKey to prevent accidental cross-tenant data leaks.

Usage on a model:
    class MyModel(models.Model):
        tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, ...)
        objects = TenantManager()

Usage in a view (always use for_tenant — never call .all() or .filter()
without scoping first):
    records = MyModel.objects.for_tenant(request.tenant_id).filter(...)
"""

import threading

from django.contrib.auth.models import BaseUserManager
from django.db import models

# ---------------------------------------------------------------------------
# Thread-local storage for the current tenant context.
# This lets TenantManager.get_queryset() auto-scope when called without an
# explicit tenant argument — useful for admin / shell / signal contexts.
# Views and serializers should always prefer the explicit .for_tenant() form.
# ---------------------------------------------------------------------------
_thread_local = threading.local()


def set_current_tenant(tenant_id):
    """Call this at the start of a request to set the ambient tenant context."""
    _thread_local.tenant_id = tenant_id


def get_current_tenant():
    """Return the ambient tenant_id, or None if not set."""
    return getattr(_thread_local, "tenant_id", None)


def clear_current_tenant():
    """Call this at the end of a request (e.g. in middleware) to clean up."""
    _thread_local.tenant_id = None


# ---------------------------------------------------------------------------
# Queryset
# ---------------------------------------------------------------------------


class TenantQuerySet(models.QuerySet):
    """
    QuerySet that can scope itself to a specific tenant.

    The .for_tenant() method is explicit and preferred.
    The .unscoped() escape hatch is available for platform-admin code that
    legitimately needs cross-tenant access — use it sparingly and always with
    a comment explaining why.
    """

    def for_tenant(self, tenant_id):
        """Return only rows belonging to the given tenant."""
        if tenant_id is None:
            # Fail loudly rather than silently returning everything.
            raise ValueError(
                "for_tenant() called with tenant_id=None. "
                "Ensure the request is authenticated and carries a valid tenant context."
            )
        return self.filter(tenant_id=tenant_id)

    def unscoped(self):
        """
        Return an unscoped queryset — ALL tenants visible.
        Only use in platform-admin code, management commands, or migrations.
        Always document why cross-tenant access is needed at the call site.
        """
        return self.all()


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


class TenantManager(models.Manager):
    """
    Default manager that uses TenantQuerySet.

    When set as `objects` on a model, it does NOT auto-scope implicitly on
    every `.all()` / `.filter()` call — that would be too magical and could
    break admin, migrations, and management commands. Instead it surfaces the
    explicit `.for_tenant()` API.

    Auto-scoping via thread-local IS applied in get_queryset() so that the
    Django admin and shell automatically filter to the ambient tenant when one
    is set — helpful for per-tenant admin sites.
    """

    def get_queryset(self):
        qs = TenantQuerySet(self.model, using=self._db)
        tenant_id = get_current_tenant()
        if tenant_id is not None:
            return qs.for_tenant(tenant_id)
        return qs

    def for_tenant(self, tenant_id):
        """Shortcut: MyModel.objects.for_tenant(tid).filter(...)"""
        return self.get_queryset().filter(tenant_id=tenant_id)

    def unscoped(self):
        """Bypass tenant scoping entirely. Use with care."""
        return TenantQuerySet(self.model, using=self._db)


class UserManager(BaseUserManager):
    """Custom user manager for multi-tenant User model."""

    def create_user(self, email, password, tenant=None, **extra_fields):
        """Create a regular user (requires tenant)."""
        if not email:
            raise ValueError("Email is required")

        if not tenant and not extra_fields.get("is_superuser"):
            raise ValueError("Tenant is required for regular users")

        email = self.normalize_email(email)

        # Check email uniqueness per tenant (or globally if no tenant)
        if tenant and self.filter(email=email, tenant=tenant).exists():
            raise ValueError(f"User with email {email} already exists in this tenant")

        user = self.model(email=email, tenant=tenant, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)

        return user

    def create_superuser(self, email, password, **extra_fields):
        """Create a platform superuser (no tenant required)."""
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        extra_fields.setdefault("is_superadmin", True)
        extra_fields.setdefault("role", "owner")

        if extra_fields.get("is_staff") is not True:
            raise ValueError("Superuser must have is_staff=True")

        if extra_fields.get("is_superuser") is not True:
            raise ValueError("Superuser must have is_superuser=True")

        # Platform superusers don't need a tenant
        return self.create_user(email, password, tenant=None, **extra_fields)
