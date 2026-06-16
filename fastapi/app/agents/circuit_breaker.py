"""
fastapi/app/agents/circuit_breaker.py

Redis-Backed Circuit Breaker for Phase 4 AI Agent Pipeline

Tracks fault-tolerance state (CLOSED / OPEN / HALF_OPEN) per external
dependency in Redis so that ALL Celery worker replicas share the same
circuit state. This prevents the scenario where replica A observes the
circuit as OPEN but replica B still hammers a failing dependency because
it has only in-process state.

States
------
CLOSED   — Normal operation. All requests pass through.
OPEN     — Failure threshold exceeded. All requests are blocked.
           After timeout_seconds the circuit moves to HALF_OPEN.
HALF_OPEN — One probe request is allowed through.
            Success  → decrement failure count; if successes >= success_threshold → CLOSED.
            Failure  → return immediately to OPEN, reset opened_at.

Redis keys (all scoped to the circuit name)
-------------------------------------------
  cb:{name}:state       → "CLOSED" | "OPEN" | "HALF_OPEN"
  cb:{name}:failures    → integer failure count (reset on CLOSED transition)
  cb:{name}:successes   → integer success count while HALF_OPEN
  cb:{name}:opened_at   → unix float timestamp when circuit last opened

Usage
-----
    breaker = CircuitBreaker(
        name="openai_analyzer",
        failure_threshold=5,
        success_threshold=2,
        timeout_seconds=30,
    )

    if not await breaker.can_execute(redis):
        raise CircuitOpenError("openai_analyzer")

    try:
        result = await call_openai(...)
        await breaker.record_success(redis)
    except Exception:
        await breaker.record_failure(redis)
        raise
"""

from __future__ import annotations

import logging
import time
from enum import Enum
from typing import Any

import redis.asyncio as aioredis

logger = logging.getLogger(__name__)


