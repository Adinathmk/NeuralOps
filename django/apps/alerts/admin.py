from django.contrib import admin

from .models import AlertRule


@admin.register(AlertRule)
class AlertRuleAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "tenant",
        "confidence_threshold",
        "enabled",
        "source_version",
        "created_at",
        "updated_at",
    )
    list_filter = ("enabled", "tenant")
    search_fields = ("id", "tenant__name")
    readonly_fields = ("id", "source_version", "created_at", "updated_at")
    ordering = ("-created_at",)

    fieldsets = (
        ("Identity", {"fields": ("id", "tenant")}),
        (
            "Rule Configuration",
            {
                "fields": (
                    "confidence_threshold",
                    "severity_filter",
                    "destinations",
                    "enabled",
                )
            },
        ),
        (
            "Versioning & Timestamps",
            {"fields": ("source_version", "created_at", "updated_at")},
        ),
    )
