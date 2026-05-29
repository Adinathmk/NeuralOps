from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import BasePermission


class IsTenantOwner(BasePermission):
    """
    Allow access only if user is tenant owner.
    Requires: request.tenant_id set by TenantMiddleware
    """

    message = "Only tenant owner can access this."

    def has_permission(self, request, view):
        return bool(request.user_id and request.user_role == "owner")


class IsTenantAdmin(BasePermission):
    """
    Allow access only if user is tenant admin or owner.
    """

    message = "Only tenant admin can access this."

    def has_permission(self, request, view):
        return bool(request.user_id and request.user_role in ["admin", "owner"])


class IsTenantUser(BasePermission):
    """
    Allow access only if user belongs to a tenant.
    Denies platform admins (tenant_id=None).
    """

    message = "Only tenant users can access this."

    def has_permission(self, request, view):
        return bool(request.tenant_id)


class IsPlatformAdmin(BasePermission):
    """
    Allow access only if user is platform admin.
    Platform admins have tenant_id=None and is_superadmin=True
    """

    message = "Only platform admin can access this."

    def has_permission(self, request, view):
        return bool(request.is_superadmin and not request.tenant_id)


class CanAccessTenant(BasePermission):
    """
    Check if user belongs to the requested tenant.
    Use when accessing tenant-specific resources.

    Requires: request.tenant_id set by TenantMiddleware
    """

    message = "You do not have access to this tenant."

    def has_permission(self, request, view):
        # User must have tenant_id and it must match
        if not request.tenant_id:
            raise PermissionDenied("No tenant context")
        return True
