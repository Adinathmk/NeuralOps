"""
app/api/v1/ingest.py

FastAPI router: ``POST /api/v1/ingest/logs``

This is the primary high-throughput log ingestion endpoint.  It accepts
the NeuralOps SDK's circular context buffer payload, compresses it
in-memory with gzip, uploads it to AWS S3, and atomically records the
ingestion metadata alongside a transactional outbox event in DB-2.

Request lifecycle
-----------------
1. JWT claims are validated by ``JWTAuthMiddleware`` at the gateway layer.
2. ``get_validated_tenant`` resolves and validates the tenant:
     a. Redis suspension flag check (fast, authoritative).
     b. Redis L1 config cache read-through (1-hour TTL).
     c. Postgres DB-2 snapshot lookup on cache miss.
3. S3 upload (async, non-blocking via aioboto3):
     - Payload: gzip-compressed JSON of ``context_logs``.
     - Key: ``logs/{tenant_id}/context/{incident_id}.json.gz``
     - If S3 is unavailable a ``502`` is returned BEFORE any DB write,
       keeping the database clean.
4. Atomic DB-2 transaction:
     - INSERT into ``ingested_log_metadata``.
     - INSERT into ``outbox`` (write_outbox helper).
     Debezium tails the WAL and delivers the outbox row to Kafka topic
     ``raw.logs.{tenant_id}`` — no direct Kafka publish from this service.
5. Return ``202 Accepted`` with the S3 path and echoed incident_id.

Architecture reference:
  NeuralOps Technical Documentation — Section 17 (AI Agent Pipeline,
  Stage 1), Section 8 (CDN & Object Storage), Section 9 (Idempotency).
"""

from __future__ import annotations

import gzip
import json
import logging
from uuid import UUID

import aioboto3
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies.rate_limit import rate_limit_dependency
from app.api.dependencies.tenant import get_validated_tenant
from app.core.config import get_settings
from app.core.logging import get_logger
from app.database.session import get_db
from app.models.logs import IngestedLogMetadata
from app.models.outbox import write_outbox
from app.models.snapshots import TenantSnapshot
from app.schemas.ingest import LogIngestRequest, LogIngestResponse
from app.services.circuit_breaker import get_es_circuit_breaker
from app.services.log_event_indexer import LogEventIndexer

logger = get_logger(__name__)
router = APIRouter(tags=["ingest"])

_settings = get_settings()


# ── S3 helpers ─────────────────────────────────────────────────────────────────


def _build_s3_key(tenant_id: str, incident_id: UUID) -> str:
    """
    Construct the canonical S3 object key for a context buffer payload.

    Format: ``logs/{tenant_id}/context/{incident_id}.json.gz``
    """
    return f"logs/{tenant_id}/context/{str(incident_id)}.json.gz"


def _compress_context_logs(context_logs: list) -> bytes:
    """
    Serialise ``context_logs`` to JSON and compress with gzip.

    Returns the compressed bytes ready for S3 upload.
    Raises ``ValueError`` if serialisation fails (should never happen
    for well-formed Pydantic-validated input).
    """
    raw_json: str = json.dumps(context_logs, default=str)
    return gzip.compress(raw_json.encode("utf-8"), compresslevel=6)


async def _upload_to_s3(
    compressed_payload: bytes,
    s3_key: str,
    tenant_id: str,
    incident_id: UUID,
) -> None:
    """
    Upload the compressed context-log payload to AWS S3 using aioboto3.

    aioboto3 is used (instead of boto3) to keep the upload fully
    non-blocking on the FastAPI async event loop.

    Args:
        compressed_payload: gzip-compressed bytes to upload.
        s3_key:             Destination S3 object key.
        tenant_id:          Used only for structured logging context.
        incident_id:        Used only for structured logging context.

    Raises:
        HTTPException(502): if the S3 upload fails for any reason.
                            This is raised BEFORE the DB transaction so
                            the database is never left in a partial state.
    """
    from botocore.config import Config

    session = aioboto3.Session(
        aws_access_key_id=_settings.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=_settings.AWS_SECRET_ACCESS_KEY,
        region_name=_settings.AWS_REGION_NAME,
    )

    try:
        async with session.client(
            "s3",
            endpoint_url=_settings.AWS_S3_ENDPOINT_URL,
            config=Config(
                connect_timeout=3, read_timeout=5, retries={"max_attempts": 1}
            ),
        ) as s3_client:
            await s3_client.put_object(
                Bucket=_settings.AWS_S3_BUCKET_NAME,
                Key=s3_key,
                Body=compressed_payload,
                ContentType="application/gzip",
                ContentEncoding="gzip",
                # Metadata stored as S3 object tags for observability
                Metadata={
                    "tenant_id": tenant_id,
                    "incident_id": str(incident_id),
                },
            )
        logger.info(
            "s3_upload_success",
            s3_key=s3_key,
            tenant_id=tenant_id,
            incident_id=str(incident_id),
            size_bytes=len(compressed_payload),
        )
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "Unknown")
        logger.error(
            "s3_upload_client_error",
            s3_key=s3_key,
            tenant_id=tenant_id,
            incident_id=str(incident_id),
            error_code=error_code,
            error=str(exc),
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                f"Failed to store log context in object storage "
                f"(S3 error {error_code}). Please retry."
            ),
        ) from exc
    except BotoCoreError as exc:
        logger.error(
            "s3_upload_botocore_error",
            s3_key=s3_key,
            tenant_id=tenant_id,
            incident_id=str(incident_id),
            error=str(exc),
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=("Object storage is temporarily unavailable. Please retry."),
        ) from exc


# ── Route ──────────────────────────────────────────────────────────────────────


