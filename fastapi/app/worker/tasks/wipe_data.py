"""
fastapi/app/worker/tasks/wipe_data.py

Celery task for wiping a tenant's data when their GitHub integration is deleted.
This wipes the code index, incidents, and logs from both PostgreSQL and Elasticsearch.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

import aioboto3
import sqlalchemy as sa
from botocore.config import Config

from elasticsearch import AsyncElasticsearch

from app.core.config import get_settings
from app.database.session import AsyncSessionLocal
from app.models.code_index import CodeIndex
from app.models.incidents import Incident
from app.worker.celery_app import celery_app

logger = logging.getLogger(__name__)


async def _wipe_tenant_data_async(tenant_id: str) -> dict:
    """Async implementation of tenant data wipe."""
    tenant_uuid = uuid.UUID(tenant_id)

    # 1. PostgreSQL DB-2 Wipe
    async with AsyncSessionLocal() as session:
        async with session.begin():
            # Delete CodeIndexNode (AST entries)
            # RLS handles isolation if session local app.tenant_id is set,
            # but background tasks bypass RLS via tenant_id explicitly.
            # Using raw DELETE statements for bulk efficiency.
            code_stmt = sa.delete(CodeIndex).where(CodeIndex.tenant_id == tenant_uuid)
            await session.execute(code_stmt)

            # Delete Incidents (which cascades to analyses, occurrences, outbox)
            # Incident model is the root.
            inc_stmt = sa.delete(Incident).where(Incident.tenant_id == tenant_uuid)
            await session.execute(inc_stmt)

            # Commit happens automatically via session.begin()

    # 2. Elasticsearch Wipe
    _settings = get_settings()
    es_kwargs = {
        "hosts": _settings.ELASTICSEARCH_HOSTS,
        "connections_per_node": 2,
        "retry_on_timeout": True,
        "max_retries": 3,
        "request_timeout": 10,
        "sniff_on_start": False,
        "sniff_on_node_failure": False,
    }
    if _settings.ELASTICSEARCH_USERNAME and _settings.ELASTICSEARCH_PASSWORD:
        es_kwargs["basic_auth"] = (
            _settings.ELASTICSEARCH_USERNAME,
            _settings.ELASTICSEARCH_PASSWORD,
        )
    if any(host.startswith("https") for host in _settings.ELASTICSEARCH_HOSTS):
        es_kwargs["verify_certs"] = True
        if _settings.ELASTICSEARCH_CA_CERT_PATH:
            es_kwargs["ca_certs"] = _settings.ELASTICSEARCH_CA_CERT_PATH
    else:
        es_kwargs["verify_certs"] = False

    es = AsyncElasticsearch(**es_kwargs)
    try:
        # Delete documents for this tenant across all neuralops-logs indices.
        # This handles both shared indices (where we must not delete the index itself)
        # and enterprise dedicated indices (where deleting the docs empties it).
        await es.delete_by_query(
            index="neuralops-logs*",
            body={
                "query": {
                    "term": {
                        "tenant_id": tenant_id
                    }
                }
            },
            conflicts="proceed",
        )
    except Exception as exc:
        logger.error(
            "wipe_data_es_failed",
            extra={"tenant_id": tenant_id, "error": str(exc)},
            exc_info=True,
        )
        raise
    finally:
        await es.close()

    # 3. MinIO / S3 Wipe
    _settings = get_settings()
    session_s3 = aioboto3.Session(
        aws_access_key_id=_settings.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=_settings.AWS_SECRET_ACCESS_KEY,
        region_name=_settings.AWS_REGION_NAME,
    )
    try:
        async with session_s3.client(
            "s3",
            endpoint_url=_settings.AWS_S3_ENDPOINT_URL,
            config=Config(
                connect_timeout=3, read_timeout=10, retries={"max_attempts": 3}
            ),
        ) as s3_client:
            prefixes_to_wipe = [
                f"{tenant_id}/",
                f"code/{tenant_id}/",
                f"logs/{tenant_id}/",
            ]
            for wipe_prefix in prefixes_to_wipe:
                paginator = s3_client.get_paginator("list_objects_v2")
                async for page in paginator.paginate(
                    Bucket=_settings.AWS_S3_BUCKET_NAME, Prefix=wipe_prefix
                ):
                    if "Contents" in page:
                        delete_keys = [{"Key": obj["Key"]} for obj in page["Contents"]]
                        if delete_keys:
                            await s3_client.delete_objects(
                                Bucket=_settings.AWS_S3_BUCKET_NAME,
                                Delete={"Objects": delete_keys},
                            )
    except Exception as exc:
        logger.error(
            "wipe_data_s3_failed",
            extra={"tenant_id": tenant_id, "error": str(exc)},
            exc_info=True,
        )
        raise

    return {"status": "success", "tenant_id": tenant_id}


@celery_app.task(
    name="wipe_tenant_data",
    bind=True,
    max_retries=3,
    default_retry_delay=10,
)
def wipe_tenant_data(self, tenant_id: str) -> dict:
    """
    Celery task to wipe all logs, incidents, and AST data for a tenant.
    Called when a tenant's GitHub Integration is deleted.
    """
    logger.info("wipe_tenant_data_started", extra={"tenant_id": tenant_id})
    try:
        result = asyncio.run(_wipe_tenant_data_async(tenant_id))
        logger.info("wipe_tenant_data_completed", extra={"tenant_id": tenant_id})
        return result
    except Exception as exc:
        logger.error(
            "wipe_tenant_data_failed",
            extra={"tenant_id": tenant_id, "error": str(exc)},
            exc_info=True,
        )
        raise self.retry(exc=exc)
