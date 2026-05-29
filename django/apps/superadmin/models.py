import uuid

from django.db import models


class SuperAdminAuditLog(models.Model):
    ACTION_CHOICES = [
        ("tenant_suspended", "Tenant Suspended"),
        ("tenant_reinstated", "Tenant Reinstated"),
        ("tenant_viewed", "Tenant Viewed"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    actor_user_id = models.UUIDField()
    action = models.CharField(max_length=50, choices=ACTION_CHOICES)
    target_tenant_id = models.UUIDField(null=True, blank=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "superadmin_audit_logs"
        ordering = ["-created_at"]
