"""
fastapi/app/services/github_auth.py

Handles authentication as a GitHub App installation.
Generates JWTs signed with the App's private key, and exchanges them
for short-lived installation access tokens.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import httpx
import jwt
import redis.asyncio as aioredis

from app.core.config import get_settings

logger = logging.getLogger(__name__)


def generate_app_jwt() -> str:
    """
    Generate a 10-minute JWT signed with the GitHub App private key.
    """
    settings = get_settings()
    app_id = settings.GITHUB_APP_ID
    private_key = settings.GITHUB_APP_PRIVATE_KEY

    if not app_id or not private_key:
        raise RuntimeError("GITHUB_APP_ID or GITHUB_APP_PRIVATE_KEY is not configured.")

    now = int(time.time())

    payload = {
        "iat": now - 60,  # Issued 60s ago to allow for clock drift
        "exp": now + (5 * 60),  # Expires in 5 minutes (avoids clock skew 401s)
        "iss": str(app_id),
    }

    try:
        encoded_jwt = jwt.encode(payload, private_key, algorithm="RS256")
        return encoded_jwt
    except Exception as e:
        logger.error(f"Failed to generate GitHub App JWT: {e}")
        raise RuntimeError(f"Failed to generate GitHub App JWT: {e}") from e


async def get_installation_token(
    installation_id: int, redis_client: Optional[aioredis.Redis] = None
) -> str:
    """
    Fetch a short-lived access token for a given GitHub App installation ID.
    Tokens are cached in Redis for 55 minutes to avoid hitting rate limits.
    """
    cache_key = f"github:token:{installation_id}"

    settings = get_settings()
    owns_redis = False

    if redis_client is None:
        redis_client = aioredis.from_url(
            settings.REDIS_URL,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=5,
        )
        owns_redis = True

    try:
        # 1. Check Redis cache
        cached_token = await redis_client.get(cache_key)
        if cached_token:
            return (
                cached_token
                if isinstance(cached_token, str)
                else cached_token.decode("utf-8")
            )

        # 2. Cache miss, fetch new token
        logger.info(
            f"Fetching new GitHub App installation token for installation_id={installation_id}"
        )
        app_jwt = generate_app_jwt()

        url = (
            f"https://api.github.com/app/installations/{installation_id}/access_tokens"
        )
        headers = {
            "Authorization": f"Bearer {app_jwt}",
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "NeuralOps-FastAPI/1.0",
        }

        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(url, headers=headers)

        if response.status_code != 201:
            logger.error(
                "github_auth_token_fetch_failed",
                extra={
                    "status_code": response.status_code,
                    "response_body": response.text[:500],
                    "installation_id": installation_id,
                },
            )
            raise RuntimeError(
                f"Failed to fetch GitHub installation token. HTTP {response.status_code}: {response.text}"
            )

        data = response.json()
        token = data.get("token")

        if not token:
            raise RuntimeError("GitHub API response missing 'token' field.")

        # 3. Cache the token for 55 minutes (tokens expire in 60 minutes)
        await redis_client.setex(cache_key, 55 * 60, token)

        return token

    except httpx.RequestError as e:
        logger.error(f"Network error while fetching GitHub token: {e}")
        raise RuntimeError(f"Network error while fetching GitHub token: {e}") from e
    finally:
        if owns_redis:
            await redis_client.aclose()
