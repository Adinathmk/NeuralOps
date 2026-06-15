"""
NeuralOps — Elasticsearch Circuit Breaker & Monitoring

Two concerns handled here:

1. Circuit breaker: if ES is down or slow, the ingest endpoint must not block.
   Log ingestion is on the critical path (200ms SLO). ES write failure
   must be non-fatal and fast-failing.

2. Prometheus metrics: track ES write latency, error rate, and circuit state
   so you can alert before ES degradation affects users.
"""

import asyncio
import logging
import time
from enum import Enum
from typing import Callable, Any, Optional
from prometheus_client import Counter, Histogram, Gauge

logger = logging.getLogger(__name__)


# ── PROMETHEUS METRICS ─────────────────────────────────────────────────────

es_index_total = Counter(
    "neuralops_es_index_total",
    "Total Elasticsearch document index attempts",
    ["result"],  # labels: "success" | "failure" | "circuit_open"
)

es_index_duration_seconds = Histogram(
    "neuralops_es_index_duration_seconds",
    "Elasticsearch index call duration",
    buckets=[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0],
)

es_search_duration_seconds = Histogram(
    "neuralops_es_search_duration_seconds",
    "Elasticsearch search call duration",
    buckets=[0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5],
)

es_circuit_state = Gauge(
    "neuralops_es_circuit_state",
    "Elasticsearch circuit breaker state: 0=closed 1=open 2=half-open",
)


# ── CIRCUIT BREAKER ────────────────────────────────────────────────────────

class CircuitState(Enum):
    CLOSED = "closed"       # Normal operation
    OPEN = "open"           # ES is down — fast-fail all calls
    HALF_OPEN = "half_open" # Testing if ES recovered


class ElasticsearchCircuitBreaker:
    """
    Simple circuit breaker for Elasticsearch calls.

    State machine:
    - CLOSED: all calls go through. Failure count tracked.
    - OPEN: all calls fast-fail. Checked every `probe_interval` seconds.
    - HALF_OPEN: one probe call allowed. Success → CLOSED. Failure → OPEN.

    Why implement this rather than using tenacity alone:
    - tenacity retries are per-call. The circuit breaker is global — after
      5 consecutive failures it stops trying ES entirely for 30 seconds.
    - This prevents every concurrent ingest request from hammering a down ES
      and all timing out at 5s each, blowing the 200ms ingest SLO.
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        probe_interval: float = 30.0,  # seconds
    ):
        self.failure_threshold = failure_threshold
        self.probe_interval = probe_interval

        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._last_failure_time: Optional[float] = None
        self._lock = asyncio.Lock()

    @property
    def state(self) -> CircuitState:
        return self._state

    async def call(self, fn: Callable, *args, **kwargs) -> Any:
        """
        Execute an ES call through the circuit breaker.
        Returns the result on success, None on fast-fail or error.
        """
        async with self._lock:
            if self._state == CircuitState.OPEN:
                elapsed = time.monotonic() - self._last_failure_time
                if elapsed >= self.probe_interval:
                    self._state = CircuitState.HALF_OPEN
                    es_circuit_state.set(2)
                    logger.info("ES circuit breaker: HALF_OPEN — sending probe")
                else:
                    es_index_total.labels(result="circuit_open").inc()
                    return None  # Fast-fail

        start = time.monotonic()
        try:
            result = await fn(*args, **kwargs)
            duration = time.monotonic() - start
            es_index_duration_seconds.observe(duration)
            es_index_total.labels(result="success").inc()

            # Successful call — reset circuit
            async with self._lock:
                self._failure_count = 0
                if self._state == CircuitState.HALF_OPEN:
                    self._state = CircuitState.CLOSED
                    es_circuit_state.set(0)
                    logger.info("ES circuit breaker: CLOSED — ES recovered")

            return result

        except Exception as e:
            duration = time.monotonic() - start
            es_index_duration_seconds.observe(duration)
            es_index_total.labels(result="failure").inc()

            async with self._lock:
                self._failure_count += 1
                self._last_failure_time = time.monotonic()

                if self._failure_count >= self.failure_threshold:
                    self._state = CircuitState.OPEN
                    es_circuit_state.set(1)
                    logger.error(
                        "ES circuit breaker: OPEN — ES appears down",
                        extra={"failure_count": self._failure_count},
                    )

            logger.warning(
                "ES call failed",
                extra={"error": str(e), "duration_ms": round(duration * 1000)},
            )
            return None


# Singleton circuit breaker — shared across all requests in the process
_es_circuit_breaker = ElasticsearchCircuitBreaker(
    failure_threshold=5,
    probe_interval=30.0,
)


def get_es_circuit_breaker() -> ElasticsearchCircuitBreaker:
    return _es_circuit_breaker


# ── ALERTING THRESHOLDS ────────────────────────────────────────────────────
"""
Add these Prometheus alert rules to your Alertmanager config:

# ES write error rate above 5% for 5 minutes
- alert: ESIndexErrorRateHigh
  expr: |
    rate(neuralops_es_index_total{result="failure"}[5m])
    /
    rate(neuralops_es_index_total[5m]) > 0.05
  for: 5m
  severity: warning

# ES circuit breaker is open
- alert: ESCircuitBreakerOpen
  expr: neuralops_es_circuit_state == 1
  for: 1m
  severity: critical

# ES search p99 latency above 2s
- alert: ESSearchLatencyHigh
  expr: histogram_quantile(0.99, neuralops_es_search_duration_seconds_bucket) > 2
  for: 5m
  severity: warning

# ES index p99 latency above 500ms (ingest SLO risk)
- alert: ESIndexLatencyHigh
  expr: histogram_quantile(0.99, neuralops_es_index_duration_seconds_bucket) > 0.5
  for: 5m
  severity: warning
"""
