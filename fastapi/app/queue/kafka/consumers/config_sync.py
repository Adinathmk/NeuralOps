"""
fastapi/app/queue/kafka/consumers/config_sync.py

Kafka consumer for configuration snapshot synchronisation.

Subscribes to:
  - config.tenants      → upserts tenant_snapshots (including GitHub columns)
  - config.alert_rules  → upserts alert_rule_snapshots
  - config.playbooks    → upserts playbook_snapshots

Phase 3 additions
-----------------
_handle_tenant_event() now reads an optional nested `github_integration`
block from the Kafka payload and maps it to the github_* columns on the
TenantSnapshot row.  All other behaviour (staleness check, Redis L1 cache
invalidation, idempotency) is unchanged.

Kafka message payload shape for a tenant.updated event with GitHub data:

{
  "event_type": "tenant.updated",
  "tenant": {
    "id": "<uuid>",
    "plan_tier": "pro",
    "is_suspended": false,
    "source_version": 42,
    "github_integration": {
      "repo_url": "https://github.com/my-org/my-repo",
      "repo_owner": "my-org",
      "repo_name": "my-repo",
      "installation_id": 123456,
      "default_branch": "main",
      "indexing_status": "pending",
      "last_indexed_commit": null
    }
  }
}

Architecture reference: NeuralOps Technical Documentation — Sections 5, 6, 7, 17
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any, Dict, Optional

import httpx
import redis.asyncio as aioredis
from aiokafka import AIOKafkaConsumer
from aiokafka.errors import KafkaError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.database.session import AsyncSessionLocal
from app.models.snapshots import AlertRuleSnapshot, PlaybookSnapshot, TenantSnapshot, APIKeySnapshot
from app.worker.tasks.wipe_data import wipe_tenant_data

logger = logging.getLogger(__name__)

# ── Kafka topic names ─────────────────────────────────────────────────────────
TOPIC_TENANTS = "config.tenants"
TOPIC_ALERT_RULES = "config.alert_rules"
TOPIC_PLAYBOOKS = "config.playbooks"
TOPIC_API_KEYS = "config.api_keys"

CONFIG_TOPICS = (TOPIC_TENANTS, TOPIC_ALERT_RULES, TOPIC_PLAYBOOKS, TOPIC_API_KEYS)


# ── GitHub field mapping ───────────────────────────────────────────────────────
# Maps incoming Kafka payload keys → TenantSnapshot column names.
# All values are nullable; missing keys in the payload leave the column unchanged.
_GITHUB_FIELD_MAP: Dict[str, str] = {
    "repo_url": "github_repo_url",
    "repo_owner": "github_repo_owner",
    "repo_name": "github_repo_name",
    "installation_id": "github_installation_id",
    "default_branch": "github_default_branch",
    "indexing_status": "github_indexing_status",
    "last_indexed_commit": "github_last_indexed_commit",
}


def _apply_github_fields(
    snapshot: TenantSnapshot,
    github_data: Dict[str, Any],
) -> None:
    """
    Apply the nested `github_integration` block from the Kafka payload to the
    TenantSnapshot ORM instance.

    Only keys present in `github_data` are written — missing keys in the
    payload are treated as "no change" (they preserve the existing column
    value).  Explicit null values in the payload WILL overwrite the column
    (allowing a disconnect flow to clear the columns).

    Args:
        snapshot:    The TenantSnapshot instance to mutate (in-session).
        github_data: Dict from payload["tenant"]["github_integration"].
    """
    for payload_key, column_name in _GITHUB_FIELD_MAP.items():
        if payload_key in github_data:
            val = github_data[payload_key]
            if payload_key == "installation_id" and val is not None:
                val = int(val)
            setattr(snapshot, column_name, val)


# ── Initial-index dispatch helpers ────────────────────────────────────────────


async def _resolve_branch_sha(
    owner: str,
    repo: str,
    branch: str,
    installation_id: int,
) -> Optional[str]:
    """
    Resolve the latest commit SHA for *branch* via the GitHub Branches API.

    Returns ``None`` on any failure (network error, bad credentials, etc.).
    The caller is responsible for deciding whether to abort or retry.
    """
    from app.services.github_auth import get_installation_token

    try:
        token = await get_installation_token(installation_id)
    except Exception as exc:
        logger.error(
            "config_sync_token_fetch_failed",
            extra={"error": str(exc)},
        )
        return None

    url = f"https://api.github.com/repos/{owner}/{repo}/branches/{branch}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "NeuralOps-ConfigSync/1.0",
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, headers=headers)
        if resp.status_code != 200:
            logger.error(
                "config_sync_branch_sha_fetch_failed",
                extra={
                    "status": resp.status_code,
                    "repo": f"{owner}/{repo}",
                    "branch": branch,
                    "response_preview": resp.text[:200],
                },
            )
            return None
        sha: str = resp.json()["commit"]["sha"]
        logger.debug(
            "config_sync_branch_sha_resolved",
            extra={"repo": f"{owner}/{repo}", "branch": branch, "sha": sha},
        )
        return sha
    except Exception as exc:
        logger.error(
            "config_sync_branch_sha_error",
            extra={"repo": f"{owner}/{repo}", "branch": branch, "error": str(exc)},
        )
        return None


async def _maybe_dispatch_initial_index(
    tenant_id: str,
    github_data: Dict[str, Any],
    previous_status: Optional[str],
) -> None:
    """
    Fire ``index_code(is_initial=True)`` if and only if this Kafka event
    represents a *fresh* GitHub connection.

    Conditions that must ALL be true before dispatching:
      1. The incoming payload's ``indexing_status`` is ``"pending"``.
      2. The row's status BEFORE this event was NOT ``"indexing"`` or
         ``"indexed"`` — prevents re-triggering on Kafka replays after
         a service restart.
      3. All required GitHub fields are present in the payload.
      4. The GitHub Branches API successfully returns a commit SHA.

    If conditions 3 or 4 fail the row stays at ``"pending"`` in the DB,
    which makes the gap observable (ops can re-connect the repo to retry).

    Args:
        tenant_id:       String UUID of the tenant.
        github_data:     The ``github_integration`` block from the Kafka payload.
        previous_status: ``github_indexing_status`` value that was in the DB
                         *before* this event was applied (``None`` for a brand-
                         new row).
    """
    incoming_status: Optional[str] = github_data.get("indexing_status")

    # ── Guard 1: only act on a fresh connection ────────────────────────────────
    if incoming_status != "pending":
        return

    # ── Guard 2: idempotency — skip if already processed ──────────────────────
    if previous_status in ("indexing", "indexed"):
        logger.info(
            "config_sync_initial_index_skip_already_processed",
            extra={
                "tenant_id": tenant_id,
                "previous_status": previous_status,
            },
        )
        return

    owner: Optional[str] = github_data.get("repo_owner")
    repo: Optional[str] = github_data.get("repo_name")
    branch: str = github_data.get("default_branch") or "main"
    installation_id: Optional[int] = github_data.get("installation_id")
    repo_url: Optional[str] = github_data.get("repo_url")

    # ── Guard 3: all required fields must be present ───────────────────────────
    if not all([owner, repo, installation_id, repo_url]):
        logger.error(
            "config_sync_initial_index_missing_fields",
            extra={
                "tenant_id": tenant_id,
                "has_owner": bool(owner),
                "has_repo": bool(repo),
                "has_installation_id": bool(installation_id),
                "has_repo_url": bool(repo_url),
            },
        )
        return

    # ── Guard 4: resolve latest commit SHA from GitHub ─────────────────────────
    commit_sha = await _resolve_branch_sha(owner, repo, branch, installation_id)
    if not commit_sha:
        logger.error(
            "config_sync_initial_index_sha_unavailable",
            extra={
                "tenant_id": tenant_id,
                "repo": f"{owner}/{repo}",
                "branch": branch,
            },
        )
        # Stay 'pending' — the next re-connection event will retry.
        return

    # ── Dispatch the Celery task ───────────────────────────────────────────────
    # Local import to avoid module-level circular dependency.
    from app.worker.tasks.index_code import index_code  # noqa: PLC0415

    index_code.delay(
        tenant_id=tenant_id,
        repo_url=repo_url,
        commit_sha=commit_sha,
        is_initial=True,
    )

    logger.info(
        "config_sync_initial_index_dispatched",
        extra={
            "tenant_id": tenant_id,
            "repo": f"{owner}/{repo}",
            "branch": branch,
            "commit_sha": commit_sha,
        },
    )


# ── ConfigSyncConsumer ────────────────────────────────────────────────────────


class ConfigSyncConsumer:
    """
    Long-running async Kafka consumer that keeps DB-2 snapshot tables in
    sync with Django's authoritative configuration in DB-1.

    Lifecycle
    ---------
    Instantiate once at application startup.
    Call `start()` inside an `asyncio.create_task()` so it runs as a
    background task without blocking the FastAPI event loop.
    Call `stop()` during graceful shutdown to drain in-flight messages and
    close the Kafka consumer cleanly.

    Usage in main.py lifespan:
        consumer = ConfigSyncConsumer()
        asyncio.create_task(consumer.start())
        ...
        await consumer.stop()
    """

    def __init__(self) -> None:
        self._settings = get_settings()
        self._consumer: Optional[AIOKafkaConsumer] = None
        self._redis: Optional[aioredis.Redis] = None
        self._running: bool = False

    # ── Public lifecycle methods ──────────────────────────────────────────────

    async def start(self) -> None:
        """
        Initialise the Kafka consumer and Redis client, then enter the
        message processing loop.

        This method is designed to be run as a background asyncio task.
        It handles its own exceptions so a transient Kafka or DB error
        will not crash the FastAPI process — it logs the error, waits
        briefly, then retries.
        """
        logger.info(
            "config_sync_consumer_starting",
            extra={
                "bootstrap_servers": self._settings.KAFKA_BOOTSTRAP_SERVERS,
                "group_id": self._settings.KAFKA_CONFIG_GROUP_ID,
                "topics": CONFIG_TOPICS,
            },
        )

        self._redis = aioredis.from_url(
            self._settings.REDIS_URL,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=5,
        )

        self._consumer = AIOKafkaConsumer(
            *CONFIG_TOPICS,
            bootstrap_servers=self._settings.KAFKA_BOOTSTRAP_SERVERS,
            group_id=self._settings.KAFKA_CONFIG_GROUP_ID,
            # Replay the full compacted topic from the beginning when the
            # consumer group has no committed offset (fresh deployment).
            auto_offset_reset="earliest",
            # Disable auto-commit so we control exactly when offsets are
            # committed: only AFTER the DB-2 transaction succeeds.
            enable_auto_commit=False,
            value_deserializer=lambda raw: raw.decode("utf-8") if raw else None,
            key_deserializer=lambda raw: raw.decode("utf-8") if raw else None,
        )

        self._running = True

        # Outer retry loop: if the consumer crashes (e.g. Kafka broker
        # unavailable at startup), wait and retry rather than dying.
        while self._running:
            try:
                if self._consumer is None:
                    self._consumer = AIOKafkaConsumer(
                        *CONFIG_TOPICS,
                        bootstrap_servers=self._settings.KAFKA_BOOTSTRAP_SERVERS,
                        group_id=self._settings.KAFKA_CONFIG_GROUP_ID,
                        # Replay the full compacted topic from the beginning when the
                        # consumer group has no committed offset (fresh deployment).
                        auto_offset_reset="earliest",
                        # Disable auto-commit so we control exactly when offsets are
                        # committed: only AFTER the DB-2 transaction succeeds.
                        enable_auto_commit=False,
                        value_deserializer=lambda raw: (
                            raw.decode("utf-8") if raw else None
                        ),
                        key_deserializer=lambda raw: (
                            raw.decode("utf-8") if raw else None
                        ),
                    )
                await self._consumer.start()
                logger.info(
                    "config_sync_consumer_started",
                    extra={"topics": CONFIG_TOPICS},
                )
                await self._consume_loop()
            except KafkaError as exc:
                logger.error(
                    "config_sync_kafka_error",
                    extra={"error": str(exc)},
                    exc_info=True,
                )
            except asyncio.CancelledError:
                logger.info("config_sync_consumer_cancelled")
                break
            except Exception as exc:
                logger.error(
                    "config_sync_unexpected_error",
                    extra={"error": str(exc)},
                    exc_info=True,
                )
            finally:
                await self._safe_stop_consumer()

            if self._running:
                logger.info(
                    "config_sync_consumer_retrying",
                    extra={"retry_delay_seconds": 5},
                )
                await asyncio.sleep(5)

        logger.info("config_sync_consumer_stopped")

    async def stop(self) -> None:
        """Signal the consumer loop to exit and wait for a clean shutdown."""
        logger.info("config_sync_consumer_stopping")
        self._running = False
        await self._safe_stop_consumer()
        if self._redis:
            await self._redis.aclose()
            self._redis = None

    # ── Internal: core consume loop ───────────────────────────────────────────

    async def _consume_loop(self) -> None:
        """
        Core message processing loop.

        For each message:
          1. Parse the JSON payload.
          2. Route to the appropriate handler based on topic.
          3. Commit the Kafka offset only after DB-2 is updated.
        """
        async for message in self._consumer:
            if not self._running:
                break

            topic = message.topic
            raw_value = message.value

            logger.debug(
                "config_sync_message_received",
                extra={
                    "topic": topic,
                    "partition": message.partition,
                    "offset": message.offset,
                    "key": message.key,
                },
            )

            # ── Parse JSON ────────────────────────────────────────────────────
            try:
                payload: Dict[str, Any] = json.loads(raw_value)
            except (json.JSONDecodeError, TypeError) as exc:
                logger.error(
                    "config_sync_json_decode_error",
                    extra={
                        "topic": topic,
                        "offset": message.offset,
                        "raw_value": raw_value[:200] if raw_value else None,
                        "error": str(exc),
                    },
                )
                # Commit and skip — permanently malformed message.
                await self._consumer.commit()
                continue

            # ── Route to handler ──────────────────────────────────────────────
            try:
                if topic == TOPIC_TENANTS:
                    await self._handle_tenant_event(payload)
                elif topic == TOPIC_ALERT_RULES:
                    await self._handle_alert_rule_event(payload)
                elif topic == TOPIC_PLAYBOOKS:
                    await self._handle_playbook_event(payload)
                elif topic == TOPIC_API_KEYS:
                    await self._handle_api_keys_event(payload)
                else:
                    logger.warning(
                        "config_sync_unknown_topic",
                        extra={"topic": topic},
                    )
            except KeyError as exc:
                logger.error(
                    "config_sync_missing_field",
                    extra={
                        "topic": topic,
                        "offset": message.offset,
                        "missing_key": str(exc),
                        "payload_keys": list(payload.keys()),
                    },
                )
                # Do NOT commit — allow retry on restart.
                continue
            except Exception as exc:
                logger.error(
                    "config_sync_handler_error",
                    extra={
                        "topic": topic,
                        "offset": message.offset,
                        "error": str(exc),
                    },
                    exc_info=True,
                )
                continue

            # ── Commit offset (only after successful DB write) ─────────────────
            await self._consumer.commit()
            logger.debug(
                "config_sync_offset_committed",
                extra={
                    "topic": topic,
                    "partition": message.partition,
                    "offset": message.offset,
                },
            )

    # ── Internal: per-topic handlers ──────────────────────────────────────────

    async def _handle_tenant_event(self, payload: Dict[str, Any]) -> None:
        """
        Process a config.tenants event.

        Core tenant fields and the optional nested github_integration block
        are both handled here.

        Expected payload shape:
        {
            "event_type": "tenant.updated" | "tenant.created" | ...,
            "tenant": {
                "id": "<uuid>",
                "plan_tier": "free" | "pro" | "enterprise",
                "vector_namespace": "<string>",
                "kafka_group_id": "<string>",
                "is_suspended": false,
                "source_version": <int>,
                "github_integration": {        ← OPTIONAL (Phase 3)
                    "repo_url": "...",
                    "repo_owner": "...",
                    "repo_name": "...",
                    "installation_id": 123456,
                    "default_branch": "main",
                    "indexing_status": "pending",
                    "last_indexed_commit": null
                }
            }
        }
        """
        tenant_data: Dict[str, Any] = payload["tenant"]

        tenant_id = uuid.UUID(str(tenant_data["id"]))
        incoming_version: int = int(tenant_data["source_version"])

        # Optional GitHub block — may be absent on non-integration events.
        github_data: Optional[Dict[str, Any]] = tenant_data.get("github_integration")

        # Capture the PREVIOUS indexing status before we overwrite it.
        # Used by _maybe_dispatch_initial_index() to enforce idempotency:
        # if the row already says 'indexing' or 'indexed' we must not
        # re-dispatch the task on a Kafka replay after a service restart.
        previous_indexing_status: Optional[str] = None

        async with AsyncSessionLocal() as session:
            async with session.begin():
                existing = await self._get_tenant_snapshot(session, tenant_id)

                # ── Staleness check ───────────────────────────────────────────
                if existing is not None:
                    if (
                        existing.source_version is not None
                        and existing.source_version >= incoming_version
                    ):
                        logger.warning(
                            "config_sync_stale_tenant_event",
                            extra={
                                "tenant_id": str(tenant_id),
                                "existing_version": existing.source_version,
                                "incoming_version": incoming_version,
                            },
                        )
                        return  # Discard stale event

                    # Snapshot the current indexing status before overwriting.
                    previous_indexing_status = existing.github_indexing_status

                    # ── Update core tenant fields ─────────────────────────────
                    existing.plan_tier = tenant_data.get(
                        "plan_tier", existing.plan_tier
                    )
                    existing.vector_namespace = tenant_data.get(
                        "vector_namespace", existing.vector_namespace
                    )
                    existing.kafka_group_id = tenant_data.get(
                        "kafka_group_id", existing.kafka_group_id
                    )
                    existing.is_suspended = bool(
                        tenant_data.get("is_suspended", existing.is_suspended)
                    )
                    existing.source_version = incoming_version

                    # ── Apply GitHub integration fields (Phase 3) ─────────────
                    if "github_integration" in tenant_data:
                        if github_data is None:
                            # Explicit null means the integration was deleted
                            _apply_github_fields(
                                existing,
                                {
                                    "repo_url": None,
                                    "repo_owner": None,
                                    "repo_name": None,
                                    "installation_id": None,
                                    "default_branch": None,
                                    "indexing_status": None,
                                    "last_indexed_commit": None,
                                },
                            )
                            # Wipe all tenant logs, incidents, and AST data
                            logger.info(
                                "config_sync_triggering_tenant_wipe",
                                extra={"tenant_id": str(tenant_id)},
                            )
                            wipe_tenant_data.delay(str(tenant_id))
                            logger.info(
                                "config_sync_tenant_github_cleared",
                                extra={"tenant_id": str(tenant_id)},
                            )
                            # Dispatch cleanup task
                            from app.worker.tasks.index_code import cleanup_code_index

                            cleanup_code_index.delay(tenant_id=str(tenant_id))
                        else:
                            _apply_github_fields(existing, github_data)
                            logger.info(
                                "config_sync_tenant_github_updated",
                                extra={
                                    "tenant_id": str(tenant_id),
                                    "repo": f"{github_data.get('repo_owner')}/{github_data.get('repo_name')}",
                                    "indexing_status": github_data.get(
                                        "indexing_status"
                                    ),
                                },
                            )

                    session.add(existing)

                    logger.info(
                        "config_sync_tenant_updated",
                        extra={
                            "tenant_id": str(tenant_id),
                            "source_version": incoming_version,
                            "has_github_data": github_data is not None,
                        },
                    )

                else:
                    # New row — previous status is implicitly None.
                    # previous_indexing_status stays None (already initialised above).

                    # ── Create new snapshot ───────────────────────────────────
                    snapshot = TenantSnapshot(
                        tenant_id=tenant_id,
                        plan_tier=tenant_data.get("plan_tier"),
                        vector_namespace=tenant_data.get("vector_namespace"),
                        kafka_group_id=tenant_data.get("kafka_group_id"),
                        is_suspended=bool(tenant_data.get("is_suspended", False)),
                        source_version=incoming_version,
                    )

                    # Apply GitHub integration fields if present.
                    if github_data is not None:
                        _apply_github_fields(snapshot, github_data)
                        logger.info(
                            "config_sync_tenant_github_set_on_create",
                            extra={
                                "tenant_id": str(tenant_id),
                                "repo": f"{github_data.get('repo_owner')}/{github_data.get('repo_name')}",
                            },
                        )

                    session.add(snapshot)

                    logger.info(
                        "config_sync_tenant_created",
                        extra={
                            "tenant_id": str(tenant_id),
                            "source_version": incoming_version,
                            "has_github_data": github_data is not None,
                        },
                    )

        # ── Redis L1 cache invalidation (outside the DB transaction) ──────────
        await self._invalidate_tenant_cache(str(tenant_id))

        # ── Dispatch initial indexing task on first GitHub connection ──────────
        # Runs AFTER the DB transaction has committed so the worker can read
        # the fully-written TenantSnapshot without hitting a race condition.
        if github_data is not None:
            await _maybe_dispatch_initial_index(
                tenant_id=str(tenant_id),
                github_data=github_data,
                previous_status=previous_indexing_status,
            )

    async def _handle_alert_rule_event(self, payload: Dict[str, Any]) -> None:
        """
        Process a config.alert_rules event.

        Expected payload shape:
        {
            "event_type": "alert_rule.created" | "alert_rule.updated" | "alert_rule.deleted",
            "alert_rule": {
                "id": "<uuid>",
                "tenant_id": "<uuid>",
                "confidence_threshold": "0.85",
                "severity_filter": ["critical", "high"],
                "recipient_ids": ["<uuid>", ...],
                "enabled": true,
                "source_version": <int>,
                "deleted": false
            }
        }
        """
        rule_data: Dict[str, Any] = payload["alert_rule"]

        rule_id = uuid.UUID(str(rule_data["id"]))
        tenant_id = uuid.UUID(str(rule_data["tenant_id"]))
        incoming_version: int = int(rule_data["source_version"])
        is_deleted: bool = bool(rule_data.get("deleted", False))

        async with AsyncSessionLocal() as session:
            async with session.begin():
                existing = await self._get_alert_rule_snapshot(session, rule_id)

                # ── Staleness check ───────────────────────────────────────────
                if existing is not None:
                    if (
                        existing.source_version is not None
                        and existing.source_version >= incoming_version
                    ):
                        logger.warning(
                            "config_sync_stale_alert_rule_event",
                            extra={
                                "rule_id": str(rule_id),
                                "existing_version": existing.source_version,
                                "incoming_version": incoming_version,
                            },
                        )
                        return

                    if is_deleted:
                        await session.delete(existing)
                        logger.info(
                            "config_sync_alert_rule_deleted",
                            extra={
                                "rule_id": str(rule_id),
                                "tenant_id": str(tenant_id),
                            },
                        )
                    else:
                        existing.tenant_id = tenant_id
                        existing.confidence_threshold = rule_data.get(
                            "confidence_threshold", existing.confidence_threshold
                        )
                        existing.severity_filter = rule_data.get(
                            "severity_filter", existing.severity_filter
                        )
                        existing.recipient_ids = rule_data.get(
                            "recipient_ids", existing.recipient_ids
                        )
                        existing.enabled = bool(
                            rule_data.get("enabled", existing.enabled)
                        )
                        existing.source_version = incoming_version
                        session.add(existing)

                        logger.info(
                            "config_sync_alert_rule_updated",
                            extra={
                                "rule_id": str(rule_id),
                                "tenant_id": str(tenant_id),
                                "source_version": incoming_version,
                            },
                        )
                else:
                    if is_deleted:
                        logger.debug(
                            "config_sync_alert_rule_delete_noop",
                            extra={"rule_id": str(rule_id)},
                        )
                        return

                    snapshot = AlertRuleSnapshot(
                        rule_id=rule_id,
                        tenant_id=tenant_id,
                        confidence_threshold=rule_data.get("confidence_threshold"),
                        severity_filter=rule_data.get("severity_filter"),
                        recipient_ids=rule_data.get("recipient_ids"),
                        enabled=bool(rule_data.get("enabled", True)),
                        source_version=incoming_version,
                    )
                    session.add(snapshot)

                    logger.info(
                        "config_sync_alert_rule_created",
                        extra={
                            "rule_id": str(rule_id),
                            "tenant_id": str(tenant_id),
                            "source_version": incoming_version,
                        },
                    )

        await self._invalidate_tenant_cache(str(tenant_id))

    async def _handle_playbook_event(self, payload: Dict[str, Any]) -> None:
        """
        Process a config.playbooks event.

        Expected payload shape:
        {
            "event_type": "playbook.created" | "playbook.updated" | "playbook.deleted",
            "playbook": {
                "id": "<uuid>",
                "tenant_id": "<uuid>",
                "error_pattern": "NullPointerException.*service",
                "instructions": "Check the null guard on line ...",
                "source_version": <int>,
                "deleted": false
            }
        }
        """
        playbook_data: Dict[str, Any] = payload["playbook"]

        playbook_id = uuid.UUID(str(playbook_data["id"]))
        tenant_id = uuid.UUID(str(playbook_data["tenant_id"]))
        incoming_version: int = int(playbook_data["source_version"])
        is_deleted: bool = bool(playbook_data.get("deleted", False))

        async with AsyncSessionLocal() as session:
            async with session.begin():
                existing = await self._get_playbook_snapshot(session, playbook_id)

                # ── Staleness check ───────────────────────────────────────────
                if existing is not None:
                    if (
                        existing.source_version is not None
                        and existing.source_version >= incoming_version
                    ):
                        logger.warning(
                            "config_sync_stale_playbook_event",
                            extra={
                                "playbook_id": str(playbook_id),
                                "existing_version": existing.source_version,
                                "incoming_version": incoming_version,
                            },
                        )
                        return

                    if is_deleted:
                        await session.delete(existing)
                        logger.info(
                            "config_sync_playbook_deleted",
                            extra={
                                "playbook_id": str(playbook_id),
                                "tenant_id": str(tenant_id),
                            },
                        )
                    else:
                        existing.tenant_id = tenant_id
                        existing.error_pattern = playbook_data.get(
                            "error_pattern", existing.error_pattern
                        )
                        existing.instructions = playbook_data.get(
                            "instructions", existing.instructions
                        )
                        existing.source_version = incoming_version
                        session.add(existing)

                        logger.info(
                            "config_sync_playbook_updated",
                            extra={
                                "playbook_id": str(playbook_id),
                                "tenant_id": str(tenant_id),
                                "source_version": incoming_version,
                            },
                        )
                else:
                    if is_deleted:
                        logger.debug(
                            "config_sync_playbook_delete_noop",
                            extra={"playbook_id": str(playbook_id)},
                        )
                        return

                    snapshot = PlaybookSnapshot(
                        playbook_id=playbook_id,
                        tenant_id=tenant_id,
                        error_pattern=playbook_data.get("error_pattern"),
                        instructions=playbook_data.get("instructions"),
                        source_version=incoming_version,
                    )
                    session.add(snapshot)

                    logger.info(
                        "config_sync_playbook_created",
                        extra={
                            "playbook_id": str(playbook_id),
                            "tenant_id": str(tenant_id),
                            "source_version": incoming_version,
                        },
                    )

        # Get plan tier to pass to embedding task
        plan_tier = "standard"
        async with AsyncSessionLocal() as session:
            tenant = await self._get_tenant_snapshot(session, tenant_id)
            if tenant:
                plan_tier = tenant.plan_tier

        await self._invalidate_tenant_cache(str(tenant_id))

        # ── Enqueue embedding task ────────────────────────────────────────────
        from app.worker.tasks.embed_playbook import embed_playbook

        embed_playbook.delay(
            playbook_id=str(playbook_id),
            tenant_id=str(tenant_id),
            plan_tier=plan_tier,
            error_pattern=playbook_data.get("error_pattern", ""),
            instructions=playbook_data.get("instructions", ""),
            source_version=incoming_version,
            deleted=is_deleted,
        )

    # ── Internal: DB query helpers ────────────────────────────────────────────

    @staticmethod
    async def _get_tenant_snapshot(
        session: AsyncSession, tenant_id: uuid.UUID
    ) -> Optional[TenantSnapshot]:
        result = await session.execute(
            select(TenantSnapshot).where(TenantSnapshot.tenant_id == tenant_id)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def _get_alert_rule_snapshot(
        session: AsyncSession, rule_id: uuid.UUID
    ) -> Optional[AlertRuleSnapshot]:
        result = await session.execute(
            select(AlertRuleSnapshot).where(AlertRuleSnapshot.rule_id == rule_id)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def _get_playbook_snapshot(
        session: AsyncSession, playbook_id: uuid.UUID
    ) -> Optional[PlaybookSnapshot]:
        result = await session.execute(
            select(PlaybookSnapshot).where(PlaybookSnapshot.playbook_id == playbook_id)
        )
        return result.scalar_one_or_none()

    # ── Internal: Redis cache invalidation ───────────────────────────────────

    async def _invalidate_tenant_cache(self, tenant_id: str) -> None:
        """
        Delete the Redis L1 cache key for the given tenant's aggregated config.

        Key: tenant:{tenant_id}:config  (TTL: 1 hour, set by API layer)

        We DELETE rather than re-populate because the cache aggregation query
        is owned by the API dependency layer.  The next API request will
        trigger a fresh DB-2 read and repopulate the cache.

        Redis errors are caught and logged; a cache invalidation failure
        is non-fatal (DB-2 snapshot is already authoritative).
        """
        if not self._redis:
            return

        key = self._settings.tenant_config_cache_key(tenant_id)
        rl_ingest_key = f"rl:ingest:{tenant_id}"
        rl_agent_key = f"rl:agent:{tenant_id}"

        try:
            await self._redis.delete(key, rl_ingest_key, rl_agent_key)
            logger.debug(
                "config_sync_cache_invalidated",
                extra={
                    "redis_keys": [key, rl_ingest_key, rl_agent_key],
                    "tenant_id": tenant_id,
                },
            )
        except Exception as exc:
            logger.error(
                "config_sync_cache_invalidation_failed",
                extra={
                    "redis_key": key,
                    "tenant_id": tenant_id,
                    "error": str(exc),
                },
            )

    # ── Internal: API Keys Sync ───────────────────────────────────────────────

    async def _handle_api_keys_event(self, payload: Dict[str, Any]) -> None:
        """
        Handle a config.api_keys CDC event from Django (DB-1).
        
        Payload:
        {
          "id": "<uuid>",
          "tenant_id": "<uuid>",
          "key": "nops_live_...",
          "is_active": true
        }
        """
        if "id" not in payload:
            raise KeyError("id")
        
        try:
            key_id = uuid.UUID(payload["id"])
            tenant_id = uuid.UUID(payload["tenant_id"])
        except (ValueError, KeyError) as exc:
            logger.error(
                "config_sync_api_key_invalid_uuid",
                extra={"payload": payload, "error": str(exc)},
            )
            return

        api_key_str = payload.get("key", "")
        is_active = payload.get("is_active", True)
        
        from sqlalchemy.dialects.postgresql import insert

        async with AsyncSessionLocal() as db:
            stmt = insert(APIKeySnapshot).values(
                id=key_id,
                tenant_id=tenant_id,
                key=api_key_str,
                is_active=is_active,
            )
            
            # Upsert on conflict
            stmt = stmt.on_conflict_do_update(
                index_elements=[APIKeySnapshot.id],
                set_={
                    "is_active": stmt.excluded.is_active,
                }
            )
            
            try:
                await db.execute(stmt)
                await db.commit()
                
                logger.info(
                    "config_sync_api_key_upserted",
                    extra={
                        "api_key_id": str(key_id),
                        "tenant_id": str(tenant_id),
                        "is_active": is_active,
                    },
                )
                
                # Manage Redis cache for immediate consistency
                if self._redis:
                    cache_key = f"apikey_to_tenant:{api_key_str}"
                    if is_active:
                        # Pre-warm the cache immediately upon creation!
                        await self._redis.setex(cache_key, 3600, str(tenant_id))
                    else:
                        # Purge the cache on revocation
                        await self._redis.delete(cache_key)
                    
            except Exception as exc:
                await db.rollback()
                logger.error(
                    "config_sync_api_key_db_error",
                    extra={"api_key_id": str(key_id), "error": str(exc)},
                )
                raise


    # ── Internal: safe consumer stop ─────────────────────────────────────────

    async def _safe_stop_consumer(self) -> None:
        """Stop the AIOKafkaConsumer, suppressing errors during shutdown."""
        if self._consumer:
            try:
                await self._consumer.stop()
            except Exception as exc:
                logger.warning(
                    "config_sync_consumer_stop_error",
                    extra={"error": str(exc)},
                )
            finally:
                self._consumer = None
