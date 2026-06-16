"""
fastapi/app/worker/tasks/index_code.py

Celery task: ``index_code``

Drives the NeuralOps AST code-indexing pipeline for both initial full-repo
imports and incremental push-webhook updates.

Task overview
-------------
``is_initial=True``  — Download the full repository tarball from GitHub,
                       extract it, upload every ``.py`` / ``.java`` file to
                       S3, parse each file with ``ASTIndexer``, and bulk-
                       insert rows into ``code_index``.

``is_initial=False`` — Process only the files listed in ``changed_files``
                       (fetch + re-index) and ``removed_files`` (delete from
                       DB + invalidate Redis cache).

Async operation
---------------
Because FastAPI uses an async database layer (``asyncpg`` via
``AsyncSessionLocal``) and async S3 (``aioboto3``), all async logic is
wrapped in a dedicated coroutine (``_run_index``) that is called via
``asyncio.run()`` inside the synchronous Celery task function.  This is
the canonical pattern for running async code inside Celery workers that
live outside the FastAPI event loop.

Fernet decryption
-----------------
The tenant's GitHub PAT (``encrypted_github_pat``) and webhook secret
(``github_webhook_secret``) are stored as Fernet ciphertext in the
``tenant_snapshots`` table.  They are decrypted at task execution time
using the shared ``FERNET_ENCRYPTION_KEY`` environment variable which
must be identical across the Django and FastAPI services.

Architecture reference: NeuralOps Technical Documentation — Section 17
(Code Indexing — Background), Section 3 (Service 2 — FastAPI).
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import shutil
import tarfile
import tempfile
import uuid
from datetime import datetime as _dt
from datetime import timezone as _tz
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import aioboto3
import httpx
from botocore.exceptions import BotoCoreError, ClientError
from celery import shared_task
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.database.session import AsyncSessionLocal
from app.models.code_index import CodeIndex
from app.models.outbox import OutboxEvent, write_outbox
from app.models.snapshots import TenantSnapshot
from app.utils.ast_parser import ASTIndexer, SymbolInfo
from app.worker.celery_app import celery_app

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SUPPORTED_EXTENSIONS = frozenset({".py", ".java"})

# Maximum characters for a Redis key value — safety guard.
_MAX_S3_KEY_LEN = 1024

# GitHub API base URL.
_GITHUB_API_BASE = "https://api.github.com"

# Indexer singleton — stateless, safe to reuse across task invocations.
_INDEXER = ASTIndexer()


# ---------------------------------------------------------------------------
# Decryption helper
# ---------------------------------------------------------------------------


def _decrypt(cipher_text: str) -> str:
    """
    Fernet-decrypt *cipher_text* using the service's ``FERNET_ENCRYPTION_KEY``.

    Raises:
        RuntimeError — key missing or decryption fails.
    """
    settings = get_settings()
    raw_key = getattr(settings, "FERNET_ENCRYPTION_KEY", None)
    if not raw_key:
        raise RuntimeError(
            "FERNET_ENCRYPTION_KEY is not set. Cannot decrypt GitHub credentials."
        )
    try:
        f = Fernet(raw_key.encode() if isinstance(raw_key, str) else raw_key)
        return f.decrypt(cipher_text.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        raise RuntimeError(
            "Fernet decryption failed — invalid ciphertext or mismatched key."
        ) from exc


# ---------------------------------------------------------------------------
# S3 helpers
# ---------------------------------------------------------------------------


def _build_s3_key(
    tenant_id: str,
    repo_name: str,
    commit_sha: str,
    file_path: str,
) -> str:
    """
    Construct the canonical S3 object key for an indexed source file.

    Format: ``code/{tenant_id}/{repo_name}/{commit_sha}/{file_path}``
    """
    return f"code/{tenant_id}/{repo_name}/{commit_sha}/{file_path}"


async def _upload_file_to_s3(
    file_bytes: bytes,
    s3_key: str,
    tenant_id: str,
) -> None:
    """
    Upload *file_bytes* to S3 at *s3_key* using aioboto3.

    Raises:
        RuntimeError — if the upload fails after boto error.
    """
    settings = get_settings()
    session = aioboto3.Session(
        aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
        region_name=settings.AWS_REGION_NAME,
    )
    try:
        async with session.client(
            "s3", endpoint_url=settings.AWS_S3_ENDPOINT_URL
        ) as s3:
            await s3.put_object(
                Bucket=settings.AWS_S3_BUCKET_NAME,
                Key=s3_key,
                Body=file_bytes,
                ContentType="text/plain",
                Metadata={"tenant_id": tenant_id},
            )
        logger.debug("s3_upload_success", extra={"s3_key": s3_key})
    except (ClientError, BotoCoreError) as exc:
        raise RuntimeError(f"S3 upload failed for key '{s3_key}': {exc}") from exc


# ---------------------------------------------------------------------------
# Redis cache invalidation helper
# ---------------------------------------------------------------------------


async def _invalidate_redis_cache(s3_key: str) -> None:
    """
    Delete the Redis L1 file-content cache entry ``code:{s3_key}``.

    Cache write errors are caught and logged — a failed invalidation is
    non-fatal; the cache will expire naturally after 24 hours.
    """
    import redis.asyncio as aioredis

    settings = get_settings()
    redis_key = f"code:{s3_key}"
    try:
        client = aioredis.from_url(
            settings.REDIS_URL,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
        await client.delete(redis_key)
        await client.aclose()
        logger.debug(
            "redis_cache_invalidated",
            extra={"redis_key": redis_key},
        )
    except Exception as exc:
        logger.warning(
            "redis_cache_invalidation_failed",
            extra={"redis_key": redis_key, "error": str(exc)},
        )


# ---------------------------------------------------------------------------
# Database helpers — code_index CRUD
# ---------------------------------------------------------------------------


async def _delete_file_rows(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    repo_url: str,
    file_path: str,
) -> None:
    """Delete all ``code_index`` rows for a specific file within a tenant/repo."""
    await session.execute(
        delete(CodeIndex).where(
            CodeIndex.tenant_id == tenant_id,
            CodeIndex.repo_url == repo_url,
            CodeIndex.file_path == file_path,
        )
    )


async def _insert_symbols(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    repo_url: str,
    file_path: str,
    commit_sha: str,
    s3_key: str,
    symbols: List[SymbolInfo],
) -> None:
    """
    Bulk-insert ``CodeIndex`` rows for every symbol extracted from a file.

    Uses individual ORM inserts (not bulk_insert_mappings) so that
    SQLAlchemy event listeners and the RLS middleware remain in effect.
    """
    for sym in symbols:
        row = CodeIndex(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            repo_url=repo_url,
            file_path=file_path,
            symbol_name=sym.symbol_name,
            chunk_type=sym.chunk_type,
            start_line=sym.start_line,
            end_line=sym.end_line,
            calls=sym.calls or [],
            called_by=[],
            imports=sym.imports or [],
            s3_key=s3_key,
            last_commit=commit_sha,
        )
        session.add(row)


# ---------------------------------------------------------------------------
# Tenant snapshot helpers
# ---------------------------------------------------------------------------


async def _get_tenant_snapshot(
    tenant_id: uuid.UUID,
) -> Optional[TenantSnapshot]:
    """Fetch the ``TenantSnapshot`` row for *tenant_id* from DB-2."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(TenantSnapshot).where(TenantSnapshot.tenant_id == tenant_id)
        )
        return result.scalar_one_or_none()


