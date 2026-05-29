from django.contrib import admin

from .models import Playbook


@admin.register(Playbook)
class PlaybookAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "tenant",
        "error_pattern_preview",
        "source_version",
        "created_at",
        "updated_at",
    )
    list_filter = ("tenant",)
    search_fields = ("id", "tenant__name", "error_pattern")
    readonly_fields = ("id", "source_version", "created_at", "updated_at")
    ordering = ("-created_at",)

    fieldsets = (
        ("Identity", {"fields": ("id", "tenant")}),
        ("Pattern & Instructions", {"fields": ("error_pattern", "instructions")}),
        (
            "Versioning & Timestamps",
            {"fields": ("source_version", "created_at", "updated_at")},
        ),
    )

    @admin.display(description="Error Pattern (preview)")
    def error_pattern_preview(self, obj):
        pattern = obj.error_pattern or ""
        return pattern[:60] + "…" if len(pattern) > 60 else pattern
