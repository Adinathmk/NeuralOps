from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("playbooks", "0001_initial"),
    ]

    operations = [
        migrations.RunSQL(
            sql="""
                ALTER TABLE playbooks ENABLE ROW LEVEL SECURITY;
                ALTER TABLE playbooks FORCE ROW LEVEL SECURITY;

                CREATE POLICY tenant_isolation_policy ON playbooks
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
                DROP POLICY IF EXISTS tenant_isolation_policy ON playbooks;
                ALTER TABLE playbooks DISABLE ROW LEVEL SECURITY;
            """,
        )
    ]
