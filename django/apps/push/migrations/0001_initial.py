from django.db import migrations, models
import uuid


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        # ── device_tokens ────────────────────────────────────────────────
        migrations.CreateModel(
            name="DeviceToken",
            fields=[
                (
                    "token_id",
                    models.UUIDField(
                        primary_key=True, default=uuid.uuid4, editable=False
                    ),
                ),
                ("tenant_id", models.UUIDField(db_index=True)),
                ("user_id", models.UUIDField()),
                ("platform", models.CharField(max_length=16)),
                ("provider", models.CharField(max_length=8)),
                ("device_token", models.TextField()),
                ("device_id", models.TextField()),
                ("is_active", models.BooleanField(default=True)),
                ("registered_at", models.DateTimeField(auto_now_add=True)),
                ("last_seen_at", models.DateTimeField(auto_now=True)),
                ("invalidated_at", models.DateTimeField(null=True, blank=True)),
            ],
            options={"db_table": "device_tokens"},
        ),
        migrations.AlterUniqueTogether(
            name="DeviceToken",
            unique_together={("user_id", "device_id")},
        ),
        # ── push_delivery_log ─────────────────────────────────────────────
        migrations.CreateModel(
            name="PushDeliveryLog",
            fields=[
                (
                    "log_id",
                    models.UUIDField(
                        primary_key=True, default=uuid.uuid4, editable=False
                    ),
                ),
                ("tenant_id", models.UUIDField()),
                ("user_id", models.UUIDField()),
                (
                    "token",
                    models.ForeignKey(
                        "push.DeviceToken",
                        on_delete=models.SET_NULL,
                        null=True,
                        db_column="token_id",
                    ),
                ),
                ("source_event_id", models.UUIDField()),
                ("incident_id", models.UUIDField()),
                ("status", models.CharField(max_length=16)),
                ("provider_message_id", models.TextField(null=True, blank=True)),
                ("failure_reason", models.TextField(null=True, blank=True)),
                ("sent_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={"db_table": "push_delivery_log"},
        ),
        migrations.AlterUniqueTogether(
            name="PushDeliveryLog",
            unique_together={("source_event_id", "token")},
        ),
        migrations.AddIndex(
            model_name="PushDeliveryLog",
            index=models.Index(fields=["incident_id"], name="idx_push_log_incident"),
        ),
        # ── Row-Level Security (same pattern as every DB-1 table) ─────────
        migrations.RunSQL(
            sql="""
                ALTER TABLE device_tokens ENABLE ROW LEVEL SECURITY;
                CREATE POLICY device_tokens_tenant_rls ON device_tokens
                    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);

                ALTER TABLE push_delivery_log ENABLE ROW LEVEL SECURITY;
                CREATE POLICY push_log_tenant_rls ON push_delivery_log
                    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);
            """,
            reverse_sql="""
                DROP POLICY IF EXISTS device_tokens_tenant_rls ON device_tokens;
                ALTER TABLE device_tokens DISABLE ROW LEVEL SECURITY;
                DROP POLICY IF EXISTS push_log_tenant_rls ON push_delivery_log;
                ALTER TABLE push_delivery_log DISABLE ROW LEVEL SECURITY;
            """,
        ),
    ]
