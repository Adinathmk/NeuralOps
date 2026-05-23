from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("alerts", "0001_initial"),
    ]

    operations = [
        migrations.RunSQL(
            sql="""
                ALTER TABLE alert_rules ENABLE ROW LEVEL SECURITY;
                ALTER TABLE alert_rules FORCE ROW LEVEL SECURITY;

                CREATE POLICY tenant_isolation_policy ON alert_rules
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
                DROP POLICY IF EXISTS tenant_isolation_policy ON alert_rules;
                ALTER TABLE alert_rules DISABLE ROW LEVEL SECURITY;
            """,
        )
    ]