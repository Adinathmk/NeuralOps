import uuid
from django.db import models

class BillingEvent(models.Model):
    """
    Idempotency log for Razorpay webhooks to prevent duplicate processing.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    razorpay_event_id = models.CharField(max_length=255, unique=True, db_index=True)
    event_type = models.CharField(max_length=100)
    payload = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "billing_events"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.event_type} - {self.razorpay_event_id}"
