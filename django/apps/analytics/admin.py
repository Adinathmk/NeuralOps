from django.contrib import admin

from .models import IncidentSnapshot


@admin.register(IncidentSnapshot)
class IncidentSnapshotAdmin(admin.ModelAdmin):
    list_display = (
        "incident_id",
        "tenant",
        "status",
        "severity",
        "confidence_score",
        "service_name",
        "synced_at",
        "created_at",
    )
    list_filter = ("status", "severity", "tenant")
    search_fields = ("incident_id", "service_name", "error_type", "tenant__name")
    readonly_fields = ("incident_id", "synced_at", "created_at", "source_version")

    def has_add_permission(self, request):
        return False  # Snapshots are written by the FastAPI consumer, not manually
