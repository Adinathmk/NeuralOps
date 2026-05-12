from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from .models import User, UserInvitation, AuditLog, APIKey
from tenants.models import TenantConfiguration
from .models import User, UserInvitation, AuditLog, APIKey, UserSession
from tenants.models import TenantConfiguration


from .models import OAuthAccount

@admin.register(OAuthAccount)
class OAuthAccountAdmin(admin.ModelAdmin):
    list_display = ('user', 'provider', 'provider_name', 'created_at', 'last_used_at')
    list_filter = ('provider', 'created_at', 'last_used_at')
    search_fields = ('user__email', 'provider_email', 'provider_name')
    readonly_fields = ('provider_user_id', 'created_at', 'last_used_at')


@admin.register(UserSession)
class UserSessionAdmin(admin.ModelAdmin):
    list_display = ('user', 'device_name', 'ip_address', 'is_active', 'created_at', 'expires_at')
    list_filter = ('is_active', 'is_revoked', 'created_at')
    search_fields = ('user__email', 'ip_address', 'device_name')
    readonly_fields = ('session_id', 'created_at', 'revoked_at')
    
    def has_add_permission(self, request):
        return False 



@admin.register(User)
class UserAdmin(BaseUserAdmin):
    ordering = ('email',)

    list_display = ('email', 'tenant', 'role', 'is_active', 'created_at','email_verified')
    list_filter = ('role', 'is_active', 'tenant')
    search_fields = ('email', 'tenant__name')
    fieldsets = (
        (None, {'fields': ('email', 'password')}),
        ('Info', {'fields': ('first_name', 'last_name', 'tenant', 'role','email_verified')}),
        ('Permissions', {'fields': ('is_active', 'is_staff', 'is_superuser', 'is_superadmin')}),
    )


@admin.register(UserInvitation)
class UserInvitationAdmin(admin.ModelAdmin):
    list_display = ('email', 'tenant', 'status', 'expires_at')
    list_filter = ('status', 'tenant')
    search_fields = ('email', 'tenant__name')
    readonly_fields = ('token', 'created_at')


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ('user_email', 'action', 'tenant', 'created_at')
    list_filter = ('action', 'tenant', 'created_at')
    search_fields = ('user_email', 'action')
    readonly_fields = ('id', 'created_at')
    
    def has_add_permission(self, request):
        return False
    
    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(APIKey)
class APIKeyAdmin(admin.ModelAdmin):
    list_display = ('name', 'tenant', 'is_active', 'last_used_at', 'created_at')
    list_filter = ('is_active', 'tenant')
    search_fields = ('name', 'tenant__name')
    readonly_fields = ('key', 'created_at')


@admin.register(TenantConfiguration)
class TenantConfigurationAdmin(admin.ModelAdmin):
    list_display = ('tenant', 'alert_confidence_threshold', 'log_retention_days')
    search_fields = ('tenant__name',)