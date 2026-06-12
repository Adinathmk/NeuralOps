"""
Uses channels_redis directly to publish to Django's channel layer.
This is the cleanest approach — same library, guaranteed wire compatibility.
"""
import asyncio
from channels_redis.core import RedisChannelLayer
from app.core.config import get_settings

settings = get_settings()

_channel_layer: RedisChannelLayer | None = None


def get_channel_layer() -> RedisChannelLayer:
    global _channel_layer
    if _channel_layer is None:
        # Django Channels listens on DB 0. FastAPI's default REDIS_URL uses DB 1.
        # We must explicitly connect the publisher to DB 0 to bridge the events.
        django_redis_url = settings.REDIS_URL.replace("/1", "/0")
        _channel_layer = RedisChannelLayer(
            hosts=[django_redis_url],
            capacity=1500,
            expiry=10,
        )
    return _channel_layer


async def notify_incident_analysis_complete(
    incident_id: str,
    tenant_id: str,
    analysis_data: dict
) -> None:
    layer = get_channel_layer()
    await layer.group_send(
        f"incident_{incident_id}",
        {
            "type": "incident.analysis_complete",
            "data": analysis_data
        }
    )
    # Also push to the collaboration channel so all tenant users see it
    await layer.group_send(
        f"collaboration_{tenant_id}",
        {
            "type": "collaboration.incident_created",
            "data": analysis_data
        }
    )


async def notify_duplicate_recorded(
    incident_id: str,
    occurrence_count: int
) -> None:
    layer = get_channel_layer()
    await layer.group_send(
        f"incident_{incident_id}",
        {
            "type": "incident.update",
            "data": {
                "incident_id": incident_id,
                "occurrence_count": occurrence_count,
                "update_type": "duplicate_recorded",
            }
        }
    )
