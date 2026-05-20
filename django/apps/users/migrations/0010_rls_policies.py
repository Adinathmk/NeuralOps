from django.db import migrations

class Migration(migrations.Migration):

    dependencies = [
        ('users', '0009_auditlog_user_alter_auditlog_action'),
    ]

    operations = [
        migrations.RunSQL(
            sql='''
                -- API Keys
                ALTER TABLE api_keys ENABLE ROW LEVEL SECURITY;
                ALTER TABLE api_keys FORCE ROW LEVEL SECURITY;
                CREATE POLICY tenant_isolation_policy ON api_keys
                AS PERMISSIVE FOR ALL USING (
                    current_setting('app.bypass_rls', true) = 'on' OR tenant_id::text = current_setting('app.current_tenant', true)
                ) WITH CHECK (
                    current_setting('app.bypass_rls', true) = 'on' OR tenant_id::text = current_setting('app.current_tenant', true)
                );
    
                -- Audit Logs
                ALTER TABLE audit_logs ENABLE ROW LEVEL SECURITY;
                ALTER TABLE audit_logs FORCE ROW LEVEL SECURITY;
                CREATE POLICY tenant_isolation_policy ON audit_logs
                AS PERMISSIVE FOR ALL USING (
                    current_setting('app.bypass_rls', true) = 'on' OR tenant_id::text = current_setting('app.current_tenant', true)
                ) WITH CHECK (
                    current_setting('app.bypass_rls', true) = 'on' OR tenant_id::text = current_setting('app.current_tenant', true)
                );

                -- User Sessions
                ALTER TABLE user_sessions ENABLE ROW LEVEL SECURITY;
                ALTER TABLE user_sessions FORCE ROW LEVEL SECURITY;
                CREATE POLICY tenant_isolation_policy ON user_sessions
                AS PERMISSIVE FOR ALL USING (
                    current_setting('app.bypass_rls', true) = 'on' OR tenant_id::text = current_setting('app.current_tenant', true)
                ) WITH CHECK (
                    current_setting('app.bypass_rls', true) = 'on' OR tenant_id::text = current_setting('app.current_tenant', true)
                );

                -- Users
                ALTER TABLE users ENABLE ROW LEVEL SECURITY;
                ALTER TABLE users FORCE ROW LEVEL SECURITY;
                CREATE POLICY tenant_isolation_policy ON users
                AS PERMISSIVE FOR ALL USING (
                    current_setting('app.bypass_rls', true) = 'on' OR tenant_id::text = current_setting('app.current_tenant', true)
                ) WITH CHECK (
                    current_setting('app.bypass_rls', true) = 'on' OR tenant_id::text = current_setting('app.current_tenant', true)
                );

                -- User Invitations
                ALTER TABLE user_invitations ENABLE ROW LEVEL SECURITY;
                ALTER TABLE user_invitations FORCE ROW LEVEL SECURITY;
                CREATE POLICY tenant_isolation_policy ON user_invitations
                AS PERMISSIVE FOR ALL USING (
                    current_setting('app.bypass_rls', true) = 'on' OR tenant_id::text = current_setting('app.current_tenant', true)
                ) WITH CHECK (
                    current_setting('app.bypass_rls', true) = 'on' OR tenant_id::text = current_setting('app.current_tenant', true)
                );
            ''',
            reverse_sql='''
                DROP POLICY IF EXISTS tenant_isolation_policy ON api_keys;
                ALTER TABLE api_keys DISABLE ROW LEVEL SECURITY;

                DROP POLICY IF EXISTS tenant_isolation_policy ON audit_logs;
                ALTER TABLE audit_logs DISABLE ROW LEVEL SECURITY;

                DROP POLICY IF EXISTS tenant_isolation_policy ON user_sessions;
                ALTER TABLE user_sessions DISABLE ROW LEVEL SECURITY;

                DROP POLICY IF EXISTS tenant_isolation_policy ON users;
                ALTER TABLE users DISABLE ROW LEVEL SECURITY;

                DROP POLICY IF EXISTS tenant_isolation_policy ON user_invitations;
                ALTER TABLE user_invitations DISABLE ROW LEVEL SECURITY;
            '''
        )
    ]
