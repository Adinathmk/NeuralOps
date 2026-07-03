"""


Django DB-1 read model for incident data owned by FastAPI (DB-2).

IncidentSnapshot is a projection of FastAPI's incidents table, populated
by the consume_incidents Kafka consumer (Phase 4, Part 5). Django uses
this table for analytics queries, the super admin dashboard, and as the
anchor for collaboration threads (Phase 5).

This model is NEVER written by Django ORM code directly. All writes go
through the consume_incidents management command which processes Kafka
events published by FastAPI via Debezium.

Row-Level Security is enforced via the existing RLS migration for this
table. The TenantMiddleware sets app.current_tenant on each connection.
"""

from __future__ import annotations

import uuid

from django.db import models


class IncidentSnapshot(models.Model):
    """
    Read-only projection of FastAPI's incidents table in DB-2.

    All fields are nullable (except incident_id and tenant) to allow
    the consume_incidents consumer to handle partial event payloads
    gracefully without crashing. Missing fields default to empty
    strings or None and are backfilled when the next event arrives.
    """

    # ── Primary key: matches incidents.id in DB-2 ────────────────────────────
    incident_id = models.UUIDField(
        primary_key=True,
        editable=False,
        help_text=(
            "UUID matching incidents.id in DB-2. "
            "Set by the Kafka consumer; never auto-generated."
        ),
    )

    # ── Tenant relationship ───────────────────────────────────────────────────
    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.CASCADE,
        related_name="incident_snapshots",
        help_text="Owning tenant.",
    )

    # ── Error identity ────────────────────────────────────────────────────────
    fingerprint = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text="SHA-256 fingerprint from FastAPI deduplication engine.",
    )

    # ── Lifecycle ─────────────────────────────────────────────────────────────
    status = models.CharField(
        max_length=32,
        default="open",
        db_index=True,
        help_text="open | investigating | resolved | draft | duplicate.",
    )
    is_draft = models.BooleanField(
        default=False,
        help_text="True when confidence_score was below tenant threshold.",
    )

    # ── Classification ────────────────────────────────────────────────────────
    severity = models.CharField(
        max_length=32,
        default="unknown",
        help_text="critical | high | medium | low | info | unknown.",
    )
    error_category = models.CharField(
        max_length=32,
        default="unknown",
        db_index=True,
        help_text=(
            "code_bug | database | infra_config | external_dependency | "
            "security | unknown."
        ),
    )
    confidence_score = models.FloatField(
        null=True,
        blank=True,
        help_text="Agent confidence score in range [0.0, 1.0].",
    )

    # ── Error metadata ────────────────────────────────────────────────────────
    error_type = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Exception or error class name.",
    )
    error_message = models.TextField(
        blank=True,
        default="",
        help_text="Full error message from the triggering log entry.",
    )
    service_name = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Name of the originating service.",
    )
    environment = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text="Deployment environment label, e.g. production.",
    )

    # ── Crash location ────────────────────────────────────────────────────────
    crash_file = models.TextField(
        blank=True,
        default="",
        help_text="Relative file path of the crash location.",
    )
    crash_method = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Method or function name where the crash occurred.",
    )

    # ── AI analysis output ────────────────────────────────────────────────────
    root_cause = models.TextField(
        blank=True,
        default="",
        help_text="GPT-4 generated root cause analysis.",
    )
    suggested_fix = models.TextField(
        blank=True,
        default="",
        help_text="GPT-4 generated code fix suggestion.",
    )

    # ── Deduplication counters ────────────────────────────────────────────────
    occurrence_count = models.IntegerField(
        default=1,
        help_text="Total number of times this fingerprint has been observed.",
    )

    # ── Assignment ────────────────────────────────────────────────────────────
    assigned_user_id = models.UUIDField(
        null=True,
        blank=True,
        help_text="UUID of the assigned engineer (from DB-2).",
    )

    # ── Snapshot versioning ───────────────────────────────────────────────────
    source_version = models.BigIntegerField(
        default=0,
        help_text=(
            "Monotonically increasing version counter from the source incident. "
            "Used to reject stale Kafka redeliveries."
        ),
    )

    # ── Timestamps (sourced from FastAPI DB-2) ────────────────────────────────
    first_seen_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Timestamp of the first occurrence of this fingerprint.",
    )
    last_seen_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Timestamp of the most recent occurrence.",
    )
    resolved_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Timestamp when status transitioned to resolved.",
    )
    created_at = models.DateTimeField(
        help_text="Incident creation timestamp from FastAPI DB-2.",
    )

    # ── Snapshot housekeeping ─────────────────────────────────────────────────
    synced_at = models.DateTimeField(
        auto_now=True,
        help_text="Timestamp of the last successful snapshot upsert by the consumer.",
    )

    class Meta:
        db_table = "incident_snapshots"
        ordering = ["-created_at"]
        indexes = [
            # Existing indexes (preserve from Phase 2)
            models.Index(
                fields=["tenant", "status"],
                name="incident_sn_tenant_status_idx",
            ),
            models.Index(
                fields=["tenant", "created_at"],
                name="incident_sn_tenant_created_idx",
            ),
            # New indexes added in Phase 4
            models.Index(
                fields=["tenant", "severity"],
                name="inc_sn_severity_idx",
            ),
            models.Index(
                fields=["tenant", "error_category"],
                name="inc_sn_category_idx",
            ),
            models.Index(
                fields=["tenant", "status", "created_at"],
                name="inc_sn_status_created_idx",
            ),
            models.Index(
                fields=["tenant", "last_seen_at"],
                name="inc_sn_last_seen_idx",
            ),
            models.Index(
                fields=["fingerprint"],
                name="incident_sn_fingerprint_idx",
            ),
        ]

    def __str__(self) -> str:
        return (
            f"IncidentSnapshot("
            f"id={self.incident_id} "
            f"tenant={self.tenant_id} "
            f"status={self.status} "
            f"severity={self.severity})"
        )