async def _update_indexing_status(
    tenant_id: uuid.UUID,
    status: str,
    commit_sha: Optional[str] = None,
) -> None:
    """
    Update ``github_indexing_status`` (and optionally ``github_last_indexed_commit``)
    in the ``tenant_snapshots`` table, then write an outbox event so Debezium
    publishes it to the ``indexing.status`` Kafka topic for Django to consume.
    """
    values: Dict = {"github_indexing_status": status}
    if commit_sha:
        values["github_last_indexed_commit"] = commit_sha

    event_id = uuid.uuid4()
    outbox_payload = {
        "event_id": str(event_id),
        "event_type": "indexing.status.updated",
        "tenant_id": str(tenant_id),
        "status": status,
        "commit_sha": commit_sha,
        "occurred_at": _dt.now(_tz.utc).isoformat(),
    }

    async with AsyncSessionLocal() as session:
        async with session.begin():
            # 1. Update tenant_snapshots (DB-2) as before.
            await session.execute(
                update(TenantSnapshot)
                .where(TenantSnapshot.tenant_id == tenant_id)
                .values(**values)
            )
            # 2. Write outbox event in the same transaction.
            #    Debezium tails the DB-2 WAL and delivers this to Kafka
            #    topic "indexing.status", which Django's consumer reads.
            write_outbox(
                session=session,
                topic="indexing.status",
                key=str(tenant_id),
                payload=outbox_payload,
            )

    logger.info(
        "index_status_updated",
        extra={
            "tenant_id": str(tenant_id),
            "status": status,
            "commit_sha": commit_sha,
            "outbox_event_id": str(event_id),
        },
    )


