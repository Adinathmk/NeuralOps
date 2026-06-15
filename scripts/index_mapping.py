"""
NeuralOps — Elasticsearch Index Mapping
Log event metadata index for multi-tenant filtering.

Design decisions:
- All string filter fields are `keyword` type — no text analysis needed.
  We never do fuzzy/relevance search on these fields; only exact-match filters.
- `timestamp` is `date` for time-range queries and ILM rollover conditions.
- `line_number` is `integer` — small, filterable, never searched as text.
- `_source` is enabled but restricted to only the fields we actually store.
  No free-text content ever enters this index.
- `dynamic: strict` — rejects any field not in the mapping.
  Prevents accidental raw log content being indexed if a bug sends wrong payload.
"""

LOG_EVENT_INDEX_MAPPING = {
    "settings": {
        # ILM policy name — defined separately in ilm_policy.py
        "index.lifecycle.name": "neuralops-logs-ilm",
        # Rollover alias — ILM uses this to create new backing indices
        "index.lifecycle.rollover_alias": "neuralops-logs",
        # Shards: 1 per node in your cluster initially.
        # At Sentry scale you'd increase this, but start here and measure.
        "number_of_shards": 1,
        # 1 replica: tolerate 1 node failure without data loss.
        "number_of_replicas": 1,
        # Refresh interval: 5s instead of default 1s.
        # Trades 5s search visibility lag for 5x less segment creation overhead.
        # For incident debugging this lag is completely acceptable.
        "refresh_interval": "5s",
        # Codec: best_compression saves ~30-40% storage at minor CPU cost.
        # Error log metadata is highly compressible (repeated field names/values).
        "codec": "best_compression",
    },
    "mappings": {
        # strict: any field not defined below causes the indexing request to fail.
        # This is your guard against accidentally storing raw log content.
        "dynamic": "strict",
        "properties": {
            # --- Identity fields ---
            "log_id": {
                "type": "keyword",
                # doc_values: true (default) — enables aggregations and sorting.
                # index: true (default) — enables filtering.
            },
            "tenant_id": {
                "type": "keyword",
                # This field is on EVERY query. Elasticsearch will push this
                # filter down to shard level — very fast.
            },
            "incident_id": {
                "type": "keyword",
                # Used for: "show all log events for this incident"
            },

            # --- Filter fields (what the search page exposes) ---
            "service_name": {
                "type": "keyword",
                # Exact match: service_name = "payment-api"
                # keyword fields are case-sensitive — normalise to lowercase
                # at write time in the ingest service, not here.
            },
            "environment": {
                "type": "keyword",
                # Values: "production", "staging", "development"
                # Low cardinality — perfect keyword field.
            },
            "severity": {
                "type": "keyword",
                # Values: "ERROR", "CRITICAL"
                # Since you only ingest errors, this will mostly be ERROR/CRITICAL.
                # Still useful for filtering CRITICAL-only incidents.
            },
            "error_type": {
                "type": "keyword",
                # e.g. "NullPointerException", "TimeoutError", "KeyError"
                # Exact match. Do NOT use text type — you don't want
                # "TimeoutError" to match "timeout" fuzzy searches.
            },
            "file_path": {
                "type": "keyword",
                # e.g. "src/payment/service.py"
                # Full path, exact match.
            },
            "line_number": {
                "type": "integer",
                # Stored for filtering: "all errors at line 142 of file X"
                # Useful for high-frequency crashes at same location.
            },

            # --- Time field ---
            "timestamp": {
                "type": "date",
                "format": "strict_date_optional_time",
                # ISO 8601: "2026-06-13T03:42:00Z"
                # This field drives:
                # 1. Time-range filter on search page (last 1h, 6h, 24h, 7d)
                # 2. ILM rollover — index rolls when it hits age or size threshold
                # 3. Default sort order (desc) on search results
            },

            # --- Incident state ---
            "status": {
                "type": "keyword",
                # Values: "open", "resolved", "investigating"
                # Lets engineers filter: "show only open incidents"
                # Updated via update-by-query when incident status changes in DB-2
            },

            # --- S3 pointer (never searched, just retrieved) ---
            "s3_key": {
                "type": "keyword",
                # e.g. "logs/tenant_x/context/incident_y.json.gz"
                # Returned in search results so the frontend knows where
                # to fetch full log content from S3 via pre-signed URL.
                # index: false would save space but we keep it true for
                # potential future filtering by key prefix.
            },
        },
    },
}
