import uuid

from django.db import models
from tenants.models import Tenant


class AlertRule(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="alert_rules",
    )
    confidence_threshold = models.FloatField(
        default=0.70,
        help_text="Minimum confidence score (0.0–1.0) to trigger this alert.",
    )
    severity_filter = models.JSONField(
        default=list,
        blank=True,
        help_text='List of severity levels, e.g. ["critical", "high"].',
    )
    destinations = models.JSONField(
        default=list,
        blank=True,
        help_text="List of destination objects (e.g. {'type': 'user', 'id': 'uuid'} or {'type': 'pagerduty', 'webhook_url': 'url'}).",
    )
    enabled = models.BooleanField(default=True)
    source_version = models.BigIntegerField(
        default=1,
        help_text="Auto-incremented on every save; used by snapshot consumers to reject stale events.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "alert_rules"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["tenant", "enabled"]),
        ]

    def save(self, *args, **kwargs):
        self.clean()
        # Increment source_version on every update (not on initial create).
        if not self._state.adding:
            AlertRule.objects.filter(pk=self.pk).update(
                source_version=models.F("source_version") + 1
            )
            # Refresh from the expression result so the instance is accurate.
            self.source_version = (
                AlertRule.objects.filter(pk=self.pk)
                .values_list("source_version", flat=True)
                .get()
            )
        super().save(*args, **kwargs)

    def clean(self):
        super().clean()
        from .destinations import validate_destinations_list
        from rest_framework.exceptions import ValidationError as DRFValidationError
        from django.core.exceptions import ValidationError
        
        try:
            # Reassign so that validated and normalized data is saved
            self.destinations = validate_destinations_list(self.destinations)
        except DRFValidationError as e:
            raise ValidationError({'destinations': str(e.detail)})

    def __str__(self):
        return f"AlertRule({self.id}) tenant={self.tenant_id} enabled={self.enabled}"