# ---------------------------------------------------------------------------
# GitHub API helpers
# ---------------------------------------------------------------------------


def _github_headers(pat: str) -> Dict[str, str]:
    return {
        "Authorization": f"token {pat}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "NeuralOps-Indexer/1.0",
    }


async def _download_repo_tarball(
    owner: str,
    repo: str,
    branch: str,
    pat: str,
) -> bytes:
    """
    Download the repository as a ``.tar.gz`` archive via the GitHub Tarball API.

    Follows redirects — GitHub returns a 302 to an S3 pre-signed URL.

    Raises:
        RuntimeError — if the download fails.
    """
    url = f"{_GITHUB_API_BASE}/repos/{owner}/{repo}/tarball/{branch}"
    async with httpx.AsyncClient(follow_redirects=True, timeout=120) as client:
        response = await client.get(url, headers=_github_headers(pat))
    if response.status_code != 200:
        raise RuntimeError(
            f"GitHub tarball download failed: {response.status_code} {response.text[:200]}"
        )
    return response.content


async def _fetch_file_content(
    owner: str,
    repo: str,
    file_path: str,
    commit_sha: str,
    pat: str,
) -> bytes:
    """
    Fetch the raw content of a single file at a specific commit using the
    GitHub Contents API, returning raw bytes.

    The Contents API returns base64-encoded content in JSON; this function
    decodes it automatically.  For large files (>1 MB) GitHub redirects to
    the blob URL — we follow the redirect transparently.

    Raises:
        RuntimeError — if the file cannot be fetched.
    """
    import base64 as _b64
    import json as _json

    url = (
        f"{_GITHUB_API_BASE}/repos/{owner}/{repo}"
        f"/contents/{file_path}?ref={commit_sha}"
    )
    headers = {
        **_github_headers(pat),
        # Request raw bytes directly when GitHub supports it.
        "Accept": "application/vnd.github.v3.raw",
    }
    async with httpx.AsyncClient(follow_redirects=True, timeout=60) as client:
        response = await client.get(url, headers=headers)

    if response.status_code == 404:
        logger.warning(
            "github_file_not_found",
            extra={"file_path": file_path, "commit_sha": commit_sha},
        )
        return b""

    if response.status_code != 200:
        raise RuntimeError(
            f"GitHub file fetch failed ({file_path}): "
            f"{response.status_code} {response.text[:200]}"
        )

    # When Accept: application/vnd.github.v3.raw, GitHub returns raw bytes.
    # Fallback: if response is JSON, decode base64 content field.
    content_type = response.headers.get("content-type", "")
    if "json" in content_type:
        try:
            data = _json.loads(response.content)
            if data.get("encoding") == "base64":
                return _b64.b64decode(data["content"])
        except Exception:
            pass

    return response.content


