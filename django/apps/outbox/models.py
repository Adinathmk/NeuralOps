import uuid
from django.db import models

class OutboxEvent(models.Model):
    event_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    topic = models.CharField(max_length=256)
    key = models.CharField(max_length=256)
    payload = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)
    published = models.BooleanField(default=False)

    class Meta:
        db_table = 'outbox'
        indexes = [models.Index(fields=['published', 'created_at'])]

class ProcessedEvent(models.Model):

    event_id = models.UUIDField(primary_key=True)

    consumer_group = models.CharField(
        max_length=128
    )

    topic = models.CharField(
        max_length=256
    )

    processed_at = models.DateTimeField(
        auto_now_add=True
    )

    class Meta:
        db_table = "processed_events"

        indexes = [
            models.Index(
                fields=["consumer_group", "topic"]
            )
        ]