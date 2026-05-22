from django.contrib import admin
from .models import SuperAdminAuditLog


@admin.register(SuperAdminAuditLog)
class SuperAdminAuditLogAdmin(admin.ModelAdmin):
    list_display = ('id', 'actor_user_id', 'action', 'target_tenant_id', 'created_at')
    list_filter = ('action', 'created_at')
    search_fields = ('actor_user_id', 'target_tenant_id', 'notes')
    readonly_fields = ('id', 'actor_user_id', 'action', 'target_tenant_id', 'notes', 'created_at')
    ordering = ('-created_at',)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

