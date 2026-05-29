import uuid

from django.utils import timezone


def write_outbox(topic: str, key: str, payload: dict, source_version: int = None):
    """
    Write an event to the outbox table.
    Must be called INSIDE an active database transaction.
    Debezium tails the WAL and delivers to Kafka automatically.
    """
    from outbox.models import OutboxEvent

    OutboxEvent.objects.create(
        topic=topic,
        key=key,
        payload=payload,
    )
