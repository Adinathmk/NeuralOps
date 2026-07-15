"""
NeuralOps — Log Event Indexing (Write Path)

Called from Stage 1 of the AI agent pipeline (ingestion_service.py).
Writes log metadata to Elasticsearch synchronously in the same request
as the DB-2 write. The HTTP response is returned after both writes complete.

Why synchronous and not async via Celery:
- The search page needs to reflect new events quickly (5s refresh interval).
- The ES write is fast (sub-10ms for a single small document).
- Deferring to Celery adds queue latency with no benefit for a 400-byte document.
- If ES is down, the circuit breaker catches it — the ingest still succeeds
  and writes to DB-2. The ES write failure is logged but not fatal.
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from elasticsearch import AsyncElasticsearch, BadRequestError, ConnectionError
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.database.elasticsearch_client import get_es_client
from app.schemas.ingest import LogIngestRequest
from app.services.index_template import get_write_alias

logger = logging.getLogger(__name__)


class LogEventIndexer:
    """
    Handles writing log metadata documents to Elasticsearch.
    One instance per request (stateless — all state is in ES and DB-2).
    """

    def __init__(self, es_client: Optional[AsyncElasticsearch] = None):
        self.es = es_client or get_es_client()

    # ── SINGLE DOCUMENT INDEX ──────────────────────────────────────────────

    @retry(
        # Retry only on transient connection errors, not on bad request errors.
        # A BadRequestError means the document schema is wrong — retrying won't fix it.
        retry=retry_if_exception_type(ConnectionError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        reraise=False,  # Don't raise after all retries — log and continue
    )
    async def index_log_event(
        self,
        parsed_log: LogIngestRequest,
        incident_id: str,
        tenant_id: str,
        plan_tier: str,
        s3_key: str,
    ) -> bool:
        """
        Index a single log metadata document into Elasticsearch.

        Returns True on success, False on failure.
        Failure is non-fatal — the ingest pipeline continues.
        The circuit breaker in the calling service handles ES unavailability.

        Args:
            parsed_log:  The structured log event after Stage 2 parsing.
            incident_id: UUID of the incident created/matched in DB-2.
            tenant_id:   From gateway-injected X-Tenant-ID header.
            plan_tier:   From tenant_snapshots — determines which alias to write to.
            s3_key:      S3 object key for the compressed context buffer.
        """
        document = self._build_document(
            parsed_log=parsed_log,
            incident_id=incident_id,
            tenant_id=tenant_id,
            s3_key=s3_key,
        )

        write_alias = get_write_alias(tenant_id=tenant_id, plan_tier=plan_tier)

        try:
            await self.es.index(
                index=write_alias,
                id=document["log_id"],  # Use log_id as ES document ID
                document=document,
                # op_type=create: fails if document with this ID already exists.
                # Prevents duplicate indexing on Celery retry without a separate check.
                op_type="create",
                # pipeline: if you add an ingest pipeline later (e.g. for
                # GeoIP enrichment or field normalisation), reference it here.
                # pipeline="neuralops-log-enrichment",
                request_timeout=5,  # Tight timeout — ingest is latency-sensitive
            )
            logger.debug(
                "Log event indexed to Elasticsearch",
                extra={
                    "log_id": document["log_id"],
                    "tenant_id": tenant_id,
                    "index": write_alias,
                },
            )
            return True

        except BadRequestError as e:
            # Document schema violation — log and skip. Never retry.
            logger.error(
                "ES index rejected document — schema mismatch",
                extra={
                    "error": str(e),
                    "log_id": document["log_id"],
                    "tenant_id": tenant_id,
                },
            )
            return False

        except Exception as e:
            # ConnectionError, TransportError, etc — tenacity will retry.
            # If all retries exhausted, reraise=False means we return False.
            logger.warning(
                "ES index failed after retries",
                extra={"error": str(e), "tenant_id": tenant_id},
            )
            return False

    # ── BULK INDEX (for backfill / replay scenarios) ───────────────────────

    async def bulk_index_log_events(
        self,
        events: list[dict],
        tenant_id: str,
        plan_tier: str,
    ) -> tuple[int, int]:
        """
        Bulk index multiple log metadata documents.
        Used for:
        - Backfilling historical events after ES downtime
        - Migrating a tenant from shared to dedicated index

        Returns (success_count, failure_count).
        """
        from elasticsearch.helpers import async_bulk

        write_alias = get_write_alias(tenant_id=tenant_id, plan_tier=plan_tier)

        actions = [
            {
                "_index": write_alias,
                "_id": event["log_id"],
                "_source": event,
                "_op_type": "create",
            }
            for event in events
        ]

        success, errors = await async_bulk(
            client=self.es,
            actions=actions,
            # chunk_size: how many documents per bulk request.
            # 500 is a safe default. Tune based on document size and ES heap.
            chunk_size=500,
            # raise_on_error=False: collect errors instead of failing fast.
            # You want to know which documents failed, not stop at the first one.
            raise_on_error=False,
            raise_on_exception=False,
            request_timeout=30,
        )

        if errors:
            logger.error(
                "Bulk index errors",
                extra={"error_count": len(errors), "tenant_id": tenant_id},
            )

        return success, len(errors) if errors else 0

    # ── STATUS UPDATE ──────────────────────────────────────────────────────

    async def update_incident_status(
        self,
        incident_id: str,
        tenant_id: str,
        plan_tier: str,
        new_status: str,
    ) -> None:
        """
        Update the `status` field on all log events for a given incident.
        Called when an incident is resolved/reopened in DB-2.

        Uses update_by_query — finds all documents where incident_id matches
        and sets the new status. This keeps ES in sync with DB-2.

        Why update_by_query and not individual updates:
        - An incident can have multiple log events (occurrence_count > 1)
        - We don't want to track every log_id — one query handles all of them
        """
        search_index = get_write_alias(tenant_id=tenant_id, plan_tier=plan_tier)

        await self.es.update_by_query(
            index=search_index,
            body={
                "query": {
                    "bool": {
                        "must": [
                            {"term": {"tenant_id.keyword": tenant_id}},
                            {"term": {"incident_id.keyword": incident_id}},
                        ]
                    }
                },
                "script": {
                    "source": "ctx._source.status = params.new_status",
                    "lang": "painless",
                    "params": {"new_status": new_status},
                },
            },
            # conflicts=proceed: if a document is being updated concurrently,
            # skip it rather than failing the whole query.
            conflicts="proceed",
            request_timeout=30,
        )

    async def link_log_to_incident(
        self,
        raw_log_id: str,
        tenant_id: str,
        plan_tier: str,
        grouped_incident_id: str,
        severity: str,
        status: str,
    ) -> None:
        """
        Link a raw log event to a grouped incident, updating its severity and status.
        Called after deduplication in run_agent.py.
        """
        search_index = get_write_alias(tenant_id=tenant_id, plan_tier=plan_tier)
        
        import asyncio
        max_retries = 6
        for attempt in range(max_retries):
            resp = await self.es.update_by_query(
                index=search_index,
                body={
                    "query": {
                        "bool": {
                            "must": [
                                {"term": {"tenant_id.keyword": tenant_id}},
                                {"term": {"incident_id.keyword": raw_log_id}},
                            ]
                        }
                    },
                    "script": {
                        "source": """
                            ctx._source.incident_id = params.grouped_incident_id;
                            ctx._source.severity = params.severity.toLowerCase();
                            ctx._source.status = params.status;
                        """,
                        "lang": "painless",
                        "params": {
                            "grouped_incident_id": grouped_incident_id,
                            "severity": severity,
                            "status": status,
                        },
                    },
                },
                conflicts="proceed",
                request_timeout=30,
                refresh=True,
            )
            if resp.get("updated", 0) > 0:
                break
            if attempt < max_retries - 1:
                await asyncio.sleep(0.5)

    async def update_parsed_fields(
        self,
        incident_id: str,
        tenant_id: str,
        plan_tier: str,
        error_type: str,
        file_path: Optional[str],
        line_number: Optional[int],
        severity: str,
    ) -> None:
        """
        Update the parsed fields on the log event after Celery parsing.
        """
        search_index = get_write_alias(tenant_id=tenant_id, plan_tier=plan_tier)

        import asyncio

        max_retries = 5
        for attempt in range(max_retries):
            resp = await self.es.update_by_query(
                index=search_index,
                body={
                    "query": {
                        "bool": {
                            "must": [
                                {"term": {"tenant_id.keyword": tenant_id}},
                                {"term": {"incident_id.keyword": incident_id}},
                            ]
                        }
                    },
                    "script": {
                        "source": """
                            ctx._source.error_type = params.error_type;
                            if (params.file_path != null) {
                                ctx._source.file_path = params.file_path;
                            }
                            if (params.line_number != null) {
                                ctx._source.line_number = params.line_number;
                            }
                            if (params.severity != null) {
                                ctx._source.severity = params.severity.toLowerCase();
                            }
                        """,
                        "lang": "painless",
                        "params": {
                            "error_type": error_type,
                            "file_path": file_path,
                            "line_number": line_number,
                            "severity": severity,
                        },
                    },
                },
                conflicts="proceed",
                request_timeout=30,
                refresh=True,
            )
            if resp.get("updated", 0) > 0:
                break
            if attempt < max_retries - 1:
                await asyncio.sleep(0.5)

    # ── DOCUMENT BUILDER ───────────────────────────────────────────────────

    def _build_document(
        self,
        parsed_log: LogIngestRequest,
        incident_id: str,
        tenant_id: str,
        s3_key: str,
    ) -> dict:
        """
        Build the ES document from a parsed log event.
        This is the ONLY place where the ES schema is constructed.
        Nothing outside this method should build ES documents.

        All string fields are normalised to lowercase before indexing.
        ES keyword fields are case-sensitive — normalise here, not in queries.
        """
        return {
            # Identity
            "log_id": str(uuid.uuid4()),
            "tenant_id": str(tenant_id),
            "incident_id": str(incident_id),
            # Filter fields — all normalised to lowercase
            "service_name": parsed_log.service_name.lower().strip(),
            "environment": parsed_log.environment.lower().strip(),
            "severity": parsed_log.severity.lower(),
            "error_type": parsed_log.error_type.strip(),
            "file_path": parsed_log.file_path.strip() if parsed_log.file_path else None,
            "line_number": parsed_log.line_number,
            # Time
            "timestamp": datetime.now(timezone.utc).isoformat(),
            # State
            "status": "open",  # Always starts as open
            # S3 pointer
            "s3_key": s3_key,
        }
