from django.db import models


class IncidentSnapshot(models.Model):
    incident_id = models.UUIDField(primary_key=True)
    tenant = models.ForeignKey("tenants.Tenant", on_delete=models.CASCADE)
    status = models.CharField(max_length=32)
    severity = models.CharField(max_length=32)
    confidence_score = models.FloatField(null=True)
    error_type = models.CharField(max_length=255, blank=True)
    service_name = models.CharField(max_length=255, blank=True)
    assigned_user_id = models.UUIDField(null=True, blank=True)
    source_version = models.BigIntegerField(default=0)
    synced_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField()

    class Meta:
        db_table = "incident_snapshots"
        indexes = [
            models.Index(fields=["tenant", "status"]),
            models.Index(fields=["tenant", "created_at"]),
        ]
