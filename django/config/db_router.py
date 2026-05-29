class AnalyticsReadRouter:
    """
    Route read-heavy analytics/reporting queries to the replica.
    All writes and everything else go to default (primary).

    Usage in views/services:
        MyModel.objects.using('replica').filter(...)

    Automatic routing is opt-in via the `using('replica')` queryset method.
    Do NOT use auto-routing for all reads — only analytics/reporting paths.
    """

    REPLICA_APPS = {"analytics"}  # expand as analytics app grows

    def db_for_read(self, model, **hints):
        if model._meta.app_label in self.REPLICA_APPS:
            return "replica"
        return "default"

    def db_for_write(self, model, **hints):
        return "default"

    def allow_relation(self, obj1, obj2, **hints):
        return True

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        # Only run migrations on the primary
        return db == "default"
