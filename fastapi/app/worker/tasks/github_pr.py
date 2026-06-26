"""
fastapi/app/worker/tasks/github_pr.py

Celery task: create_github_pr

After the LangGraph pipeline produces a structured patch for a new incident,
this task turns that patch into a real GitHub Pull Request via the GitHub
Tree + Commits API.

Flow
----
  1. Load TenantSnapshot from DB-2 to get GitHub repo metadata.
  2. If no GitHub installation configured: save pr_status="skipped", return.
  3. Fetch short-lived GitHub App installation token.
  4. Parse structured_patch JSON → list of {file, search, replace}.
  5. For each patch:
       a) Fetch current file content from GitHub API.
       b) Apply replace (str.replace with count=1).
       c) For .py files: validate syntax via py_compile on a temp file.
          Syntax error → abort ALL patches, save pr_status="syntax_error".
  6. Zero patches applied → save pr_status="no_patch", return.
  7. Create branch: neuralops-fix/{incident_id[:8]}
  8. Commit all changed files via GitHub Tree API.
  9. Create PR with structured markdown body.
 10. Save pr_url, pr_number, pr_status="open" to incidents table.

All GitHub calls follow the _github_headers() pattern from index_code.py.
The entire task is wrapped in try/except; any unhandled error saves
pr_status="failed".
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import py_compile
import tempfile
import uuid as _uuid_module
from typing import Any, Dict, List, Optional

import httpx
import sqlalchemy.exc
from celery.utils.log import get_task_logger

from app.database.session import AsyncSessionLocal
from app.worker.celery_app import celery_app

logger = get_task_logger(__name__)

_GITHUB_API_BASE = "https://api.github.com"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _github_headers(token: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "NeuralOps-PRBot/1.0",
    }


async def _save_pr_fields(
    incident_id: str,
    pr_url: Optional[str],
    pr_number: Optional[int],
    pr_status: str,
) -> None:
    """
    Persist PR metadata to the incidents table via its own session.
    Uses AsyncSessionLocal() independently — NOT the run_agent session.
    """
    from sqlalchemy import update

    from app.models.incidents import Incident

    try:
        incident_uuid = _uuid_module.UUID(incident_id)
    except (ValueError, AttributeError) as exc:
        logger.error(
            "github_pr_invalid_incident_id",
            extra={"incident_id": incident_id, "error": str(exc)},
        )
        return

    try:
        async with AsyncSessionLocal() as session:
            async with session.begin():
                await session.execute(
                    update(Incident)
                    .where(Incident.id == incident_uuid)
                    .values(
                        pr_url=pr_url,
                        pr_number=pr_number,
                        pr_status=pr_status,
                    )
                )
        logger.info(
            "github_pr_status_saved",
            extra={
                "incident_id": incident_id,
                "pr_status": pr_status,
                "pr_url": pr_url,
                "pr_number": pr_number,
            },
        )
    except Exception as exc:
        logger.error(
            "github_pr_status_save_failed",
            extra={"incident_id": incident_id, "error": str(exc)},
        )


async def _fetch_file_from_github(
    client: httpx.AsyncClient,
    owner: str,
    repo: str,
    file_path: str,
    branch: str,
    token: str,
) -> Optional[str]:
    """
    Fetch current file content from GitHub as raw text.
    Returns None if file not found or any error occurs.
    """
    url = (
        f"{_GITHUB_API_BASE}/repos/{owner}/{repo}"
        f"/contents/{file_path}?ref={branch}"
    )
    headers = {
        **_github_headers(token),
        "Accept": "application/vnd.github.v3.raw",
    }
    try:
        response = await client.get(url, headers=headers)
    except Exception as exc:
        logger.warning(
            "github_pr_file_fetch_error",
            extra={"file_path": file_path, "error": str(exc)},
        )
        return None

    if response.status_code == 404:
        logger.warning(
            "github_pr_file_not_found",
            extra={"file_path": file_path, "branch": branch},
        )
        return None

    if response.status_code != 200:
        logger.warning(
            "github_pr_file_fetch_failed",
            extra={
                "file_path": file_path,
                "status_code": response.status_code,
                "body": response.text[:200],
            },
        )
        return None

    return response.text


def _check_python_syntax(source_code: str) -> Optional[str]:
    """
    Validate Python source syntax via py_compile.
    Returns None if syntax is valid, or an error message string if invalid.
    """
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False, encoding="utf-8"
        ) as tmp:
            tmp.write(source_code)
            tmp_path = tmp.name

        try:
            py_compile.compile(tmp_path, doraise=True)
            return None  # valid
        except py_compile.PyCompileError as exc:
            return str(exc)
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    except Exception as exc:
        return f"Syntax check failed: {exc}"


# ---------------------------------------------------------------------------
# Core async logic
# ---------------------------------------------------------------------------


async def _execute_create_github_pr(
    tenant_id: str,
    incident_id: str,
    structured_patch: str,
    error_type: str,
    root_cause: str,
    suggested_fix: str,
) -> None:
    """
    Core async coroutine. Called via asyncio.run() from the Celery task.
    """
    from sqlalchemy.future import select

    from app.models.snapshots import TenantSnapshot
    from app.services.github_auth import get_installation_token

    # ── Step 1: Load TenantSnapshot ──────────────────────────────────────────
    try:
        tenant_uuid = _uuid_module.UUID(tenant_id)
    except (ValueError, AttributeError) as exc:
        logger.error(
            "github_pr_invalid_tenant_id",
            extra={"tenant_id": tenant_id, "error": str(exc)},
        )
        await _save_pr_fields(incident_id, None, None, "failed")
        return

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(TenantSnapshot).where(TenantSnapshot.tenant_id == tenant_uuid)
        )
        snapshot: Optional[TenantSnapshot] = result.scalar_one_or_none()

    if snapshot is None:
        logger.warning(
            "github_pr_no_tenant_snapshot",
            extra={"tenant_id": tenant_id, "incident_id": incident_id},
        )
        await _save_pr_fields(incident_id, None, None, "failed")
        return

    # ── Step 2: Check GitHub App installation ─────────────────────────────────
    installation_id: Optional[int] = snapshot.github_installation_id
    if installation_id is None:
        logger.warning(
            "github_pr_no_installation",
            extra={"tenant_id": tenant_id, "incident_id": incident_id},
        )
        await _save_pr_fields(incident_id, None, None, "skipped")
        return

    owner: str = snapshot.github_repo_owner or ""
    repo: str = snapshot.github_repo_name or ""
    default_branch: str = snapshot.github_default_branch or "main"

    if not owner or not repo:
        logger.warning(
            "github_pr_missing_repo_metadata",
            extra={"tenant_id": tenant_id, "incident_id": incident_id},
        )
        await _save_pr_fields(incident_id, None, None, "skipped")
        return

    # ── Step 3: Fetch installation token ─────────────────────────────────────
    try:
        token: str = await get_installation_token(installation_id)
    except Exception as exc:
        logger.error(
            "github_pr_token_fetch_failed",
            extra={
                "tenant_id": tenant_id,
                "incident_id": incident_id,
                "error": str(exc),
            },
        )
        await _save_pr_fields(incident_id, None, None, "failed")
        return

    # ── Step 4: Parse structured_patch JSON ───────────────────────────────────
    try:
        patch_data: Dict[str, Any] = json.loads(structured_patch)
        patches: List[Dict[str, str]] = patch_data.get("patches") or []
    except (json.JSONDecodeError, TypeError) as exc:
        logger.error(
            "github_pr_patch_parse_failed",
            extra={"incident_id": incident_id, "error": str(exc)},
        )
        await _save_pr_fields(incident_id, None, None, "failed")
        return

    if not patches:
        logger.warning(
            "github_pr_empty_patches",
            extra={"incident_id": incident_id},
        )
        await _save_pr_fields(incident_id, None, None, "no_patch")
        return

    # ── Step 5: Fetch, apply, and validate each patch ─────────────────────────
    # changed_files: {file_path: new_content_str}
    changed_files: Dict[str, str] = {}

    async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
        for patch in patches:
            file_path: str = patch.get("file", "")
            search_str: str = patch.get("search", "")
            replace_str: str = patch.get("replace", "")

            if not file_path or not search_str:
                logger.warning(
                    "github_pr_patch_missing_fields",
                    extra={"incident_id": incident_id, "file": file_path},
                )
                continue

            # a) Fetch current file content from GitHub
            old_content = await _fetch_file_from_github(
                client, owner, repo, file_path, default_branch, token
            )
            if old_content is None:
                logger.warning(
                    "github_pr_file_not_on_github",
                    extra={"file_path": file_path, "incident_id": incident_id},
                )
                continue

            # b) Apply patch
            new_content = old_content.replace(search_str, replace_str, 1)
            if new_content == old_content:
                logger.warning(
                    "github_pr_search_not_found_in_github",
                    extra={
                        "file_path": file_path,
                        "incident_id": incident_id,
                        "search_preview": search_str[:80],
                    },
                )
                continue

            # c) Syntax check for .py files — abort ALL patches on failure
            if file_path.endswith(".py"):
                syntax_error = _check_python_syntax(new_content)
                if syntax_error:
                    logger.error(
                        "github_pr_syntax_error",
                        extra={
                            "file_path": file_path,
                            "incident_id": incident_id,
                            "syntax_error": syntax_error,
                        },
                    )
                    await _save_pr_fields(incident_id, None, None, "syntax_error")
                    return  # abort ALL patches

            changed_files[file_path] = new_content

    # ── Step 6: Check we have at least one patched file ───────────────────────
    if not changed_files:
        logger.warning(
            "github_pr_no_files_changed",
            extra={"incident_id": incident_id},
        )
        await _save_pr_fields(incident_id, None, None, "no_patch")
        return

    # ── Step 7: Create branch ─────────────────────────────────────────────────
    branch_name = f"neuralops-fix/{incident_id[:8]}"
    base_sha: str = ""

    async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
        # Get base SHA from default branch
        ref_url = (
            f"{_GITHUB_API_BASE}/repos/{owner}/{repo}"
            f"/git/ref/heads/{default_branch}"
        )
        ref_resp = await client.get(ref_url, headers=_github_headers(token))
        if ref_resp.status_code != 200:
            logger.error(
                "github_pr_get_ref_failed",
                extra={
                    "incident_id": incident_id,
                    "status_code": ref_resp.status_code,
                    "body": ref_resp.text[:200],
                },
            )
            await _save_pr_fields(incident_id, None, None, "failed")
            return

        base_sha = ref_resp.json()["object"]["sha"]

        # Create the branch
        create_ref_url = f"{_GITHUB_API_BASE}/repos/{owner}/{repo}/git/refs"
        create_ref_resp = await client.post(
            create_ref_url,
            headers=_github_headers(token),
            json={
                "ref": f"refs/heads/{branch_name}",
                "sha": base_sha,
            },
        )
        if create_ref_resp.status_code not in (200, 201, 422):
            # 422 = branch already exists, which is fine
            logger.error(
                "github_pr_create_ref_failed",
                extra={
                    "incident_id": incident_id,
                    "status_code": create_ref_resp.status_code,
                    "body": create_ref_resp.text[:200],
                },
            )
            await _save_pr_fields(incident_id, None, None, "failed")
            return

        # ── Step 8: Commit via GitHub Tree API ────────────────────────────────
        import base64

        # Build tree entries (mode 100644 = file blob)
        tree_entries: List[Dict[str, Any]] = []
        for fp, content in changed_files.items():
            tree_entries.append(
                {
                    "path": fp,
                    "mode": "100644",
                    "type": "blob",
                    "content": content,
                }
            )

        tree_url = f"{_GITHUB_API_BASE}/repos/{owner}/{repo}/git/trees"
        tree_resp = await client.post(
            tree_url,
            headers=_github_headers(token),
            json={
                "base_tree": base_sha,
                "tree": tree_entries,
            },
        )
        if tree_resp.status_code not in (200, 201):
            logger.error(
                "github_pr_create_tree_failed",
                extra={
                    "incident_id": incident_id,
                    "status_code": tree_resp.status_code,
                    "body": tree_resp.text[:200],
                },
            )
            await _save_pr_fields(incident_id, None, None, "failed")
            return

        tree_sha = tree_resp.json()["sha"]

        # Create commit
        commit_message = (
            f"fix({error_type}): NeuralOps AI fix for incident {incident_id[:8]}\n\n"
            f"Automated patch generated by NeuralOps.\n"
            f"⚠️ This is AI-generated code — review before merging."
        )
        commit_url = f"{_GITHUB_API_BASE}/repos/{owner}/{repo}/git/commits"
        commit_resp = await client.post(
            commit_url,
            headers=_github_headers(token),
            json={
                "message": commit_message,
                "tree": tree_sha,
                "parents": [base_sha],
            },
        )
        if commit_resp.status_code not in (200, 201):
            logger.error(
                "github_pr_create_commit_failed",
                extra={
                    "incident_id": incident_id,
                    "status_code": commit_resp.status_code,
                    "body": commit_resp.text[:200],
                },
            )
            await _save_pr_fields(incident_id, None, None, "failed")
            return

        commit_sha = commit_resp.json()["sha"]

        # Update branch ref to point to new commit
        update_ref_url = (
            f"{_GITHUB_API_BASE}/repos/{owner}/{repo}"
            f"/git/refs/heads/{branch_name}"
        )
        update_resp = await client.patch(
            update_ref_url,
            headers=_github_headers(token),
            json={"sha": commit_sha, "force": False},
        )
        if update_resp.status_code not in (200, 201):
            logger.error(
                "github_pr_update_ref_failed",
                extra={
                    "incident_id": incident_id,
                    "status_code": update_resp.status_code,
                    "body": update_resp.text[:200],
                },
            )
            await _save_pr_fields(incident_id, None, None, "failed")
            return

        # ── Step 9: Create PR ─────────────────────────────────────────────────
        files_changed_list = "\n".join(f"- `{fp}`" for fp in changed_files)
        pr_body = f"""\