@router.post(
    "/ingest/logs",
    response_model=LogIngestResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(rate_limit_dependency)],
    summary="Ingest SDK log context buffer",
    description=(
        "Accepts the NeuralOps SDK's circular context-log buffer after an "
        "error event. Compresses the payload with gzip, stores it in S3, "
        "and atomically records ingestion metadata and a transactional "
        "outbox event in DB-2. Debezium delivers the outbox row to Kafka "
        "topic ``raw.logs.{tenant_id}`` for downstream processing."
    ),
    responses={
        202: {"description": "Log context accepted and stored."},
        400: {"description": "Request validation error."},
        401: {"description": "Missing or invalid authentication token."},
        403: {"description": "Tenant is suspended or permission denied."},
        502: {"description": "S3 upload failed (upstream error)."},
        503: {"description": "Object storage temporarily unavailable."},
    },
)
async def ingest_logs(
    request: Request,
    payload: LogIngestRequest,
    tenant: TenantSnapshot = Depends(get_validated_tenant),
    db: AsyncSession = Depends(get_db),
) -> LogIngestResponse:
    """
    ``POST /api/v1/ingest/logs``

    High-throughput log context ingestion endpoint.

    Steps
    -----
    1. Resolve and validate tenant via ``get_validated_tenant``.
    2. Compress ``context_logs`` in-memory with gzip.
    3. Upload compressed bytes to S3 (non-blocking via aioboto3).
       Returns HTTP 502/503 if S3 is unavailable — NO DB write occurs.
    4. Open an atomic DB-2 transaction:
         a. INSERT ``IngestedLogMetadata`` row.
         b. INSERT ``OutboxEvent`` row via ``write_outbox()``.
            Debezium → Kafka topic ``raw.logs.{tenant_id}``.
    5. Return ``202 Accepted`` with ``incident_id`` and ``s3_path``.

    Args:
        request: Starlette Request (used for request_id logging).
        payload: Validated ``LogIngestRequest`` body.
        tenant:  Resolved TenantSnapshot from ``get_validated_tenant``.
        db:      Async SQLAlchemy session from ``get_db``.

    Returns:
        ``LogIngestResponse`` with the echoed incident_id and S3 path.
    """
    tenant_id_str = str(tenant.tenant_id)
    incident_id = payload.incident_id

    logger.info(
        "ingest_logs_start",
        tenant_id=tenant_id_str,
        incident_id=str(incident_id),
        service_name=payload.service_name,
        environment=payload.environment,
        context_log_count=len(payload.context_logs),
    )

    # ── Step 1: Compress context logs ─────────────────────────────────────────
    try:
        compressed = _compress_context_logs(payload.context_logs)
    except (TypeError, ValueError) as exc:
        logger.error(
            "ingest_compression_failed",
            tenant_id=tenant_id_str,
            incident_id=str(incident_id),
            error=str(exc),
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Failed to serialise context_logs. Ensure all values are JSON-serialisable.",
        ) from exc

    # ── Step 2: Upload to S3 (before DB write — fail-fast on S3 error) ────────
    s3_key = _build_s3_key(tenant_id_str, incident_id)

    await _upload_to_s3(
        compressed_payload=compressed,
        s3_key=s3_key,
        tenant_id=tenant_id_str,
        incident_id=incident_id,
    )

    # ── Step 3: Atomic DB-2 transaction ───────────────────────────────────────
    # Both the metadata row and the outbox event MUST be committed in the
    # same transaction.  If either INSERT fails the whole transaction rolls
    # back — the S3 object will be orphaned in that case (cleaned up by the
    # S3 lifecycle rule after 30 days) but no partial state is written to DB.
    try:
        # 3a. Persist ingestion metadata
        log_meta = IngestedLogMetadata(
            incident_id=incident_id,
            tenant_id=tenant.tenant_id,
            service_name=payload.service_name,
            environment=payload.environment,
            s3_path=s3_key,
        )
        db.add(log_meta)

        # 3b. Write outbox event — Debezium delivers to Kafka
        outbox_payload = {
            "event_type": "log.ingested",
            "incident_id": str(incident_id),
            "tenant_id": tenant_id_str,
            "service_name": payload.service_name,
            "environment": payload.environment,
            "s3_path": s3_key,
            "context_log_count": len(payload.context_logs),
        }
        write_outbox(
            session=db,
            topic=f"raw.logs.{tenant_id_str}",
            key=str(incident_id),
            payload=outbox_payload,
        )

        await db.commit()

    except Exception as exc:
        await db.rollback()
        # DB write failed after a successful S3 upload.
        # The S3 object is orphaned but will be cleaned up by the 30-day
        # lifecycle rule.  We return 500 so the SDK retries with the same
        # incident_id (idempotent — ON CONFLICT DO NOTHING on incident_id PK).
        logger.error(
            "ingest_db_transaction_failed",
            tenant_id=tenant_id_str,
            incident_id=str(incident_id),
            s3_key=s3_key,
            error=str(exc),
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to persist ingestion record. Please retry.",
        ) from exc

    # ── Step 4: Write to Elasticsearch (non-fatal) ────────────────────────────
    indexer = LogEventIndexer()
    await get_es_circuit_breaker().call(
        indexer.index_log_event,
        parsed_log=payload,
        incident_id=str(incident_id),
        tenant_id=tenant_id_str,
        plan_tier=tenant.plan_tier,
        s3_key=s3_key,
    )

    logger.info(
        "ingest_logs_success",
        tenant_id=tenant_id_str,
        incident_id=str(incident_id),
        s3_key=s3_key,
        compressed_bytes=len(compressed),
    )

    return LogIngestResponse(
        incident_id=incident_id,
        s3_path=s3_key,
        message="Log context ingested successfully.",
    )
