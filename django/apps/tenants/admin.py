from django.contrib import admin

from .models import Tenant, TenantConfiguration


@admin.register(Tenant)
class TenantAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "plan_tier", "status", "created_at")
    search_fields = ("name", "slug")
    list_filter = ("plan_tier", "status")
    prepopulated_fields = {"slug": ("name",)}
    readonly_fields = ("id", "created_at", "updated_at")


@admin.register(TenantConfiguration)
class TenantConfigurationAdmin(admin.ModelAdmin):
    list_display = (
        "tenant",
        "alert_confidence_threshold",
        "log_retention_days",
        "enable_email_notifications",
        "updated_at",
    )
    search_fields = ("tenant__name",)
    readonly_fields = ("created_at", "updated_at")
