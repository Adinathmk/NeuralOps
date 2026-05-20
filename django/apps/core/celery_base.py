import logging
from celery import Task
from django.db import connection

logger = logging.getLogger(__name__)

class TenantAwareTask(Task):
    """
    A custom Celery Task base class that ensures PostgreSQL Row-Level Security (RLS)
    is properly configured for background tasks.
    
    Usage:
        @shared_task(bind=True, base=TenantAwareTask)
        def my_background_task(self, tenant_id, *args, **kwargs):
            # Database context is already set to the tenant!
            pass
    """
    abstract = True

    def __call__(self, *args, **kwargs):
        # We need to find the tenant_id. 
        # Usually, the best practice is to pass it as an explicit keyword argument.
        tenant_id = kwargs.get('tenant_id', None)
        is_superadmin = kwargs.get('is_superadmin', False)

        try:
            # Set the Postgres connection context for RLS
            with connection.cursor() as cursor:
                if is_superadmin:
                    cursor.execute("SELECT set_config('app.bypass_rls', 'on', false)")
                    cursor.execute("SELECT set_config('app.current_tenant', '', false)")
                    logger.debug("TenantAwareTask running as Platform Admin")
                elif tenant_id:
                    cursor.execute("SELECT set_config('app.bypass_rls', 'off', false)")
                    cursor.execute("SELECT set_config('app.current_tenant', %s, false)", [str(tenant_id)])
                    logger.debug(f"TenantAwareTask running for tenant: {tenant_id}")
                else:
                    # Fail-closed for tasks that don't pass a context
                    cursor.execute("SELECT set_config('app.bypass_rls', 'off', false)")
                    cursor.execute("SELECT set_config('app.current_tenant', '', false)")
                    logger.warning("TenantAwareTask running WITHOUT tenant context (Fail-Closed)")

            # Execute the actual task logic
            return super().__call__(*args, **kwargs)

        finally:
            # Clean up the DB connection before returning it to the pool
            if connection.connection is not None:
                try:
                    with connection.cursor() as cursor:
                        cursor.execute("SELECT set_config('app.bypass_rls', 'off', false)")
                        cursor.execute("SELECT set_config('app.current_tenant', '', false)")
                except Exception:
                    pass
