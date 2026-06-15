"""
NeuralOps — ILM (Index Lifecycle Management) Policy
Controls how log metadata indices age through hot → warm → cold → delete.

Why each tier exists:
- Hot:  Recent logs. Engineers search these most. Fast NVMe SSD nodes.
        Rollover keeps individual index size manageable (avoid huge segments).
- Warm: Older logs. Still searchable but less frequently. Can use HDD nodes.
        Read-only — no more writes. Force-merge to 1 segment: maximises
        compression, minimises memory overhead for segment metadata.
- Cold: Old logs. Rarely searched. Cheapest storage tier.
        Searchable but slow — acceptable for "historical incident review".
- Delete: Retention boundary. Data gone. S3 still has the raw content.
"""

ILM_POLICY = {
    "policy": {
        "phases": {

            # ── HOT PHASE ──────────────────────────────────────────────────
            # New indices start here. Active writes + high-frequency reads.
            "hot": {
                "min_age": "0ms",
                "actions": {
                    "rollover": {
                        # Roll to a new backing index when EITHER condition hits.
                        # This keeps index size predictable and prevents one huge index.
                        "max_age": "7d",        # Roll after 7 days regardless of size
                        "max_primary_shard_size": "10gb",  # Roll at 10GB per shard
                        # At early scale you'll hit 7d before 10GB.
                        # At Sentry scale you may hit 10GB in hours — tune accordingly.
                    },
                    "set_priority": {
                        "priority": 100  # Highest priority for node recovery
                    },
                },
            },

            # ── WARM PHASE ─────────────────────────────────────────────────
            # Index stops receiving writes. Move to cheaper nodes if available.
            "warm": {
                "min_age": "7d",  # Enter warm 7 days after rollover
                "actions": {
                    "set_priority": {"priority": 50},
                    # "readonly": {},  # No more writes
                    # "forcemerge": {
                    #     "max_num_segments": 1
                    # },
                    "shrink": {
                        # Reduce to 1 shard if you had more during hot phase.
                        # Warm data doesn't need write parallelism.
                        "number_of_shards": 1
                    },
                    # "allocate": {
                    #     # Move to warm-tier nodes (HDD nodes tagged data_warm).
                    #     # If you don't have tiered hardware, remove this block.
                    #     "require": {"data": "warm"}
                    # },
                },
            },

            # ── COLD PHASE ─────────────────────────────────────────────────
            # Rarely searched. Minimum cost. Searchable on demand.
            "cold": {
                "min_age": "30d",  # Enter cold 30 days after rollover
                "actions": {
                    "set_priority": {"priority": 0},
                    # "readonly": {},
                    # "allocate": {
                    #     "require": {"data": "cold"}
                    #     # Cold-tier nodes: cheapest available storage.
                    #     # If running on Elastic Cloud: frozen tier is even cheaper
                    #     # but adds search latency (fetches from snapshot on demand).
                    # },
                    # Optional: searchable snapshots
                    # Mounts index from S3 snapshot instead of local disk.
                    # Cuts storage cost ~90% but adds 1-3s search latency.
                    # "searchable_snapshot": {
                    #     "snapshot_repository": "neuralops-s3-repo"
                    # }
                },
            },

            # ── DELETE PHASE ───────────────────────────────────────────────
            "delete": {
                "min_age": "365d",  # Delete 1 year after rollover
                # This is the ES metadata retention. S3 raw content has its own
                # lifecycle rule (30 days for context buffers, 1 year for archives).
                # The two lifecycles are independent — ES deletion does not touch S3.
                "actions": {
                    "delete": {}
                },
            },
        }
    }
}

# ── ENTERPRISE TENANT OVERRIDE ─────────────────────────────────────────────
# Enterprise tenants can configure custom retention (up to 2 years).
# This is handled by creating a separate ILM policy per enterprise tenant
# and assigning it at index creation time via the index template.
#
# Pattern:
#   - Standard tenants: neuralops-logs-ilm (above)
#   - Enterprise tenants: neuralops-logs-ilm-{tenant_id} (clone with custom delete age)

def build_enterprise_ilm_policy(retention_days: int) -> dict:
    """
    Clone the standard ILM policy with a custom delete age.
    Called during tenant provisioning for enterprise plan tenants.
    """
    import copy
    policy = copy.deepcopy(ILM_POLICY)
    policy["policy"]["phases"]["delete"]["min_age"] = f"{retention_days}d"
    return policy
