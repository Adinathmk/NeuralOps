from typing import Optional
import redis.asyncio as aioredis
from app.core.config import get_settings

_settings = get_settings()
_redis_client: Optional[aioredis.Redis] = None

def get_redis() -> aioredis.Redis:
    """Return the module-level async Redis client, creating it on first call."""
    global _redis_client
    if _redis_client is None:
        _redis_client = aioredis.from_url(
            _settings.REDIS_URL,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
    return _redis_client
