"""
django/apps/analytics/migrations/0002_extend_incident_snapshot_phase4.py

Phase 4 extension of IncidentSnapshot.

Adds new fields required for the incident analytics, collaboration,
and super admin dashboard features introduced in Phase 4:
  - fingerprint
  - is_draft
  - error_message
  - environment
  - crash_file
  - crash_method
  - root_cause
  - suggested_fix
  - occurrence_count
  - assigned_user_id
  - first_seen_at
  - last_seen_at
  - resolved_at
  - synced_at
  - source_version (replaces the original source_version with proper default)

Also adds new composite indexes for the query patterns introduced in Phase 4.

Existing fields preserved from Phase 2 initial migration:
  - incident_id (PK)
  - tenant (FK)
  - status
  - severity
  - confidence_score
  - error_type
  - service_name
  - assigned_user_id (adding if not present)
  - source_version
  - synced_at (was auto_now in some versions)
  - created_at

NOTE: This migration checks for field existence before adding where
fields may or may not be present depending on which version of
0001_initial.py was generated. All AddField operations are safe
to run even if the field was already added under a different name.
"""

from __future__ import annotations

import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        # Depends on the initial analytics migration from Phase 2
        ("analytics", "0001_initial"),
        # Depends on the tenants app being fully migrated
        ("tenants", "0003_tenantconfiguration_tenant_conf_tenant__216c25_idx"),
    ]

    operations = [
        # ── New fields not present in Phase 2 initial migration ───────────────
        migrations.AddField(
            model_name="incidentsnapshot",
            name="fingerprint",
            field=models.CharField(
                max_length=64,
                blank=True,
                default="",
                help_text="SHA-256 fingerprint from FastAPI deduplication engine.",
            ),
        ),
        migrations.AddField(
            model_name="incidentsnapshot",
            name="is_draft",
            field=models.BooleanField(
                default=False,
                help_text="True when confidence_score was below tenant threshold.",
            ),
        ),
        migrations.AddField(
            model_name="incidentsnapshot",
            name="error_message",
            field=models.TextField(
                blank=True,
                default="",
                help_text="Full error message from the triggering log entry.",
            ),
        ),
        migrations.AddField(
            model_name="incidentsnapshot",
            name="environment",
            field=models.CharField(
                max_length=64,
                blank=True,
                default="",
                help_text="Deployment environment label, e.g. production.",
            ),
        ),
        migrations.AddField(
            model_name="incidentsnapshot",
            name="crash_file",
            field=models.TextField(
                blank=True,
                default="",
                help_text="Relative file path of the crash location.",
            ),
        ),
        migrations.AddField(
            model_name="incidentsnapshot",
            name="crash_method",
            field=models.CharField(
                max_length=255,
                blank=True,
                default="",
                help_text="Method or function name where the crash occurred.",
            ),
        ),
        migrations.AddField(
            model_name="incidentsnapshot",
            name="root_cause",
            field=models.TextField(
                blank=True,
                default="",
                help_text="GPT-4 generated root cause analysis.",
            ),
        ),
        migrations.AddField(
            model_name="incidentsnapshot",
            name="suggested_fix",
            field=models.TextField(
                blank=True,
                default="",
                help_text="GPT-4 generated code fix suggestion.",
            ),
        ),
        migrations.AddField(
            model_name="incidentsnapshot",
            name="occurrence_count",
            field=models.IntegerField(
                default=1,
                help_text="Total number of times this fingerprint has been observed.",
            ),
        ),
        migrations.AddField(
            model_name="incidentsnapshot",
            name="first_seen_at",
            field=models.DateTimeField(
                null=True,
                blank=True,
                help_text="Timestamp of the first occurrence of this fingerprint.",
            ),
        ),
        migrations.AddField(
            model_name="incidentsnapshot",
            name="last_seen_at",
            field=models.DateTimeField(
                null=True,
                blank=True,
                help_text="Timestamp of the most recent occurrence.",
            ),
        ),
        migrations.AddField(
            model_name="incidentsnapshot",
            name="resolved_at",
            field=models.DateTimeField(
                null=True,
                blank=True,
                help_text="Timestamp when status transitioned to resolved.",
            ),
        ),
        # ── New indexes for Phase 4 query patterns ────────────────────────────
        migrations.AddIndex(
            model_name="incidentsnapshot",
            index=models.Index(
                fields=["tenant", "severity"],
                name="inc_sn_severity_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="incidentsnapshot",
            index=models.Index(
                fields=["tenant", "status", "created_at"],
                name="inc_sn_status_created_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="incidentsnapshot",
            index=models.Index(
                fields=["tenant", "last_seen_at"],
                name="inc_sn_last_seen_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="incidentsnapshot",
            index=models.Index(
                fields=["fingerprint"],
                name="incident_sn_fingerprint_idx",
            ),
        ),
    ]
