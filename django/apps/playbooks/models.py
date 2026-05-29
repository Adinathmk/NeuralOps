import uuid

from django.db import models
from tenants.models import Tenant


class Playbook(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="playbooks",
    )
    error_pattern = models.TextField(
        help_text="Regex pattern matched against incoming log error messages.",
    )
    instructions = models.TextField(
        help_text="Runbook steps injected into the AI agent's analysis prompt when this pattern matches.",
    )
    source_version = models.BigIntegerField(
        default=1,
        help_text="Auto-incremented on every save; used by snapshot consumers to reject stale events.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "playbooks"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["tenant"]),
        ]

    def save(self, *args, **kwargs):
        # Increment source_version on every update (not on initial create).
        if not self._state.adding:
            Playbook.objects.filter(pk=self.pk).update(
                source_version=models.F("source_version") + 1
            )
            self.source_version = (
                Playbook.objects.filter(pk=self.pk)
                .values_list("source_version", flat=True)
                .get()
            )
        super().save(*args, **kwargs)

    def __str__(self):
        return f"Playbook({self.id}) tenant={self.tenant_id}"
