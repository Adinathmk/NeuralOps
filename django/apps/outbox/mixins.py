import uuid
from django.utils import timezone

def write_outbox(topic: str, key: str, payload: dict, source_version: int = None):
    """
    Write an event to the outbox table.
    Must be called INSIDE an active database transaction.
    Debezium tails the WAL and delivers to Kafka automatically.
    """
    from outbox.models import OutboxEvent
    
    envelope = {
        'event_id': str(uuid.uuid4()),
        'event_type': topic,
        'version': 1,
        'idempotency_key': key,
        'source_version': source_version,
        'occurred_at': timezone.now().isoformat(),
        'payload': payload,
    }
    
    OutboxEvent.objects.create(
        topic=topic,
        key=key,
        payload=envelope,
    )