class CircuitState(str, Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class CircuitOpenError(RuntimeError):
    """Raised when a call is attempted against an OPEN circuit."""

    def __init__(self, name: str) -> None:
        super().__init__(f"Circuit breaker '{name}' is OPEN — request blocked.")
        self.name = name


class CircuitBreaker:
    """
    Async, Redis-backed circuit breaker.

    All methods accept the redis client as an explicit parameter so this
    class remains stateless beyond configuration, making it safe to
    instantiate once at module level and share across tasks.

    Parameters
    ----------
    name : str
        Unique name for this circuit (used as Redis key prefix).
    failure_threshold : int
        Number of consecutive failures required to open the circuit.
    success_threshold : int
        Number of consecutive successes in HALF_OPEN required to close.
    timeout_seconds : int
        Seconds to wait in OPEN state before transitioning to HALF_OPEN.
    """

    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        success_threshold: int = 2,
        timeout_seconds: int = 30,
    ) -> None:
        self.name = name
        self.failure_threshold = failure_threshold
        self.success_threshold = success_threshold
        self.timeout_seconds = timeout_seconds

    # ------------------------------------------------------------------
    # Private key helpers
    # ------------------------------------------------------------------

    def _state_key(self) -> str:
        return f"cb:{self.name}:state"

    def _failures_key(self) -> str:
        return f"cb:{self.name}:failures"

    def _successes_key(self) -> str:
        return f"cb:{self.name}:successes"

    def _opened_at_key(self) -> str:
        return f"cb:{self.name}:opened_at"

    async def _get_state(self, redis: aioredis.Redis) -> str:
        """Return current state string, defaulting to CLOSED if key missing."""
        raw = await redis.get(self._state_key())
        if raw is None:
            return CircuitState.CLOSED.value
        # redis.asyncio returns bytes when decode_responses=False
        if isinstance(raw, bytes):
            return raw.decode("utf-8")
        return str(raw)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def can_execute(self, redis: aioredis.Redis) -> bool:
        """
        Determine whether a request should be allowed through.

        Returns
        -------
        bool
            True  — request may proceed (CLOSED or HALF_OPEN probe allowed).
            False — request must be blocked (OPEN and timeout not yet elapsed).
        """
        try:
            state = await self._get_state(redis)

            if state == CircuitState.CLOSED.value:
                return True

            if state == CircuitState.OPEN.value:
                raw_opened = await redis.get(self._opened_at_key())
                if raw_opened is None:
                    # Key expired or was deleted — treat as closed
                    await redis.set(self._state_key(), CircuitState.CLOSED.value)
                    return True

                opened_at = float(
                    raw_opened.decode("utf-8")
                    if isinstance(raw_opened, bytes)
                    else raw_opened
                )
                elapsed = time.time() - opened_at

                if elapsed >= self.timeout_seconds:
                    # Transition to HALF_OPEN to probe
                    await redis.set(self._state_key(), CircuitState.HALF_OPEN.value)
                    await redis.set(self._successes_key(), "0")
                    logger.info(
                        "circuit_breaker_half_open",
                        extra={
                            "circuit": self.name,
                            "elapsed_seconds": round(elapsed, 1),
                        },
                    )
                    return True

                # Still within timeout — block request
                logger.debug(
                    "circuit_breaker_open_blocking",
                    extra={
                        "circuit": self.name,
                        "seconds_remaining": round(self.timeout_seconds - elapsed, 1),
                    },
                )
                return False

            if state == CircuitState.HALF_OPEN.value:
                # Allow exactly one probe through; subsequent concurrent
                # requests are still blocked until the probe resolves
                return True

            # Unknown state — default safe
            return True

        except aioredis.RedisError as exc:
            # Redis unavailable — fail open to avoid blocking all requests
            logger.warning(
                "circuit_breaker_redis_error_fail_open",
                extra={"circuit": self.name, "error": str(exc)},
            )
            return True

    async def record_success(self, redis: aioredis.Redis) -> None:
        """
        Record a successful call.

        In HALF_OPEN: if successes >= success_threshold, close the circuit.
        In CLOSED: no-op (no state change needed).
        """
        try:
            state = await self._get_state(redis)

            if state == CircuitState.HALF_OPEN.value:
                successes = await redis.incr(self._successes_key())

                if successes >= self.success_threshold:
                    # Transition to CLOSED
                    pipe = redis.pipeline()
                    pipe.set(self._state_key(), CircuitState.CLOSED.value)
                    pipe.set(self._failures_key(), "0")
                    pipe.set(self._successes_key(), "0")
                    pipe.delete(self._opened_at_key())
                    await pipe.execute()

                    logger.info(
                        "circuit_breaker_closed",
                        extra={
                            "circuit": self.name,
                            "successes_required": self.success_threshold,
                        },
                    )

            elif state == CircuitState.CLOSED.value:
                # Reset failure counter on any success while closed
                await redis.set(self._failures_key(), "0")

        except aioredis.RedisError as exc:
            logger.warning(
                "circuit_breaker_record_success_redis_error",
                extra={"circuit": self.name, "error": str(exc)},
            )

    async def record_failure(self, redis: aioredis.Redis) -> None:
        """
        Record a failed call.

        In CLOSED: increment failure counter; if >= failure_threshold, open circuit.
        In HALF_OPEN: immediately reopen circuit (probe failed).
        In OPEN: no-op.
        """
        try:
            state = await self._get_state(redis)

            if state == CircuitState.OPEN.value:
                return  # Already open — nothing to do

            if state == CircuitState.HALF_OPEN.value:
                # Probe failed — reopen immediately
                pipe = redis.pipeline()
                pipe.set(self._state_key(), CircuitState.OPEN.value)
                pipe.set(self._opened_at_key(), str(time.time()))
                pipe.set(self._successes_key(), "0")
                await pipe.execute()

                logger.warning(
                    "circuit_breaker_reopened_from_half_open",
                    extra={"circuit": self.name},
                )
                return

            # CLOSED state — increment failure count
            failures = await redis.incr(self._failures_key())

            if failures >= self.failure_threshold:
                # Open the circuit
                pipe = redis.pipeline()
                pipe.set(self._state_key(), CircuitState.OPEN.value)
                pipe.set(self._opened_at_key(), str(time.time()))
                pipe.set(self._successes_key(), "0")
                await pipe.execute()

                logger.error(
                    "circuit_breaker_opened",
                    extra={
                        "circuit": self.name,
                        "failure_count": failures,
                        "failure_threshold": self.failure_threshold,
                        "will_retry_after_seconds": self.timeout_seconds,
                    },
                )
            else:
                logger.warning(
                    "circuit_breaker_failure_recorded",
                    extra={
                        "circuit": self.name,
                        "failure_count": failures,
                        "failure_threshold": self.failure_threshold,
                    },
                )

        except aioredis.RedisError as exc:
            logger.warning(
                "circuit_breaker_record_failure_redis_error",
                extra={"circuit": self.name, "error": str(exc)},
            )

    async def get_state(self, redis: aioredis.Redis) -> CircuitState:
        """Return the current CircuitState enum value."""
        raw = await self._get_state(redis)
        try:
            return CircuitState(raw)
        except ValueError:
            return CircuitState.CLOSED

    async def reset(self, redis: aioredis.Redis) -> None:
        """
        Manually reset the circuit to CLOSED state.
        Used by operators via a management command to recover after a
        prolonged outage once the dependency is confirmed healthy.
        """
        pipe = redis.pipeline()
        pipe.set(self._state_key(), CircuitState.CLOSED.value)
        pipe.set(self._failures_key(), "0")
        pipe.set(self._successes_key(), "0")
        pipe.delete(self._opened_at_key())
        await pipe.execute()

        logger.info(
            "circuit_breaker_manually_reset",
            extra={"circuit": self.name},
        )
