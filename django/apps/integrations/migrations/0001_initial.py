"""
django/apps/integrations/migrations/0001_initial.py

Creates the github_integrations table and enables Row-Level Security.
"""
from __future__ import annotations

import uuid
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ("tenants", "0003_tenantconfiguration_tenant_conf_tenant__216c25_idx"),
    ]

    operations = [
        # ── Create table ───────────────────────────────────────────────────────
        migrations.CreateModel(
            name="GitHubIntegration",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        primary_key=True,
                        default=uuid.uuid4,
                        editable=False,
                        serialize=False,
                    ),
                ),
                (
                    "tenant",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="github_integration",
                        to="tenants.tenant",
                        help_text="Each tenant may have at most one GitHub integration.",
                    ),
                ),
                (
                    "repo_url",
                    models.URLField(
                        max_length=500,
                        help_text="Full HTTPS clone URL.",
                    ),
                ),
                (
                    "repo_owner",
                    models.CharField(
                        max_length=255,
                        help_text="GitHub organisation or user name.",
                    ),
                ),
                (
                    "repo_name",
                    models.CharField(
                        max_length=255,
                        help_text="Repository name.",
                    ),
                ),
                (
                    "default_branch",
                    models.CharField(
                        max_length=255,
                        default="main",
                    ),
                ),
                (
                    "encrypted_pat",
                    models.TextField(
                        help_text="Fernet-encrypted GitHub PAT.",
                    ),
                ),
                (
                    "webhook_id",
                    models.CharField(
                        max_length=255,
                        null=True,
                        blank=True,
                        help_text="GitHub webhook ID. Null until registered.",
                    ),
                ),
                (
                    "webhook_secret",
                    models.TextField(
                        help_text="Fernet-encrypted webhook signing secret.",
                    ),
                ),
                (
                    "indexing_status",
                    models.CharField(
                        max_length=20,
                        choices=[
                            ("pending", "Pending"),
                            ("indexing", "Indexing"),
                            ("indexed", "Indexed"),
                            ("failed", "Failed"),
                        ],
                        default="pending",
                        db_index=True,
                    ),
                ),
                (
                    "last_indexed_commit",
                    models.CharField(
                        max_length=40,
                        null=True,
                        blank=True,
                    ),
                ),
                (
                    "source_version",
                    models.BigIntegerField(
                        default=1,
                        help_text="Auto-incremented on every save; used by snapshot consumers.",
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "db_table": "github_integrations",
                "indexes": [
                    models.Index(fields=["tenant"], name="gh_integ_tenant_idx"),
                    models.Index(fields=["indexing_status"], name="gh_integ_status_idx"),
                ],
            },
        ),

        # ── Enable Row-Level Security ──────────────────────────────────────────
        migrations.RunSQL(
            sql="""
                ALTER TABLE github_integrations ENABLE ROW LEVEL SECURITY;
                ALTER TABLE github_integrations FORCE ROW LEVEL SECURITY;

                CREATE POLICY tenant_isolation_policy ON github_integrations
                AS PERMISSIVE FOR ALL
                USING (
                    current_setting('app.bypass_rls', true) = 'on'
                    OR tenant_id::text = current_setting('app.current_tenant', true)
                )
                WITH CHECK (
                    current_setting('app.bypass_rls', true) = 'on'
                    OR tenant_id::text = current_setting('app.current_tenant', true)
                );
            """,
            reverse_sql="""
                DROP POLICY IF EXISTS tenant_isolation_policy ON github_integrations;
                ALTER TABLE github_integrations DISABLE ROW LEVEL SECURITY;
            """,
        ),
    ]