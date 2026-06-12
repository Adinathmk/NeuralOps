"""
fastapi/app/services/dedup_lock.py

Phase 4 — Redis SETNX Distributed Lock for Incident Deduplication

Provides a context-manager-style distributed lock backed by Redis.
Used by run_agent to prevent multiple concurrent workers from all
passing the fingerprint DB check simultaneously and creating duplicate
incident rows during a high-volume ingest spike.

Problem being solved
--------------------
Consider 50 concurrent log events all carrying the same error fingerprint
arriving within milliseconds of each other. Without a lock:

  Worker 1:  SELECT fingerprint → not found → INSERT incident  ✓
  Worker 2:  SELECT fingerprint → not found (Worker 1 not committed yet!)
             → INSERT incident → UNIQUE CONSTRAINT VIOLATION  ✗
  Workers 3–50: same race as Worker 2

With the Redis SETNX lock:

  Worker 1:  SETNX lock_key → acquired → SELECT → INSERT  ✓
  Workers 2–50: SETNX lock_key → NOT acquired → return "lock_contention"
  (Workers 2–50 do nothing; Worker 1's incident covers all their occurrences
   once the duplicate path catches up on the next batch)

Lock design
-----------
Key pattern:  dedup:lock:{fingerprint}
Value:        {current_incident_id}  (stored for debugging; not used in logic)
TTL:          DEDUP_LOCK_TTL_SECONDS (default: 30 seconds)

The TTL ensures the lock is automatically released even if the worker
crashes mid-execution. 30 seconds is deliberately generous — a full
GPT-4 analysis run takes 3–8 seconds at p99. The lock must live long
enough to cover the entire run_agent execution including DB writes.

Failure semantics
-----------------
- Lock NOT acquired → return False immediately (non-blocking)
  The calling code treats this as "lock contention — skip quietly".
  This is correct because the worker that DID acquire the lock will
  handle the new incident. Non-acquiring workers should not retry.

- Redis unavailable → log warning, return True (fail-open)
  Failing open means we proceed WITHOUT the lock when Redis is down.
  The PostgreSQL partial unique index is the second safety layer and
  will catch any resulting concurrent INSERT conflicts with an
  IntegrityError that run_agent handles gracefully.

- Lock acquired but worker crashes before explicit release → TTL expires
  The lock auto-releases after DEDUP_LOCK_TTL_SECONDS seconds.
  This prevents permanent lock starvation.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

import redis.asyncio as aioredis

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# TTL for the deduplication lock in seconds.
# Must be longer than the maximum expected run_agent execution time (p99).
# GPT-4 calls take 3–8s at p99. DB writes take < 50ms.
# We use 30s to provide a 3–4x safety margin.
DEDUP_LOCK_TTL_SECONDS: int = 30

# Redis key prefix for deduplication locks
_LOCK_KEY_PREFIX: str = "dedup:lock:"


# ---------------------------------------------------------------------------
# Key builder
# ---------------------------------------------------------------------------

def dedup_lock_key(fingerprint: str) -> str:
    """
    Build the Redis key for a deduplication lock.

    Parameters
    ----------
    fingerprint : str
        64-character hex fingerprint of the error identity.

    Returns
    -------
    str
        Redis key in the format: dedup:lock:{fingerprint}
    """
    return f"{_LOCK_KEY_PREFIX}{fingerprint}"


# ---------------------------------------------------------------------------
# Acquire / release (explicit API — used by run_agent directly)
# ---------------------------------------------------------------------------

async def acquire_dedup_lock(
    redis: aioredis.Redis,
    fingerprint: str,
    owner_id: str,
) -> bool:
    """
    Attempt to acquire the deduplication lock for a fingerprint.

    Uses Redis SET NX EX (atomic set-if-not-exists with expiry).
    This is the correct Redis primitive — it is atomic and avoids the
    SETNX + EXPIRE race condition present in older Redis clients.

    Parameters
    ----------
    redis : aioredis.Redis
        An open async Redis connection.
    fingerprint : str
        64-character hex fingerprint to lock on.
    owner_id : str
        A unique identifier for the lock owner (the current incident_id
        or Celery task ID). Stored as the key value for debugging.
        Not used in lock release logic.

    Returns
    -------
    bool
        True  — lock acquired; this worker is the sole processor for
                this fingerprint until the lock is released or expires.
        False — lock NOT acquired; another worker holds it. The caller
                should return immediately without processing.

    Notes
    -----
    On Redis connection failure, logs a warning and returns True (fail-open).
    The PostgreSQL partial unique index acts as the fallback safety layer.
    """
    lock_key: str = dedup_lock_key(fingerprint)

    try:
        # SET key value NX EX ttl
        # NX = only set if key does NOT exist (SETNX semantics)
        # EX = set expiry in seconds
        # Returns True if the key was set, None if it already existed
        result = await redis.set(
            lock_key,
            owner_id,
            nx=True,            # Only set if Not eXists
            ex=DEDUP_LOCK_TTL_SECONDS,
        )

        acquired: bool = result is True

        logger.debug(
            "dedup_lock_acquire_attempt",
            extra={
                "fingerprint_prefix": fingerprint[:16],
                "lock_key": lock_key,
                "owner_id": owner_id,
                "acquired": acquired,
            },
        )

        return acquired

    except aioredis.RedisError as exc:
        # Redis unavailable — fail open so ingestion is not blocked.
        # The DB partial unique index will catch concurrent duplicates.
        logger.warning(
            "dedup_lock_redis_error_fail_open",
            extra={
                "fingerprint_prefix": fingerprint[:16],
                "lock_key": lock_key,
                "error": str(exc),
                "action": "proceeding_without_lock",
            },
        )
        return True  # Fail-open: allow execution to proceed


async def release_dedup_lock(
    redis: aioredis.Redis,
    fingerprint: str,
) -> None:
    """
    Release the deduplication lock for a fingerprint.

    Deletes the Redis key unconditionally. This is correct because:
      - Only one worker ever holds the lock (NX ensures this).
      - The worker that acquired it is the only one that calls release.
      - We do not need Lua compare-and-delete because there is no
        competing writer for this exact key during the lock period.

    Parameters
    ----------
    redis : aioredis.Redis
        An open async Redis connection.
    fingerprint : str
        64-character hex fingerprint to release the lock for.

    Notes
    -----
    - Called in a finally block in run_agent to guarantee release.
    - Errors are caught and logged but NOT re-raised, so a release
      failure never masks the original exception from run_agent.
    - If the worker crashed and the lock was never explicitly released,
      the TTL will expire the key automatically.
    """
    lock_key: str = dedup_lock_key(fingerprint)

    try:
        deleted: int = await redis.delete(lock_key)
        logger.debug(
            "dedup_lock_released",
            extra={
                "fingerprint_prefix": fingerprint[:16],
                "lock_key": lock_key,
                "key_existed": deleted > 0,
            },
        )
    except aioredis.RedisError as exc:
        # Log but never re-raise — we are in a finally block.
        logger.warning(
            "dedup_lock_release_failed",
            extra={
                "fingerprint_prefix": fingerprint[:16],
                "lock_key": lock_key,
                "error": str(exc),
            },
        )


# ---------------------------------------------------------------------------
# Context manager API (alternative usage pattern)
# ---------------------------------------------------------------------------

@asynccontextmanager
async def dedup_lock(
    redis: aioredis.Redis,
    fingerprint: str,
    owner_id: str,
) -> AsyncIterator[bool]:
    """
    Async context manager for the deduplication lock.

    Yields True if the lock was acquired, False if not.
    The lock is always released in the __aexit__ regardless of exceptions.

    Usage
    -----
    async with dedup_lock(redis, fingerprint, owner_id) as acquired:
        if not acquired:
            return {"action": "lock_contention"}
        # ... proceed with analysis ...

    Note: run_agent uses the explicit acquire/release API (not this
    context manager) because it needs to release the lock in a finally
    block after an asyncio.run() boundary. Both patterns are provided
    for completeness.
    """
    acquired: bool = await acquire_dedup_lock(redis, fingerprint, owner_id)
    try:
        yield acquired
    finally:
        if acquired:
            await release_dedup_lock(redis, fingerprint)