# ---------------------------------------------------------------------------
# Core async logic — initial indexing
# ---------------------------------------------------------------------------


async def _run_initial_index(
    tenant_id: uuid.UUID,
    repo_url: str,
    commit_sha: str,
    owner: str,
    repo: str,
    branch: str,
    pat: str,
    tenant_id_str: str,
) -> None:
    """
    Full repository indexing:
    1. Download tarball from GitHub.
    2. Extract to a secure temp directory.
    3. For each .py/.java file: upload to S3 + parse + insert into DB.
    4. Mark indexing status as 'indexed'.
    5. Clean up temp directory.
    """
    # Mark status as 'indexing' immediately.
    await _update_indexing_status(tenant_id, "indexing")

    tmp_dir = Path(tempfile.mkdtemp(prefix=f"neuralops-index-{uuid.uuid4()}-"))
    logger.info(
        "initial_index_started",
        extra={
            "tenant_id": tenant_id_str,
            "repo": f"{owner}/{repo}",
            "branch": branch,
            "tmp_dir": str(tmp_dir),
        },
    )

    try:
        # ── Download tarball ──────────────────────────────────────────────────
        logger.info("downloading_github_tarball", extra={"owner": owner, "repo": repo})
        tarball_bytes = await _download_repo_tarball(owner, repo, branch, pat)

        # ── Extract ───────────────────────────────────────────────────────────
        tar_path = tmp_dir / "repo.tar.gz"
        tar_path.write_bytes(tarball_bytes)
        del tarball_bytes  # free memory

        with tarfile.open(tar_path, "r:gz") as tar:
            tar.extractall(path=tmp_dir, filter="data")  # safe extraction

        # GitHub tarballs wrap files in a top-level directory like
        # ``{owner}-{repo}-{sha}/``.  Find that directory.
        extracted_dirs = [
            d for d in tmp_dir.iterdir() if d.is_dir() and d.name != "__MACOSX"
        ]
        repo_root = extracted_dirs[0] if extracted_dirs else tmp_dir

        # ── Walk files ────────────────────────────────────────────────────────
        all_files = [
            p
            for p in repo_root.rglob("*")
            if p.is_file() and p.suffix in SUPPORTED_EXTENSIONS
        ]

        logger.info(
            "initial_index_file_count",
            extra={"count": len(all_files), "repo": f"{owner}/{repo}"},
        )

        indexed_count = 0
        failed_count = 0

        for abs_path in all_files:
            # Relative path from repo root (used as file_path in DB).
            rel_path = abs_path.relative_to(repo_root).as_posix()
            s3_key = _build_s3_key(tenant_id_str, repo, commit_sha, rel_path)

            try:
                file_bytes = abs_path.read_bytes()
                if not file_bytes:
                    continue

                # Upload to S3.
                await _upload_file_to_s3(file_bytes, s3_key, tenant_id_str)

                # Parse symbols.
                symbols = _INDEXER.extract_symbols(file_bytes, abs_path.suffix)

                if not symbols:
                    # File has no indexable symbols (e.g. empty __init__.py).
                    continue

                # Insert into DB.
                async with AsyncSessionLocal() as session:
                    async with session.begin():
                        await _insert_symbols(
                            session,
                            tenant_id,
                            repo_url,
                            rel_path,
                            commit_sha,
                            s3_key,
                            symbols,
                        )

                indexed_count += 1

            except Exception as exc:
                failed_count += 1
                logger.error(
                    "initial_index_file_failed",
                    extra={
                        "file": rel_path,
                        "tenant_id": tenant_id_str,
                        "error": str(exc),
                    },
                    exc_info=True,
                )
                # Continue with other files — partial index is better than none.
                continue

        # ── Update snapshot status ────────────────────────────────────────────
        await _update_indexing_status(tenant_id, "indexed", commit_sha)

        logger.info(
            "initial_index_complete",
            extra={
                "tenant_id": tenant_id_str,
                "indexed": indexed_count,
                "failed": failed_count,
                "commit_sha": commit_sha,
            },
        )

    except Exception as exc:
        # Mark as failed so the UI shows the error state.
        await _update_indexing_status(tenant_id, "failed")
        logger.error(
            "initial_index_fatal_error",
            extra={"tenant_id": tenant_id_str, "error": str(exc)},
            exc_info=True,
        )
        raise

    finally:
        # ── Cleanup temp directory ────────────────────────────────────────────
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            logger.debug("temp_dir_cleaned", extra={"tmp_dir": str(tmp_dir)})
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Core async logic — incremental indexing
# ---------------------------------------------------------------------------


