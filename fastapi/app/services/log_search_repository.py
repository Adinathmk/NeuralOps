"""
NeuralOps — Log Search Repository (Read Path)

Called from the FastAPI log search endpoint.
Translates filter parameters from the request into an Elasticsearch query.

Query design principles:
1. tenant_id is ALWAYS the first filter. Non-negotiable. Every query starts here.
2. All filters are exact-match `term` queries on keyword fields — no full-text analysis.
3. Time range uses `range` on the `timestamp` field — pushed down to shard level.
4. Results are sorted by timestamp descending (newest first).
5. Pagination uses `search_after` (keyset pagination), not `from/size`.
   Reason: `from/size` scans all preceding documents on each page — becomes
   very slow past page 10. `search_after` is O(1) regardless of page depth.
"""

import logging
from dataclasses import dataclass, field
from typing import Optional

from elasticsearch import AsyncElasticsearch, NotFoundError

from app.database.elasticsearch_client import get_es_client
from app.services.index_template import get_search_index

logger = logging.getLogger(__name__)


# ── FILTER SCHEMA ─────────────────────────────────────────────────────────


@dataclass
class LogSearchFilters:
    """
    All possible filters the search page can send.
    All fields are optional except tenant_id.
    """

    tenant_id: str  # REQUIRED — always enforced
    severity: Optional[str] = None  # "ERROR" | "CRITICAL"
    service_name: Optional[str] = None  # e.g. "payment-api"
    environment: Optional[str] = None  # "production" | "staging"
    error_type: Optional[str] = None  # e.g. "NullPointerException"
    file_path: Optional[str] = None  # e.g. "src/payment/service.py"
    search_query: Optional[str] = None # Wildcard search on file_path and error_type
    status: Optional[str] = None  # "open" | "resolved"
    time_from: Optional[str] = None  # ISO 8601 e.g. "2026-06-13T00:00:00Z"
    time_to: Optional[str] = None  # ISO 8601
    # Preset time windows (convenience — overrides time_from/time_to if set)
    time_window: Optional[str] = None  # "1h" | "6h" | "24h" | "7d" | "30d"


@dataclass
class LogSearchRequest:
    filters: LogSearchFilters
    page_size: int = 50  # Max 200 — enforced in the endpoint
    # search_after: the sort values of the last document from the previous page.
    # Frontend sends this back to get the next page.
    # Format: [timestamp_value, log_id_value] — matches the sort clause below.
    search_after: Optional[list] = None
    plan_tier: str = "standard"


@dataclass
class LogSearchResult:
    hits: list[dict]
    total: int
    # search_after values from the last hit — send to client for next page cursor
    next_search_after: Optional[list] = None
    took_ms: int = 0


# ── REPOSITORY ─────────────────────────────────────────────────────────────


