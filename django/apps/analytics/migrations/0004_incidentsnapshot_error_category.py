from django.db import migrations, models


class Migration(migrations.Migration):
    """
    Add error_category field to IncidentSnapshot.

    Dependency: 0003_alter_incidentsnapshot_options_and_more
    (confirmed as current head of analytics migrations).

    The field is NOT NULL with default='unknown' so this is safe to apply
    before any FastAPI code starts populating it — existing rows read back
    'unknown'. No data backfill is needed.

    Index inc_sn_category_idx enables efficient per-tenant category
    filtering in the super-admin dashboard and analytics queries.
    """

    dependencies = [
        ("analytics", "0003_alter_incidentsnapshot_options_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="incidentsnapshot",
            name="error_category",
            field=models.CharField(
                max_length=32,
                default="unknown",
                db_index=True,
                help_text=(
                    "code_bug | database | infra_config | "
                    "external_dependency | security | unknown."
                ),
            ),
        ),
        migrations.AddIndex(
            model_name="incidentsnapshot",
            index=models.Index(
                fields=["tenant", "error_category"],
                name="inc_sn_category_idx",
            ),
        ),
    ]