async def _run_incremental_index(
    tenant_id: uuid.UUID,
    repo_url: str,
    commit_sha: str,
    changed_files: List[str],
    removed_files: List[str],
    owner: str,
    repo: str,
    pat: str,
    tenant_id_str: str,
) -> None:
    """
    Incremental (push-webhook) indexing:
    1. For each changed file: fetch content → upload to S3 → delete old rows
       → insert fresh rows → invalidate Redis cache.
    2. For each removed file: delete DB rows → invalidate Redis cache.
    3. Update ``github_last_indexed_commit``.
    """
    logger.info(
        "incremental_index_started",
        extra={
            "tenant_id": tenant_id_str,
            "commit_sha": commit_sha,
            "changed": len(changed_files),
            "removed": len(removed_files),
        },
    )

    # ── Process changed / added files ─────────────────────────────────────────
    for file_path in changed_files:
        ext = Path(file_path).suffix
        if ext not in SUPPORTED_EXTENSIONS:
            continue

        new_s3_key = _build_s3_key(tenant_id_str, repo, commit_sha, file_path)

        # Determine the OLD s3_key so we can invalidate the Redis cache.
        # The old key is whatever commit SHA was last indexed for this file.
        old_s3_key: Optional[str] = None

        try:
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    select(CodeIndex.s3_key, CodeIndex.last_commit)
                    .where(
                        CodeIndex.tenant_id == tenant_id,
                        CodeIndex.repo_url == repo_url,
                        CodeIndex.file_path == file_path,
                    )
                    .limit(1)
                )
                row = result.first()
                if row:
                    old_s3_key = row.s3_key
        except Exception as exc:
            logger.warning(
                "incremental_index_old_key_lookup_failed",
                extra={"file_path": file_path, "error": str(exc)},
            )

        try:
            # Fetch file content from GitHub.
            file_bytes = await _fetch_file_content(
                owner, repo, file_path, commit_sha, pat
            )
            if not file_bytes:
                logger.warning(
                    "incremental_index_empty_file",
                    extra={"file_path": file_path, "commit_sha": commit_sha},
                )
                # File was deleted or empty — treat as removal.
                async with AsyncSessionLocal() as session:
                    async with session.begin():
                        await _delete_file_rows(session, tenant_id, repo_url, file_path)
                if old_s3_key:
                    await _invalidate_redis_cache(old_s3_key)
                continue

            # Upload to S3.
            await _upload_file_to_s3(file_bytes, new_s3_key, tenant_id_str)

            # Parse symbols.
            symbols = _INDEXER.extract_symbols(file_bytes, ext)

            # Atomic DB transaction: delete old rows + insert fresh ones.
            async with AsyncSessionLocal() as session:
                async with session.begin():
                    await _delete_file_rows(session, tenant_id, repo_url, file_path)
                    if symbols:
                        await _insert_symbols(
                            session,
                            tenant_id,
                            repo_url,
                            file_path,
                            commit_sha,
                            new_s3_key,
                            symbols,
                        )

            # Invalidate L1 cache for the old S3 key.
            if old_s3_key and old_s3_key != new_s3_key:
                await _invalidate_redis_cache(old_s3_key)

            logger.debug(
                "incremental_index_file_done",
                extra={
                    "file_path": file_path,
                    "symbols": len(symbols),
                    "tenant_id": tenant_id_str,
                },
            )

        except Exception as exc:
            logger.error(
                "incremental_index_file_failed",
                extra={
                    "file_path": file_path,
                    "tenant_id": tenant_id_str,
                    "error": str(exc),
                },
                exc_info=True,
            )
            # Continue processing other files.
            continue

    # ── Process removed files ─────────────────────────────────────────────────
    for file_path in removed_files:
        try:
            # Get old S3 key for cache invalidation before deleting rows.
            old_s3_key = None
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    select(CodeIndex.s3_key)
                    .where(
                        CodeIndex.tenant_id == tenant_id,
                        CodeIndex.repo_url == repo_url,
                        CodeIndex.file_path == file_path,
                    )
                    .limit(1)
                )
                row = result.first()
                if row:
                    old_s3_key = row.s3_key

            # Delete from DB.
            async with AsyncSessionLocal() as session:
                async with session.begin():
                    await _delete_file_rows(session, tenant_id, repo_url, file_path)

            # Invalidate L1 cache.
            if old_s3_key:
                await _invalidate_redis_cache(old_s3_key)

            logger.debug(
                "incremental_index_file_removed",
                extra={"file_path": file_path, "tenant_id": tenant_id_str},
            )

        except Exception as exc:
            logger.error(
                "incremental_index_remove_failed",
                extra={
                    "file_path": file_path,
                    "tenant_id": tenant_id_str,
                    "error": str(exc),
                },
                exc_info=True,
            )
            continue

    # ── Update commit SHA in snapshot ─────────────────────────────────────────
    await _update_indexing_status(tenant_id, "indexed", commit_sha)

    logger.info(
        "incremental_index_complete",
        extra={
            "tenant_id": tenant_id_str,
            "commit_sha": commit_sha,
        },
    )


