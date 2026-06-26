"""
fastapi/app/worker/tasks/run_agent.py

Phase 4 Final — run_agent Celery Task

Flow
----
  1. Deserialise ParsedLogEvent
  2. Compute SHA-256 fingerprint
  3. Acquire Redis SETNX dedup lock (Layer 1)
  4. Open AsyncSession, create IncidentService
  5. DB fingerprint query (Layer 2)
  6. Invoke LangGraph agent workflow (all nodes including patch_generator)
  7. Enforce draft rule
  8. Non-actionable guard
  9. persist_new_incident
 10. ws_notify
 11. Release Redis lock

  After asyncio.run() returns in the synchronous task wrapper:
 12. Dispatch create_github_pr.delay() if conditions are met.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid as _uuid_module
from typing import Any, Dict, Optional

import redis.asyncio as aioredis
import sqlalchemy.exc
from celery.utils.log import get_task_logger

from app.database.redis import get_redis
from app.database.session import AsyncSessionLocal
from app.schemas.parse_log import ParsedLogEvent
from app.services.dedup_lock import (
    DEDUP_LOCK_TTL_SECONDS,
    acquire_dedup_lock,
    release_dedup_lock,
)
from app.services.incidents import IncidentService, compute_fingerprint
from app.worker.celery_app import celery_app

logger = get_task_logger(__name__)

_AGENT_SOFT_TIME_LIMIT: int = 480
_AGENT_HARD_TIME_LIMIT: int = 600


# ---------------------------------------------------------------------------
# Core async logic
# ---------------------------------------------------------------------------


async def _execute_run_agent(
    task_id: str,
    parsed_event_dict: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Core async coroutine for the run_agent task.

    Returns a result dict that now includes structured_patch, root_cause,
    suggested_fix, and new_incident_id so the synchronous wrapper can
    dispatch the GitHub PR task without re-entering async context.
    """
    # ── Step 0: Deserialise ParsedLogEvent ────────────────────────────────────
    try:
        parsed_event: ParsedLogEvent = ParsedLogEvent.from_dict(parsed_event_dict)
    except Exception as exc:
        raise ValueError(
            f"Invalid ParsedLogEvent dict — cannot deserialise: {exc}"
        ) from exc

    tenant_id_str: str = parsed_event.tenant_id
    incident_id_str: str = parsed_event.incident_id

    try:
        tenant_uuid: _uuid_module.UUID = _uuid_module.UUID(tenant_id_str)
    except ValueError as exc:
        raise ValueError(f"Invalid tenant_id UUID: '{tenant_id_str}': {exc}") from exc

    # ── Step 1: Compute fingerprint ───────────────────────────────────────────
    fingerprint: str = compute_fingerprint(
        tenant_id=tenant_id_str,
        service_name=parsed_event.service_name,
        error_type=parsed_event.error_type,
        crash_file=parsed_event.crash_file,
        crash_line=parsed_event.crash_line,
        crash_method=parsed_event.crash_method,
    )

    logger.info(
        "run_agent_started",
        extra={
            "tenant_id": tenant_id_str,
            "incident_id": incident_id_str,
            "error_type": parsed_event.error_type,
            "severity": parsed_event.severity,
            "fingerprint_prefix": fingerprint[:16],
            "task_id": task_id,
        },
    )

    # ── Step 2: Acquire Redis deduplication lock (Layer 1) ────────────────────
    redis: aioredis.Redis = get_redis()

    lock_acquired: bool = await acquire_dedup_lock(
        redis=redis,
        fingerprint=fingerprint,
        owner_id=task_id,
    )

    if not lock_acquired:
        logger.info(
            "run_agent_lock_contention",
            extra={
                "tenant_id": tenant_id_str,
                "incident_id": incident_id_str,
                "fingerprint_prefix": fingerprint[:16],
                "task_id": task_id,
                "action": "lock_contention",
            },
        )
        return {
            "action": "lock_contention",
            "tenant_id": tenant_id_str,
            "incident_id": incident_id_str,
            "fingerprint": fingerprint,
        }

    agent_start_time: float = time.monotonic()

    try:
        async with AsyncSessionLocal() as session:

            # ── Step 2.5: Update Elasticsearch log document ───────────────────
            from sqlalchemy import select

            from app.models.snapshots import TenantSnapshot

            tenant_snapshot = await session.execute(
                select(TenantSnapshot).where(TenantSnapshot.tenant_id == tenant_uuid)
            )
            tenant = tenant_snapshot.scalar_one_or_none()
            plan_tier = tenant.plan_tier if tenant else "standard"

            from elasticsearch import AsyncElasticsearch

            from app.database.elasticsearch_client import get_settings

            es_settings = get_settings()

            es_kwargs = {
                "hosts": es_settings.ELASTICSEARCH_HOSTS,
                "connections_per_node": 10,
                "retry_on_timeout": True,
                "max_retries": 3,
                "request_timeout": 10,
                "sniff_on_start": False,
                "sniff_on_node_failure": False,
                "min_delay_between_sniffing": 60,
            }
            if (
                es_settings.ELASTICSEARCH_USERNAME
                and es_settings.ELASTICSEARCH_PASSWORD
            ):
                es_kwargs["basic_auth"] = (
                    es_settings.ELASTICSEARCH_USERNAME,
                    es_settings.ELASTICSEARCH_PASSWORD,
                )
            if any(
                host.startswith("https") for host in es_settings.ELASTICSEARCH_HOSTS
            ):
                es_kwargs["verify_certs"] = True
                if es_settings.ELASTICSEARCH_CA_CERT_PATH:
                    es_kwargs["ca_certs"] = es_settings.ELASTICSEARCH_CA_CERT_PATH
            else:
                es_kwargs["verify_certs"] = False

            es_client = AsyncElasticsearch(**es_kwargs)

            from app.services.log_event_indexer import LogEventIndexer

            es_indexer = LogEventIndexer(es_client=es_client)
            try:
                await es_indexer.update_parsed_fields(
                    incident_id=incident_id_str,
                    tenant_id=tenant_id_str,
                    plan_tier=plan_tier,
                    error_type=parsed_event.error_type,
                    file_path=parsed_event.crash_file,
                    line_number=parsed_event.crash_line,
                    severity=parsed_event.severity,
                )
            except Exception as e:
                logger.exception(
                    "es_update_parsed_fields_failed",
                    extra={"error": str(e), "incident_id": incident_id_str},
                )
            finally:
                await es_client.close()

            # ── Step 3: DB deduplication check (Layer 2) ─────────────────────
            incident_service = IncidentService(session)

            existing_incident = await incident_service.find_active_by_fingerprint(
                tenant_id=tenant_uuid,
                fingerprint=fingerprint,
            )

            if existing_incident is not None:
                new_count: int = await incident_service.record_duplicate_occurrence(
                    incident=existing_incident,
                    new_s3_key=parsed_event.s3_path,
                )

                from app.services.ws_publisher import notify_duplicate_recorded

                try:
                    await notify_duplicate_recorded(
                        incident_id=str(existing_incident.id),
                        occurrence_count=new_count,
                    )
                except Exception as exc:
                    logger.warning(
                        "ws_duplicate_notification_failed",
                        extra={"error": str(exc)},
                    )

                logger.info(
                    "run_agent_duplicate_recorded",
                    extra={
                        "tenant_id": tenant_id_str,
                        "incident_id": incident_id_str,
                        "existing_incident_id": str(existing_incident.id),
                        "new_occurrence_count": new_count,
                        "fingerprint_prefix": fingerprint[:16],
                        "task_id": task_id,
                        "action": "duplicate_recorded",
                    },
                )

                return {
                    "action": "duplicate_recorded",
                    "tenant_id": tenant_id_str,
                    "incident_id": incident_id_str,
                    "existing_incident_id": str(existing_incident.id),
                    "new_occurrence_count": new_count,
                    "fingerprint": fingerprint,
                }

            # ── Step 4: Invoke LangGraph agent workflow ───────────────────────
            from app.agents.workflow import get_agent_workflow

            workflow = get_agent_workflow()

            initial_state: Dict[str, Any] = {
                "tenant_id": tenant_id_str,
                "parsed_event": parsed_event_dict,
                "fingerprint": fingerprint,
                "session": session,
                "redis": redis,
            }

            agent_result: Dict[str, Any] = await workflow.ainvoke(initial_state)

            total_latency_ms: int = int((time.monotonic() - agent_start_time) * 1000)
            agent_result["total_latency_ms"] = total_latency_ms

            # ── Step 5: Determine action ──────────────────────────────────────
            action: str = agent_result.get("action", "store_draft")
            confidence_score: Optional[float] = agent_result.get("confidence_score")
            confidence_threshold: float = float(
                agent_result.get("confidence_threshold", 0.70)
            )

            if confidence_score is not None and confidence_score < confidence_threshold:
                action = "store_draft"
            is_draft: bool = action == "store_draft"

            if not agent_result.get("actionable", True):
                logger.info(
                    "run_agent_not_actionable",
                    extra={
                        "tenant_id": tenant_id_str,
                        "incident_id": incident_id_str,
                        "error_type": parsed_event.error_type,
                        "task_id": task_id,
                    },
                )
                return {
                    "action": "not_actionable",
                    "tenant_id": tenant_id_str,
                    "incident_id": incident_id_str,
                }

            # ── Step 6: Persist new Incident + Analysis + Outbox ─────────────
            try:
                persistence_result: Dict[str, _uuid_module.UUID] = (
                    await incident_service.persist_new_incident(
                        tenant_id=tenant_uuid,
                        parsed_event=parsed_event,
                        fingerprint=fingerprint,
                        agent_result=agent_result,
                        is_draft=is_draft,
                    )
                )

                from app.services.ws_publisher import notify_incident_analysis_complete

                ws_payload = {
                    "incident_id": str(persistence_result["incident_id"]),
                    "tenant_id": tenant_id_str,
                    "status": "open" if not is_draft else "draft",
                    "severity": agent_result.get("severity", "unknown"),
                    "error_type": parsed_event.error_type,
                    "service_name": parsed_event.service_name,
                    "root_cause": agent_result.get("root_cause", ""),
                    "suggested_fix": agent_result.get("suggested_fix", ""),
                    "confidence_score": agent_result.get("confidence_score"),
                    "crash_file": parsed_event.crash_file,
                    "crash_line": parsed_event.crash_line,
                    "crash_method": parsed_event.crash_method,
                    "is_draft": is_draft,
                    "total_latency_ms": total_latency_ms,
                }
                try:
                    await notify_incident_analysis_complete(
                        incident_id=str(persistence_result["incident_id"]),
                        tenant_id=tenant_id_str,
                        analysis_data=ws_payload,
                    )
                except Exception as exc:
                    logger.warning(
                        "ws_notification_failed",
                        extra={
                            "incident_id": str(persistence_result["incident_id"]),
                            "error": str(exc),
                        },
                    )

            except sqlalchemy.exc.IntegrityError as exc:
                logger.warning(
                    "run_agent_late_duplicate_detected",
                    extra={
                        "tenant_id": tenant_id_str,
                        "incident_id": incident_id_str,
                        "fingerprint_prefix": fingerprint[:16],
                        "error": str(exc),
                        "task_id": task_id,
                        "action": "late_duplicate",
                    },
                )
                return {
                    "action": "late_duplicate",
                    "tenant_id": tenant_id_str,
                    "incident_id": incident_id_str,
                    "fingerprint": fingerprint,
                    "note": "IntegrityError — concurrent worker created incident first",
                }

    finally:
        await release_dedup_lock(redis=redis, fingerprint=fingerprint)

    # ── Return success result (includes patch fields for PR dispatch) ──────────
    new_incident_id: str = str(persistence_result["incident_id"])
    new_analysis_id: str = str(persistence_result["analysis_id"])

    logger.info(
        "run_agent_completed",
        extra={
            "tenant_id": tenant_id_str,
            "incident_id": incident_id_str,
            "new_incident_id": new_incident_id,
            "new_analysis_id": new_analysis_id,
            "fingerprint_prefix": fingerprint[:16],
            "action": action,
            "is_draft": is_draft,
            "confidence_score": confidence_score,
            "total_latency_ms": total_latency_ms,
            "task_id": task_id,
        },
    )

    return {
        "action": action,
        "tenant_id": tenant_id_str,
        "incident_id": incident_id_str,
        "new_incident_id": new_incident_id,
        "new_analysis_id": new_analysis_id,
        "fingerprint": fingerprint,
        "is_draft": is_draft,
        "confidence_score": confidence_score,
        "total_latency_ms": total_latency_ms,
        # Fields required by the synchronous PR dispatch block below
        "structured_patch": agent_result.get("structured_patch") or "",
        "root_cause": agent_result.get("root_cause") or "",
        "suggested_fix": agent_result.get("suggested_fix") or "",
    }