class LogSearchRepository:

    def __init__(self, es_client: Optional[AsyncElasticsearch] = None):
        self.es = es_client or get_es_client()

    async def search(self, request: LogSearchRequest) -> LogSearchResult:
        """
        Execute a filtered log search query.
        Returns paginated results with a cursor for the next page.
        """
        index = get_search_index(
            tenant_id=request.filters.tenant_id,
            plan_tier=request.plan_tier,
        )
        query = self._build_query(request.filters)
        body = {
            "query": query,
            "sort": self._build_sort(),
            "size": min(request.page_size, 200),  # Hard cap at 200
            "_source": self._source_fields(),
        }

        # Keyset pagination — only add if client sent a cursor
        if request.search_after:
            body["search_after"] = request.search_after

        # track_total_hits: true gives exact count up to 10,000.
        # For counts > 10,000 ES returns 10000+ which is fine for a UI counter.
        body["track_total_hits"] = True

        try:
            response = await self.es.search(
                index=index,
                body=body,
                request_timeout=10,
            )
        except NotFoundError:
            return LogSearchResult(hits=[], total=0, next_search_after=None, took_ms=0)

        hits = response["hits"]["hits"]
        total = response["hits"]["total"]["value"]
        took_ms = response["took"]

        # Extract the next page cursor from the last hit's sort values
        next_cursor = hits[-1]["sort"] if hits else None

        return LogSearchResult(
            hits=[h["_source"] for h in hits],
            total=total,
            next_search_after=next_cursor,
            took_ms=took_ms,
        )

    async def get_filter_options(
        self,
        tenant_id: str,
        plan_tier: str,
        time_window: str = "7d",
    ) -> dict:
        """
        Returns the available filter values for the search page dropdowns.
        e.g. "which service names does this tenant have errors from?"

        Uses terms aggregations — fast because they operate on doc_values
        (columnar storage, already sorted by Lucene). Not a scan.
        """
        index = get_search_index(tenant_id=tenant_id, plan_tier=plan_tier)

        try:
            response = await self.es.search(
                index=index,
                body={
                    "query": {
                        "bool": {
                            "must": [
                                {"term": {"tenant_id": tenant_id}},
                                {"range": {"timestamp": {"gte": f"now-{time_window}"}}},
                            ]
                        }
                    },
                    # We only need aggregations, not the hits themselves
                    "size": 0,
                    "aggs": {
                        # All aggregation fields must use .keyword sub-field.
                        # text fields have fielddata disabled — terms aggs require keyword.
                        "service_names": {
                            "terms": {
                                "field": "service_name.keyword",
                                "size": 100,  # Max 100 distinct service names
                            }
                        },
                        "severities": {"terms": {"field": "severity.keyword", "size": 10}},
                        "error_types": {"terms": {"field": "error_type.keyword", "size": 50}},
                        "environments": {"terms": {"field": "environment.keyword", "size": 10}},
                        "statuses": {"terms": {"field": "status.keyword", "size": 5}},
                    },
                },
                request_timeout=10,
            )
        except NotFoundError:
            return {
                "service_names": [],
                "severities": [],
                "error_types": [],
                "environments": [],
                "statuses": [],
            }

        aggs = response["aggregations"]
        return {
            "service_names": [b["key"] for b in aggs["service_names"]["buckets"]],
            "severities": [b["key"] for b in aggs["severities"]["buckets"]],
            "error_types": [b["key"] for b in aggs["error_types"]["buckets"]],
            "environments": [b["key"] for b in aggs["environments"]["buckets"]],
            "statuses": [b["key"] for b in aggs["statuses"]["buckets"]],
        }

    async def count_volume(
        self,
        tenant_id: str,
        plan_tier: str,
        time_window: str = "24h",
    ) -> int:
        """
        Count total logs ingested for a tenant within a time window.
        Used for the dashboard metrics.
        """
        index = get_search_index(tenant_id=tenant_id, plan_tier=plan_tier)
        try:
            response = await self.es.count(
                index=index,
                body={
                    "query": {
                        "bool": {
                            "must": [
                                {"term": {"tenant_id": tenant_id}},
                                {"range": {"timestamp": {"gte": f"now-{time_window}"}}},
                            ]
                        }
                    }
                }
            )
            return response["count"]
        except NotFoundError:
            return 0

    # ── QUERY BUILDERS ─────────────────────────────────────────────────────

    def _build_query(self, filters: LogSearchFilters) -> dict:
        """
        Build the bool query from filter parameters.

        Structure:
        bool.must:   All of these must match (AND semantics)
        bool.filter: Like must but no relevance scoring (faster — we don't
                     need relevance here, just filtering).

        We use `filter` context for everything because:
        - No relevance scoring needed (all exact-match filters)
        - Filter clauses are cached by ES — repeated queries are faster
        - Slightly lower query cost than `must` for pure filtering
        """
        # tenant_id is ALWAYS in filter. This runs before any other clause.
        filter_clauses = [{"term": {"tenant_id": filters.tenant_id}}]

        # Optional exact-match filters — only added if the value is set
        exact_match_fields = {
            "severity": filters.severity,
            "service_name": filters.service_name,
            "environment": filters.environment,
            "error_type": filters.error_type,
            "file_path": filters.file_path,
            "status": filters.status,
        }
        for field_name, value in exact_match_fields.items():
            if value:
                filter_clauses.append({"term": {field_name: value.strip()}})

        if getattr(filters, 'search_query', None):
            import re
            sq_raw = filters.search_query.strip()
            
            # Escape backslashes for Elasticsearch wildcard queries (vital for Windows paths)
            sq_escaped = sq_raw.replace('\\', '\\\\')
            
            # The UI shows "path:line_number" (e.g. "C:\...\file.py:41").
            # If the user pastes this exact string, strip the line number for the file_path search.
            file_path_sq_raw = re.sub(r':\d+$', '', sq_raw)
            file_path_sq_escaped = file_path_sq_raw.replace('\\', '\\\\')
            
            sq_file = f"*{file_path_sq_escaped}*"
            sq_error = f"*{sq_escaped}*"

            filter_clauses.append({
                "bool": {
                    "should": [
                        {"wildcard": {"file_path": {"value": sq_file, "case_insensitive": True}}},
                        {"wildcard": {"error_type": {"value": sq_error, "case_insensitive": True}}}
                    ],
                    "minimum_should_match": 1
                }
            })

        # Time range filter
        time_range = self._build_time_range(
            time_from=filters.time_from,
            time_to=filters.time_to,
            time_window=filters.time_window,
        )
        if time_range:
            filter_clauses.append({"range": {"timestamp": time_range}})

        return {"bool": {"filter": filter_clauses}}

    def _build_time_range(
        self,
        time_from: Optional[str],
        time_to: Optional[str],
        time_window: Optional[str],
    ) -> Optional[dict]:
        """
        Build the time range clause.
        time_window takes precedence if set (convenience presets).
        """
        if time_window:
            # ES date math: "now-1h", "now-7d", etc.
            return {"gte": f"now-{time_window}", "lte": "now"}

        range_clause = {}
        if time_from:
            range_clause["gte"] = time_from
        if time_to:
            range_clause["lte"] = time_to

        return range_clause if range_clause else None

    def _build_sort(self) -> list:
        """
        Sort by timestamp descending, then log_id ascending as tiebreaker.

        The tiebreaker (log_id) is critical for search_after pagination:
        if two documents have the same timestamp, the sort order must be
        deterministic or pagination will skip/duplicate documents.
        log_id is a UUID — unique per document — so it's a perfect tiebreaker.
        """
        return [
            {"timestamp": {"order": "desc"}},
            {"log_id.keyword": {"order": "asc"}},  # tiebreaker — must use .keyword (log_id is text type)
        ]

    def _source_fields(self) -> list[str]:
        """
        Only return the fields the UI actually needs.
        Excludes no fields here (all 11 are useful) but this is where you'd
        add exclusions if you added internal-only fields to the document later.
        """
        return [
            "log_id",
            "tenant_id",
            "incident_id",
            "service_name",
            "environment",
            "severity",
            "error_type",
            "file_path",
            "line_number",
            "timestamp",
            "status",
            "s3_key",
        ]
