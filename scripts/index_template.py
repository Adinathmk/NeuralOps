"""
NeuralOps — Index Template & Tenant Routing Strategy

Index naming strategy matters for multi-tenancy:
- Standard tenants: shared index  →  neuralops-logs-000001, -000002, ...
- Enterprise tenants: dedicated index →  neuralops-logs-{tenant_id}-000001, ...

Why the split:
- Shared index: simpler operations, lower overhead, fine for standard tenants
  because tenant_id is always the first filter on every query.
- Dedicated index: enterprise isolation, custom ILM, custom shard sizing,
  ability to delete ALL tenant data by deleting the index (GDPR compliance).

The API surface is identical — the ingest service decides which index alias
to write to based on tenant_snapshots.plan_tier. The search service queries
the right alias. No change in the FastAPI or Django routing logic.
"""

from typing import Optional
from index_mapping import LOG_EVENT_INDEX_MAPPING


# ── SHARED INDEX TEMPLATE (Standard + Professional tenants) ────────────────

SHARED_INDEX_TEMPLATE = {
    "index_patterns": ["neuralops-logs-*"],
    # Exclude dedicated enterprise indices from this template
    "composed_of": [],
    "priority": 100,
    "template": {
        "settings": LOG_EVENT_INDEX_MAPPING["settings"],
        "mappings": LOG_EVENT_INDEX_MAPPING["mappings"],
    },
    "_meta": {
        "description": "NeuralOps log metadata — shared tenant index"
    },
}


# ── ENTERPRISE INDEX TEMPLATE (per-tenant) ─────────────────────────────────

def build_enterprise_index_template(tenant_id: str, retention_days: int = 365) -> dict:
    """
    Build a dedicated index template for an enterprise tenant.
    Called once at tenant provisioning time.

    Enterprise tenants get:
    - Dedicated index alias: neuralops-logs-{tenant_id}
    - Custom ILM policy with their configured retention
    - Dedicated shard allocation (can pin to specific nodes if needed)
    - SSE-KMS equivalent: Elasticsearch keystore field-level encryption
      is NOT used here — raw content never enters ES, so field encryption
      on metadata fields is unnecessary overhead.
    """
    import copy
    template = copy.deepcopy(SHARED_INDEX_TEMPLATE)

    # Override the index pattern to match only this tenant's indices
    template["index_patterns"] = [f"neuralops-logs-{tenant_id}-*"]
    template["priority"] = 200  # Higher priority than shared template

    # Assign the tenant-specific ILM policy
    template["template"]["settings"]["index.lifecycle.name"] = (
        f"neuralops-logs-ilm-{tenant_id}"
    )
    template["template"]["settings"]["index.lifecycle.rollover_alias"] = (
        f"neuralops-logs-{tenant_id}"
    )

    # Enterprise tenants get 2 primary shards for higher write throughput
    # (they generate more errors at scale)
    template["template"]["settings"]["number_of_shards"] = 2

    template["_meta"]["description"] = (
        f"NeuralOps log metadata — enterprise tenant {tenant_id}"
    )
    return template



