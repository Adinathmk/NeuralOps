"""
fastapi/app/api/v1/webhooks.py

GitHub push webhook receiver.

``POST /api/v1/webhooks/github``

Responsibilities
----------------
1. Read the raw request body BEFORE Pydantic parses it so the exact bytes
   can be used for HMAC-SHA256 signature verification.
2. Look up the tenant that owns the repository referenced in the push event
   by matching ``payload.repository.clone_url`` against
   ``tenant_snapshots.github_repo_url``.
3. Verify the ``X-Hub-Signature-256`` header using the tenant's decrypted
   webhook secret.
4. Dispatch an ``index_code`` Celery task with ``is_initial=False`` and
   the lists of changed / removed files extracted from the push payload.
5. Return ``202 Accepted`` immediately — no synchronous indexing happens
   in the HTTP request path.

Security model
--------------
* The webhook uses a single global secret configured in the environment as
  ``GITHUB_WEBHOOK_SECRET``. All webhook events from the GitHub App use
  this secret for HMAC verification.
* If the signature header is absent, malformed, or doesn't match, the
  endpoint returns ``401 Unauthorized`` — the Celery task is never queued.
* Tenant lookup is done by repository URL.  If no tenant is found the
  endpoint returns ``404`` to avoid leaking information.

Architecture reference: NeuralOps Technical Documentation — Section 17
(Code Indexing — Incremental Update / push webhook).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.logging import get_logger
from app.database.session import get_db
from app.models.snapshots import TenantSnapshot

logger = get_logger(__name__)
router = APIRouter(tags=["webhooks"])


# ---------------------------------------------------------------------------
# HMAC verification
# ---------------------------------------------------------------------------


def _verify_signature(
    raw_body: bytes,
    plain_secret: str,
    signature_header: Optional[str],
) -> None:
    """
    Verify the ``X-Hub-Signature-256: sha256=<hex>`` header produced by GitHub.

    Raises:
        HTTPException(401) — if the header is absent, malformed, or the
                             HMAC comparison fails.
    """
    if not signature_header:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-Hub-Signature-256 header.",
        )

    if not signature_header.startswith("sha256="):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Malformed X-Hub-Signature-256 header (expected 'sha256=...').",
        )

    received_hex = signature_header[len("sha256=") :]

    expected = hmac.new(
        plain_secret.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected, received_hex):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Webhook signature verification failed.",
        )


# ---------------------------------------------------------------------------
# Tenant lookup
# ---------------------------------------------------------------------------


async def _find_tenant_and_integration_by_repo_url(
    repo_clone_url: str,
    db: AsyncSession,
) -> tuple[Optional[TenantSnapshot], Optional[Any]]:
    """
    Return the (TenantSnapshot, GitHubIntegrationSnapshot) whose ``repo_url`` matches
    *repo_clone_url*, or (None, None) if not found.

    GitHub sends both HTTPS and SSH variants; we normalise both to bare
    HTTPS by stripping trailing ``.git`` before comparing.
    """
    from app.models.github_integration_snapshots import GitHubIntegrationSnapshot
    from sqlalchemy.orm import selectinload

    normalised = repo_clone_url.rstrip("/").removesuffix(".git")

    result = await db.execute(
        select(GitHubIntegrationSnapshot).options(selectinload(GitHubIntegrationSnapshot.tenant))
    )
    integrations = result.scalars().all()

    for integration in integrations:
        stored = (integration.repo_url or "").rstrip("/").removesuffix(".git")
        if stored == normalised:
            return integration.tenant, integration

    return None, None


# ---------------------------------------------------------------------------
# File-list helpers
# ---------------------------------------------------------------------------


def _collect_changed_files(commits: List[Dict[str, Any]]) -> List[str]:
    """
    Aggregate ``added`` and ``modified`` file paths across all commits in
    the push payload, deduplicating while preserving order.
    """
    seen: set = set()
    paths: List[str] = []
    for commit in commits:
        for path in commit.get("added", []) + commit.get("modified", []):
            if path not in seen:
                seen.add(path)
                paths.append(path)
    return paths


def _collect_removed_files(commits: List[Dict[str, Any]]) -> List[str]:
    """
    Aggregate ``removed`` file paths across all commits, deduplicating.
    """
    seen: set = set()
    paths: List[str] = []
    for commit in commits:
        for path in commit.get("removed", []):
            if path not in seen:
                seen.add(path)
                paths.append(path)
    return paths


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


@router.post(
    "/webhooks/github",
    status_code=status.HTTP_202_ACCEPTED,
    summary="GitHub push webhook receiver",
    description=(
        "Receives GitHub push events, verifies the HMAC-SHA256 signature, "
        "looks up the owning tenant, and dispatches an incremental "
        "``index_code`` Celery task for changed files."
    ),
    responses={
        202: {"description": "Webhook accepted; indexing task queued."},
        401: {"description": "Signature verification failed."},
        404: {"description": "No tenant found for this repository."},
        422: {"description": "Payload is not a push event or is malformed."},
    },
)
async def receive_github_webhook(
    request: Request,
    x_hub_signature_256: Optional[str] = Header(
        default=None, alias="x-hub-signature-256"
    ),
    x_github_event: Optional[str] = Header(default=None, alias="x-github-event"),
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    """
    ``POST /api/v1/webhooks/github``

    Steps
    -----
    1. Read raw body bytes (required for exact HMAC computation).
    2. Parse JSON payload.
    3. Ignore non-push events (ping, create, etc.) — return 202 immediately.
    4. Locate tenant by repository clone URL.
    5. Verify HMAC signature using the global webhook secret.
    6. Extract head commit SHA + changed / removed file lists.
    7. Dispatch ``index_code`` Celery task.
    8. Return 202 Accepted.
    """
    # ── Step 1: Read raw body ─────────────────────────────────────────────────
    raw_body: bytes = await request.body()

    # ── Step 2: Parse JSON ────────────────────────────────────────────────────
    import json as _json

    try:
        payload: Dict[str, Any] = _json.loads(raw_body)
    except _json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid JSON payload: {exc}",
        ) from exc

    # ── Step 3: Ignore non-push events ────────────────────────────────────────
    event_type = x_github_event or ""
    if event_type == "ping":
        logger.info("github_webhook_ping_received")
        return {"status": "pong", "message": "Webhook registered successfully."}

    if event_type != "push" and event_type != "":
        # GitHub sends X-GitHub-Event; if missing we still try to process.
        logger.info("github_webhook_non_push_event", extra={"event": event_type})
        return {"status": "ignored", "message": f"Event '{event_type}' not handled."}

    # ── Step 4: Locate tenant by repository URL ───────────────────────────────
    repo_info: Dict[str, Any] = payload.get("repository", {})
    # GitHub provides both html_url (https://github.com/org/repo) and
    # clone_url (https://github.com/org/repo.git).  We prefer clone_url.
    clone_url: str = repo_info.get("clone_url") or repo_info.get("html_url") or ""

    if not clone_url:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Push payload is missing repository.clone_url.",
        )

    tenant, integration = await _find_tenant_and_integration_by_repo_url(clone_url, db)
    if tenant is None or integration is None:
        logger.warning(
            "github_webhook_tenant_not_found",
            extra={"clone_url": clone_url},
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No tenant found for repository: {clone_url}",
        )

    # ── Step 5: Verify HMAC signature ─────────────────────────────────────────
    settings = get_settings()
    if not settings.GITHUB_WEBHOOK_SECRET:
        logger.error("github_webhook_no_global_secret")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Global webhook secret is not configured.",
        )

    _verify_signature(raw_body, settings.GITHUB_WEBHOOK_SECRET, x_hub_signature_256)

    # ── Step 6: Extract commit SHA and file lists ─────────────────────────────
    # GitHub push payload: ``head_commit`` is the most recent commit.
    head_commit: Dict[str, Any] = payload.get("head_commit") or {}
    commit_sha: str = head_commit.get("id") or payload.get("after", "")

    if not commit_sha:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Push payload is missing head_commit.id / after field.",
        )

    commits: List[Dict[str, Any]] = payload.get("commits", [])
    changed_files: List[str] = _collect_changed_files(commits)
    removed_files: List[str] = _collect_removed_files(commits)

    # Filter to supported file types only — no point queuing tasks for
    # markdown, JSON, or other assets.
    SUPPORTED_EXTS = (".py", ".java")
    changed_files = [f for f in changed_files if f.endswith(SUPPORTED_EXTS)]
    removed_files = [f for f in removed_files if f.endswith(SUPPORTED_EXTS)]

    logger.info(
        "github_webhook_push_received",
        extra={
            "tenant_id": str(tenant.tenant_id),
            "commit_sha": commit_sha,
            "changed_count": len(changed_files),
            "removed_count": len(removed_files),
        },
    )

    # ── Step 7: Dispatch Celery task ──────────────────────────────────────────
    from app.worker.tasks.index_code import (  # local import avoids circular
        index_code,
    )

    index_code.delay(
        tenant_id=str(tenant.tenant_id),
        repo_url=integration.repo_url,
        commit_sha=commit_sha,
        changed_files=changed_files,
        removed_files=removed_files,
        is_initial=False,
    )
    logger.info(
        "github_webhook_index_task_queued",
        extra={
            "tenant_id": str(tenant.tenant_id),
            "commit_sha": commit_sha,
            "changed_files": changed_files,
            "removed_files": removed_files,
        },
    )

    # ── Step 8: Return 202 ────────────────────────────────────────────────────
    return {
        "status": "accepted",
        "message": "Push event received. Indexing task queued.",
        "commit_sha": commit_sha,
        "changed_files": len(changed_files),
        "removed_files": len(removed_files),
    }
