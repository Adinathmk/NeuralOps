from typing import Optional

import redis.asyncio as aioredis

from app.core.config import get_settings

_settings = get_settings()


def get_redis() -> aioredis.Redis:
    """Return a new async Redis client instance.

    Not cached globally because Celery workers use asyncio.run()
    which creates a new event loop per execution, and global pools
    would bind to a closed event loop on subsequent tasks.
    """
    return aioredis.from_url(
        _settings.REDIS_URL,
        encoding="utf-8",
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=2,
    )
