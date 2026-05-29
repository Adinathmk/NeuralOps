from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("tenants", "0001_initial"),
    ]

    operations = [
        migrations.RunSQL(
            sql="""
                -- Tenants
                ALTER TABLE tenants ENABLE ROW LEVEL SECURITY;
                ALTER TABLE tenants FORCE ROW LEVEL SECURITY;
                CREATE POLICY tenant_isolation_policy ON tenants
                AS PERMISSIVE FOR ALL USING (
                    current_setting('app.bypass_rls', true) = 'on' OR id::text = current_setting('app.current_tenant', true)
                ) WITH CHECK (
                    current_setting('app.bypass_rls', true) = 'on' OR id::text = current_setting('app.current_tenant', true)
                );

                -- Tenant Configurations
                ALTER TABLE tenant_configurations ENABLE ROW LEVEL SECURITY;
                ALTER TABLE tenant_configurations FORCE ROW LEVEL SECURITY;
                CREATE POLICY tenant_isolation_policy ON tenant_configurations
                AS PERMISSIVE FOR ALL USING (
                    current_setting('app.bypass_rls', true) = 'on' OR tenant_id::text = current_setting('app.current_tenant', true)
                ) WITH CHECK (
                    current_setting('app.bypass_rls', true) = 'on' OR tenant_id::text = current_setting('app.current_tenant', true)
                );
            """,
            reverse_sql="""
                DROP POLICY IF EXISTS tenant_isolation_policy ON tenants;
                ALTER TABLE tenants DISABLE ROW LEVEL SECURITY;

                DROP POLICY IF EXISTS tenant_isolation_policy ON tenant_configurations;
                ALTER TABLE tenant_configurations DISABLE ROW LEVEL SECURITY;
            """,
        )
    ]