## 🤖 NeuralOps AI-Generated Fix

> ⚠️ **This pull request was generated automatically by NeuralOps AI.**
> **Review all changes carefully before merging. AI-generated code may contain errors.**

---

### Incident ID
`{incident_id}`

### Root Cause
{root_cause or "_Not available_"}

### Suggested Fix
{suggested_fix or "_Not available_"}

### Files Modified
{files_changed_list}

---

*Generated by [NeuralOps](https://neuralops.io) — AI-powered incident debugging.*
"""

        pr_url_api = f"{_GITHUB_API_BASE}/repos/{owner}/{repo}/pulls"
        pr_resp = await client.post(
            pr_url_api,
            headers=_github_headers(token),
            json={
                "title": (
                    f"fix({error_type}): NeuralOps AI fix for incident {incident_id[:8]}"
                ),
                "body": pr_body,
                "base": default_branch,
                "head": branch_name,
            },
        )

        if pr_resp.status_code not in (200, 201):
            logger.error(
                "github_pr_create_pr_failed",
                extra={
                    "incident_id": incident_id,
                    "status_code": pr_resp.status_code,
                    "body": pr_resp.text[:200],
                },
            )
            await _save_pr_fields(incident_id, None, None, "failed")
            return

        pr_data = pr_resp.json()
        pr_html_url: str = pr_data.get("html_url", "")
        pr_number_val: int = pr_data.get("number", 0)

    # ── Step 10: Persist PR metadata ──────────────────────────────────────────
    await _save_pr_fields(
        incident_id=incident_id,
        pr_url=pr_html_url,
        pr_number=pr_number_val,
        pr_status="open",
    )

    logger.info(
        "github_pr_created",
        extra={
            "incident_id": incident_id,
            "tenant_id": tenant_id,
            "pr_url": pr_html_url,
            "pr_number": pr_number_val,
            "branch": branch_name,
            "files_changed": list(changed_files.keys()),
        },
    )


# ---------------------------------------------------------------------------
# Celery task entry point
# ---------------------------------------------------------------------------


@celery_app.task(
    name="app.worker.tasks.github_pr.create_github_pr",
    bind=True,
    acks_late=True,
    reject_on_worker_lost=True,
    autoretry_for=(
        OSError,
        ConnectionError,
        TimeoutError,
        httpx.NetworkError,
        httpx.TimeoutException,
    ),
    max_retries=3,
    default_retry_delay=15,
    soft_time_limit=300,
    time_limit=360,
)
def create_github_pr(
    self,
    *,
    tenant_id: str,
    incident_id: str,
    structured_patch: str,
    error_type: str,
    root_cause: str,
    suggested_fix: str,
) -> Dict[str, Any]:
    """
    Celery task: create a GitHub PR for an AI-generated patch.

    Parameters
    ----------
    tenant_id       : str  — UUID of the owning tenant.
    incident_id     : str  — UUID of the incident whose patch we are applying.
    structured_patch: str  — JSON string from PatchGeneratorNode.
    error_type      : str  — Exception class name (for PR title).
    root_cause      : str  — Root cause text (for PR body).
    suggested_fix   : str  — Suggested fix text (for PR body).
    """
    task_id: str = self.request.id or str(_uuid_module.uuid4())

    logger.info(
        "github_pr_task_received",
        extra={
            "tenant_id": tenant_id,
            "incident_id": incident_id,
            "error_type": error_type,
            "task_id": task_id,
            "attempt": self.request.retries + 1,
        },
    )

    try:
        asyncio.run(
            _execute_create_github_pr(
                tenant_id=tenant_id,
                incident_id=incident_id,
                structured_patch=structured_patch,
                error_type=error_type,
                root_cause=root_cause,
                suggested_fix=suggested_fix,
            )
        )
    except Exception as exc:
        logger.error(
            "github_pr_task_failed",
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
        # Save failed status on final retry exhaustion — not on intermediate
        # retries, as they may succeed.
        if self.request.retries >= self.max_retries:
            asyncio.run(
                _save_pr_fields(
                    incident_id=incident_id,
                    pr_url=None,
                    pr_number=None,
                    pr_status="failed",
                )
            )
        raise

    logger.info(
        "github_pr_task_complete",
        extra={
            "tenant_id": tenant_id,
            "incident_id": incident_id,
            "task_id": task_id,
        },
    )

    return {"status": "ok", "tenant_id": tenant_id, "incident_id": incident_id}