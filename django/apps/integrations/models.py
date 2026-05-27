"""
django/apps/integrations/models.py

GitHubIntegration model — Django-owned (DB-1).

Stores the GitHub repository connection details for a tenant:
  - Repository metadata (URL, owner, name, default branch)
  - Encrypted credentials (PAT, webhook secret) — never stored in plaintext
  - Indexing lifecycle state (pending → indexing → indexed | failed)
  - source_version counter for snapshot staleness protection

One-to-one relationship with Tenant: each tenant may have at most one
connected GitHub repository at a time.

The encrypted_pat field is decrypted at runtime by FastAPI (Service 2)
when it needs to make GitHub API calls. The decrypted PAT is NEVER
persisted anywhere other than in-memory during an indexing task.

Architecture reference: NeuralOps Technical Documentation — Sections 17
(Code Indexing), 20 (Security — GitHub PATs stored encrypted).
"""

from __future__ import annotations

import uuid

from django.db import models

from tenants.models import Tenant


class GitHubIntegration(models.Model):
    """
    Per-tenant GitHub repository integration.

    Credentials (PAT and webhook secret) are stored encrypted using
    Fernet symmetric encryption. Use integrations.encryption helpers
    to encrypt before save and decrypt before use.
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

    # ── Credentials (encrypted at rest) ───────────────────────────────────────
    encrypted_pat = models.TextField(
        help_text=(
            "Fernet-encrypted GitHub Personal Access Token. "
            "Use integrations.encryption.decrypt_secret() to read at runtime."
        ),
    )
    webhook_id = models.CharField(
        max_length=255,
        null=True,
        blank=True,
        help_text="GitHub webhook ID returned after webhook registration. Null until registered.",
    )
    webhook_secret = models.TextField(
        help_text=(
            "Fernet-encrypted webhook secret used to validate incoming push events. "
            "Use integrations.encryption.decrypt_secret() to read at runtime."
        ),
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