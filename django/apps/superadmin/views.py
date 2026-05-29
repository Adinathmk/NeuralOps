# GET  /api/admin/stats
# GET  /api/admin/tenants
# GET  /api/admin/tenants/{tenant_id}
# PATCH /api/admin/tenants/{tenant_id}/suspend    ← writes Redis + outbox
# PATCH /api/admin/tenants/{tenant_id}/reinstate  ← deletes Redis key + outbox