# ---------------------------------------------------------------------------
# Entry-point dispatcher — runs the right coroutine based on is_initial
# ---------------------------------------------------------------------------


async def _run_index(
    tenant_id_str: str,
    repo_url: str,
    commit_sha: str,
    changed_files: List[str],
    removed_files: List[str],
    is_initial: bool,
) -> None:
    """
    Top-level async coroutine invoked by the Celery task via ``asyncio.run()``.

    Responsibilities:
    1. Validate tenant snapshot exists.
    2. Decrypt PAT.
    3. Derive owner/repo/branch from snapshot.
    4. Dispatch to initial or incremental indexing coroutine.
    """
    tenant_uuid = uuid.UUID(tenant_id_str)

    # ── Fetch tenant snapshot ─────────────────────────────────────────────────
    snapshot = await _get_tenant_snapshot(tenant_uuid)
    if snapshot is None:
        raise RuntimeError(
            f"TenantSnapshot not found for tenant_id={tenant_id_str}. "
            "Kafka config-sync consumer may be lagging."
        )

    if not snapshot.encrypted_github_pat:
        raise RuntimeError(
            f"No encrypted GitHub PAT configured for tenant {tenant_id_str}."
        )

    # ── Decrypt PAT ───────────────────────────────────────────────────────────
    try:
        plain_pat = _decrypt(snapshot.encrypted_github_pat)
    except RuntimeError as exc:
        raise RuntimeError(f"PAT decryption failed: {exc}") from exc

    # ── Derive repo owner, name, branch ──────────────────────────────────────
    owner = snapshot.github_repo_owner
    repo_name = snapshot.github_repo_name
    branch = snapshot.github_default_branch or "main"

    if not owner or not repo_name:
        raise RuntimeError(
            f"Tenant {tenant_id_str} snapshot is missing github_repo_owner "
            f"or github_repo_name. Re-connect the GitHub integration."
        )

    if is_initial:
        await _run_initial_index(
            tenant_id=tenant_uuid,
            repo_url=repo_url,
            commit_sha=commit_sha,
            owner=owner,
            repo=repo_name,
            branch=branch,
            pat=plain_pat,
            tenant_id_str=tenant_id_str,
        )
    else:
        await _run_incremental_index(
            tenant_id=tenant_uuid,
            repo_url=repo_url,
            commit_sha=commit_sha,
            changed_files=changed_files,
            removed_files=removed_files,
            owner=owner,
            repo=repo_name,
            pat=plain_pat,
            tenant_id_str=tenant_id_str,
        )


