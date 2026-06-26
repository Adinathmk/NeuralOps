"""
django/apps/integrations/models.py

GitHubIntegration model — Django-owned (DB-1).

Stores the GitHub repository connection details for a tenant:
  - Repository metadata (URL, owner, name, default branch)
  - GitHub App Installation ID (replaces deprecated PATs)
  - Indexing lifecycle state (pending → indexing → indexed | failed)
  - source_version counter for snapshot staleness protection

One-to-one relationship with Tenant: each tenant may have at most one
connected GitHub repository at a time.

Architecture reference: NeuralOps Technical Documentation — Sections 17
(Code Indexing), 20 (Security).
"""

from __future__ import annotations

import uuid

from django.db import models
from tenants.models import Tenant


class GitHubIntegration(models.Model):
    """
    Per-tenant GitHub repository integration.

    Uses GitHub App authentication via github_installation_id.
    """

    # ── Indexing status choices ────────────────────────────────────────────────
    INDEXING_STATUS_PENDING = "pending"
    INDEXING_STATUS_INDEXING = "indexing"
    INDEXING_STATUS_INDEXED = "indexed"
    INDEXING_STATUS_FAILED = "failed"

    INDEXING_STATUS_CHOICES = [
        (INDEXING_STATUS_PENDING, "Pending"),
        (INDEXING_STATUS_INDEXING, "Indexing"),
        (INDEXING_STATUS_INDEXED, "Indexed"),
        (INDEXING_STATUS_FAILED, "Failed"),
    ]

    # ── Primary key ───────────────────────────────────────────────────────────
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    # ── Tenant relationship ───────────────────────────────────────────────────
    tenant = models.OneToOneField(
        Tenant,
        on_delete=models.CASCADE,
        related_name="github_integration",
        help_text="Each tenant may have at most one GitHub integration.",
    )

    # ── Repository metadata ───────────────────────────────────────────────────
    repo_url = models.URLField(
        max_length=500,
        help_text="Full HTTPS clone URL, e.g. https://github.com/my-org/my-repo",
    )
    repo_owner = models.CharField(
        max_length=255,
        help_text="GitHub organisation or user name that owns the repository.",
    )
    repo_name = models.CharField(
        max_length=255,
        help_text="Repository name (without the owner prefix).",
    )
    default_branch = models.CharField(
        max_length=255,
        default="main",
        help_text="Branch that is indexed and monitored for push events.",
    )

    # ── Credentials (GitHub App) ───────────────────────────────────────
    github_installation_id = models.CharField(
        max_length=255,
        default="",
        help_text="The GitHub App installation ID used to authenticate and fetch tokens.",
    )
    webhook_id = models.CharField(
        max_length=255,
        null=True,
        blank=True,
        help_text="GitHub webhook ID returned after webhook registration. Null until registered.",
    )

    # ── Indexing lifecycle ────────────────────────────────────────────────────
    indexing_status = models.CharField(
        max_length=20,
        choices=INDEXING_STATUS_CHOICES,
        default=INDEXING_STATUS_PENDING,
        db_index=True,
        help_text="Current state of the AST code-indexing pipeline for this repository.",
    )
    last_indexed_commit = models.CharField(
        max_length=40,
        null=True,
        blank=True,
        help_text="SHA of the last successfully indexed commit. Null until first index.",
    )

    # ── Snapshot versioning ───────────────────────────────────────────────────
    source_version = models.BigIntegerField(
        default=1,
        help_text=(
            "Monotonically-increasing counter. Incremented on every save. "
            "FastAPI's snapshot consumer discards events with a "
            "source_version <= the current snapshot row's version."
        ),
    )

    # ── Timestamps ────────────────────────────────────────────────────────────
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "github_integrations"
        indexes = [
            models.Index(fields=["tenant"]),
            models.Index(fields=["indexing_status"]),
        ]

    # ── Atomic source_version increment on update ─────────────────────────────
    def save(self, *args, **kwargs) -> None:
        """
        Override save() to atomically increment source_version on every update.

        Uses a database-side F() expression — identical to the pattern in
        AlertRule and Playbook models — to prevent lost-update races when
        multiple workers update the same row concurrently.

        On INSERT (self._state.adding is True) the default value (1) is used.
        """
        if not self._state.adding:
            # Atomic increment at the DB layer
            GitHubIntegration.objects.filter(pk=self.pk).update(
                source_version=models.F("source_version") + 1
            )
            # Reload the actual new value so the in-memory instance is accurate
            # before super().save() runs (needed for outbox payload building).
            self.source_version = (
                GitHubIntegration.objects.filter(pk=self.pk)
                .values_list("source_version", flat=True)
                .get()
            )

        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return (
            f"GitHubIntegration("
            f"tenant={self.tenant_id}, "
            f"repo={self.repo_owner}/{self.repo_name}, "
            f"status={self.indexing_status})"
        )
