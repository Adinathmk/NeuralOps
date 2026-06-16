"""
NeuralOps — Alias Routing Logic

The API surface is identical — the ingest service decides which index alias
to write to based on tenant_snapshots.plan_tier. The search service queries
the right alias. No change in the FastAPI or Django routing logic.
"""


def get_write_alias(tenant_id: str, plan_tier: str) -> str:
    """
    Returns the Elasticsearch alias to write to for a given tenant.
    Called by the ingest service on every log event.

    Standard/Professional → shared alias (tenant_id filter handles isolation)
    Enterprise            → dedicated alias (full index isolation)
    """
    if plan_tier == "enterprise":
        return f"neuralops-logs-{tenant_id}"
    return "neuralops-logs"


def get_search_index(tenant_id: str, plan_tier: str) -> str:
    """
    Returns the index pattern to search for a given tenant.
    Called by the log search endpoint.

    For shared tenants: search the shared alias but ALWAYS include
    tenant_id as a required filter — never omit it.
    For enterprise: search the dedicated alias.
    """
    if plan_tier == "enterprise":
        return f"neuralops-logs-{tenant_id}"
    return "neuralops-logs"