# ---------------------------------------------------------------------------
# Celery task
# ---------------------------------------------------------------------------


@celery_app.task(
    name="app.worker.tasks.index_code.index_code",
    bind=True,
    acks_late=True,
    reject_on_worker_lost=True,
    # Retry on transient failures (network errors, DB errors).
    # Logic errors (missing tenant, decryption failure) are NOT retried
    # because retrying will not fix them.
    autoretry_for=(
        RuntimeError,
        ConnectionError,
        TimeoutError,
        OSError,
    ),
    max_retries=10,  # code indexing tolerates longer delays
    default_retry_delay=10,  # seconds; Celery applies exponential backoff
    soft_time_limit=540,  # 9 minutes soft limit (SoftTimeLimitExceeded)
    time_limit=600,  # 10 minutes hard kill
)
def index_code(
    self,
    *,
    tenant_id: str,
    repo_url: str,
    commit_sha: str,
    changed_files: Optional[List[str]] = None,
    removed_files: Optional[List[str]] = None,
    is_initial: bool = False,
) -> Dict:
    """
    Celery task: index a GitHub repository (full or incremental).

    Parameters
    ----------
    tenant_id : str
        UUID string of the owning tenant.
    repo_url : str
        Full HTTPS clone URL of the repository.
    commit_sha : str
        SHA of the commit being indexed.
    changed_files : list[str], optional
        File paths to re-index (added + modified).  Only used when
        ``is_initial=False``.
    removed_files : list[str], optional
        File paths to remove from the index.  Only used when
        ``is_initial=False``.
    is_initial : bool
        ``True``  → full tarball import (first-time connection).
        ``False`` → incremental push-webhook update.

    Returns
    -------
    dict
        ``{"status": "ok", "tenant_id": ..., "commit_sha": ...}``
    """
    logger.info(
        "index_code_task_started",
        extra={
            "tenant_id": tenant_id,
            "commit_sha": commit_sha,
            "is_initial": is_initial,
            "changed_files": (changed_files or []),
            "removed_files": (removed_files or []),
            "task_id": self.request.id,
        },
    )

    try:
        asyncio.run(
            _run_index(
                tenant_id_str=tenant_id,
                repo_url=repo_url,
                commit_sha=commit_sha,
                changed_files=changed_files or [],
                removed_files=removed_files or [],
                is_initial=is_initial,
            )
        )
    except Exception as exc:
        logger.error(
            "index_code_task_failed",
            extra={
                "tenant_id": tenant_id,
                "commit_sha": commit_sha,
                "is_initial": is_initial,
                "error": str(exc),
                "task_id": self.request.id,
            },
            exc_info=True,
        )
        # Re-raise so Celery's autoretry_for mechanism can pick it up.
        raise

    logger.info(
        "index_code_task_complete",
        extra={
            "tenant_id": tenant_id,
            "commit_sha": commit_sha,
            "is_initial": is_initial,
            "task_id": self.request.id,
        },
    )

    return {
        "status": "ok",
        "tenant_id": tenant_id,
        "commit_sha": commit_sha,
        "is_initial": is_initial,
    }


# ---------------------------------------------------------------------------
# Cleanup Task
# ---------------------------------------------------------------------------


