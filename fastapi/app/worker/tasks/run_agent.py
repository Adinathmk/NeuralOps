"""
fastapi/app/worker/tasks/run_agent.py

Phase 4 — Parts 3 & 4 will implement this task in full.

This stub exists so that parse_log can import and enqueue run_agent
without a circular import error. The stub task accepts the correct
arguments and logs receipt, but performs no analysis.

DO NOT DEPLOY TO PRODUCTION in stub form.
Replace entirely with the full implementation from Parts 3 & 4.
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from app.worker.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(
    name="app.worker.tasks.run_agent.run_agent",
    bind=True,
    acks_late=True,
    reject_on_worker_lost=True,
    max_retries=5,
    default_retry_delay=10,
    soft_time_limit=480,
    time_limit=600,
)
def run_agent(
    self,
    *,
    parsed_event: Dict[str, Any],
) -> Dict[str, Any]:
    """
    STUB — Full implementation delivered in Parts 3 & 4.

    Accepts a ParsedLogEvent dict from parse_log and will orchestrate
    the full LangGraph agent pipeline including:
      - SHA-256 fingerprint computation
      - Redis distributed deduplication lock
      - DB-2 active incident check
      - LangGraph node execution
      - Incident + Analysis persistence
      - Outbox event publication
    """
    tenant_id = parsed_event.get("tenant_id", "unknown")
    incident_id = parsed_event.get("incident_id", "unknown")
    error_type = parsed_event.get("error_type", "unknown")

    logger.info(
        "run_agent_stub_received",
        extra={
            "tenant_id": tenant_id,
            "incident_id": incident_id,
            "error_type": error_type,
            "task_id": self.request.id,
            "note": "STUB — Parts 3 & 4 not yet implemented.",
        },
    )

    # Return a stub result so parse_log's calling code does not break
    return {
        "action": "stub",
        "tenant_id": tenant_id,
        "incident_id": incident_id,
        "note": "run_agent stub — replace with full implementation in Parts 3 & 4",
    }