# ---------------------------------------------------------------------------
# Celery task entry point
# ---------------------------------------------------------------------------


@celery_app.task(
    name="app.worker.tasks.run_agent.run_agent",
    bind=True,
    acks_late=True,
    reject_on_worker_lost=True,
    autoretry_for=(
        OSError,
        ConnectionError,
        TimeoutError,
        aioredis.ConnectionError,
        sqlalchemy.exc.OperationalError,
        sqlalchemy.exc.DisconnectionError,
    ),
    max_retries=5,
    default_retry_delay=10,
    soft_time_limit=_AGENT_SOFT_TIME_LIMIT,
    time_limit=_AGENT_HARD_TIME_LIMIT,
)
def run_agent(
    self,
    *,
    parsed_event: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Phase 4 Final — run_agent Celery task with full LangGraph workflow
    including patch_generator node and post-execution PR dispatch.
    """
    task_id: str = self.request.id or str(_uuid_module.uuid4())
    tenant_id: str = parsed_event.get("tenant_id", "unknown")
    incident_id: str = parsed_event.get("incident_id", "unknown")

    logger.info(
        "run_agent_task_received",
        extra={
            "tenant_id": tenant_id,
            "incident_id": incident_id,
            "error_type": parsed_event.get("error_type", "unknown"),
            "task_id": task_id,
            "attempt": self.request.retries + 1,
            "max_retries": self.max_retries,
        },
    )

    try:
        result: Dict[str, Any] = asyncio.run(
            _execute_run_agent(
                task_id=task_id,
                parsed_event_dict=parsed_event,
            )
        )
    except ValueError as exc:
        logger.error(
            "run_agent_validation_error",
            extra={
                "tenant_id": tenant_id,
                "incident_id": incident_id,
                "error": str(exc),
                "task_id": task_id,
            },
        )
        raise
    except Exception as exc:
        logger.error(
            "run_agent_error",
            extra={
                "tenant_id": tenant_id,
                "incident_id": incident_id,
                "error_type": type(exc).__name__,
                "error": str(exc),
                "task_id": task_id,
                "attempt": self.request.retries + 1,
            },
            exc_info=True,
        )
        raise

    # ── Dispatch GitHub PR task (synchronous, after asyncio.run returns) ──────
    # Conditions:
    #   1. Agent decided to create a real incident (not a draft).
    #   2. PatchGeneratorNode produced at least one validated patch.
    #   3. The incident was actually persisted (new_incident_id is present).
    if (
        result.get("action") == "create_incident"
        and result.get("structured_patch")
        and not result.get("is_draft")
        and result.get("new_incident_id")
    ):
        try:
            from app.worker.tasks.github_pr import create_github_pr

            create_github_pr.delay(
                tenant_id=result["tenant_id"],
                incident_id=result["new_incident_id"],
                structured_patch=result["structured_patch"],
                error_type=parsed_event.get("error_type", ""),
                root_cause=result.get("root_cause", ""),
                suggested_fix=result.get("suggested_fix", ""),
            )
            logger.info(
                "github_pr_task_dispatched",
                extra={
                    "tenant_id": result["tenant_id"],
                    "incident_id": result["new_incident_id"],
                    "task_id": task_id,
                },
            )
        except Exception as exc:
            # A Celery broker failure must never block run_agent from returning.
            logger.warning(
                "github_pr_dispatch_failed",
                extra={"error": str(exc), "task_id": task_id},
            )

    logger.info(
        "run_agent_task_complete",
        extra={
            "tenant_id": tenant_id,
            "incident_id": incident_id,
            "action": result.get("action"),
            "task_id": task_id,
        },
    )

    return result