async def _cleanup_index_async(tenant_id_str: str) -> None:
    """
    Asynchronously purge all code indexes, S3 source files, and Redis cache
    entries for a specific tenant when their GitHub integration is deleted.
    """
    tenant_uuid = uuid.UUID(tenant_id_str)
    settings = get_settings()

    # 1. DB Cleanup: rapidly drop all parsed AST nodes for this tenant
    logger.info("cleanup_db_started", extra={"tenant_id": tenant_id_str})
    try:
        async with AsyncSessionLocal() as session:
            async with session.begin():
                await session.execute(
                    delete(CodeIndex).where(CodeIndex.tenant_id == tenant_uuid)
                )
    except Exception as exc:
        logger.error(
            "cleanup_db_failed",
            extra={"tenant_id": tenant_id_str, "error": str(exc)},
            exc_info=True,
        )

    # 2. S3 Cleanup: delete all objects under `code/{tenant_id}/`
    logger.info("cleanup_s3_started", extra={"tenant_id": tenant_id_str})
    boto_session = aioboto3.Session()
    prefix = f"code/{tenant_id_str}/"
    try:
        async with boto_session.client(
            "s3", endpoint_url=settings.AWS_S3_ENDPOINT_URL
        ) as s3:
            paginator = s3.get_paginator("list_objects_v2")
            async for page in paginator.paginate(
                Bucket=settings.AWS_S3_BUCKET_NAME, Prefix=prefix
            ):
                if "Contents" in page:
                    objects_to_delete = [
                        {"Key": obj["Key"]} for obj in page["Contents"]
                    ]
                    if objects_to_delete:
                        await s3.delete_objects(
                            Bucket=settings.AWS_S3_BUCKET_NAME,
                            Delete={"Objects": objects_to_delete},
                        )
    except (BotoCoreError, ClientError) as exc:
        logger.error(
            "cleanup_s3_failed", extra={"tenant_id": tenant_id_str, "error": str(exc)}
        )

    # 3. Redis Cleanup: scan and delete L1 cache keys
    logger.info("cleanup_redis_started", extra={"tenant_id": tenant_id_str})
    import redis.asyncio as aioredis

    try:
        client = aioredis.from_url(
            settings.REDIS_URL,
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
        cursor = 0
        pattern = f"code:{prefix}*"
        while True:
            cursor, keys = await client.scan(cursor=cursor, match=pattern, count=100)
            if keys:
                await client.delete(*keys)
            if cursor == 0:
                break
        await client.aclose()
    except Exception as exc:
        logger.error(
            "cleanup_redis_failed",
            extra={"tenant_id": tenant_id_str, "error": str(exc)},
        )

    # 4. Elasticsearch Cleanup: delete all logs for this tenant
    logger.info("cleanup_elasticsearch_started", extra={"tenant_id": tenant_id_str})
    from app.database.elasticsearch_client import get_es_client

    try:
        es_client = get_es_client()
        await es_client.delete_by_query(
            index="neuralops-logs*",
            body={
                "query": {"match": {"tenant_id": tenant_id_str}}
            },
            conflicts="proceed",
        )
    except Exception as exc:
        logger.error(
            "cleanup_elasticsearch_failed",
            extra={"tenant_id": tenant_id_str, "error": str(exc)},
        )


@celery_app.task(
    name="app.worker.tasks.index_code.cleanup_code_index",
    bind=True,
    acks_late=True,
    max_retries=3,
    default_retry_delay=10,
)
def cleanup_code_index(self, *, tenant_id: str) -> Dict:
    """
    Celery task: delete all indexed AST data and source files for a tenant.
    """
    logger.info(
        "cleanup_code_index_task_started",
        extra={
            "tenant_id": tenant_id,
            "task_id": self.request.id,
        },
    )

    try:
        asyncio.run(_cleanup_index_async(tenant_id))
    except Exception as exc:
        logger.error(
            "cleanup_code_index_task_failed",
            extra={
                "tenant_id": tenant_id,
                "error": str(exc),
                "task_id": self.request.id,
            },
            exc_info=True,
        )
        raise

    logger.info(
        "cleanup_code_index_task_complete",
        extra={
            "tenant_id": tenant_id,
            "task_id": self.request.id,
        },
    )

    return {"status": "ok", "tenant_id": tenant_id}
