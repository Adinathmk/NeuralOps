"""
NeuralOps — Elasticsearch Bootstrap Script

Run this ONCE when setting up a new cluster, or when cluster state is wiped.
Idempotent — safe to run multiple times (uses create-if-not-exists semantics).

Order matters:
1. Create ILM policy first (index template references it)
2. Create index template (new indices will inherit it)
3. Create initial write alias (first index in the rollover chain)

Run as a Kubernetes Job before the FastAPI deployment rolls out,
same as database migrations. Never run from application startup.

Usage:
    python bootstrap_elasticsearch.py
"""

import asyncio
import logging
from elasticsearch import AsyncElasticsearch, NotFoundError

from ilm_policy import ILM_POLICY
from index_template import SHARED_INDEX_TEMPLATE
from index_mapping import LOG_EVENT_INDEX_MAPPING

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

import os
import json

# In production these come from environment variables injected by Vault
ES_HOSTS = json.loads(os.getenv("ELASTICSEARCH_HOSTS", '["http://localhost:9200"]'))
ES_USERNAME = os.getenv("ELASTICSEARCH_USERNAME", "elastic")
ES_PASSWORD = os.getenv("ELASTICSEARCH_PASSWORD", "changeme")


async def bootstrap(es: AsyncElasticsearch) -> None:

    # ── Step 1: ILM Policy ─────────────────────────────────────────────────
    logger.info("Creating ILM policy: neuralops-logs-ilm")
    await es.ilm.put_lifecycle(
        name="neuralops-logs-ilm",
        body=ILM_POLICY,
    )
    logger.info("ILM policy created")

    # ── Step 2: Index Template ─────────────────────────────────────────────
    logger.info("Creating index template: neuralops-logs-template")
    await es.indices.put_index_template(
        name="neuralops-logs-template",
        body=SHARED_INDEX_TEMPLATE,
    )
    logger.info("Index template created")

    # ── Step 3: Initial write index + alias ────────────────────────────────
    # The first backing index for the rollover chain.
    # Naming convention: {alias}-000001
    # ILM will create -000002, -000003, etc. as it rolls over.
    initial_index = "neuralops-logs-000001"
    alias = "neuralops-logs"

    try:
        exists = await es.indices.exists(index=initial_index)
        if exists:
            logger.info(f"Index {initial_index} already exists — skipping creation")
        else:
            logger.info(f"Creating initial index: {initial_index}")
            await es.indices.create(
                index=initial_index,
                body={
                    "settings": {
                        **LOG_EVENT_INDEX_MAPPING["settings"],
                        # Mark this index as the write index for the alias.
                        # When ILM rolls over, it creates a new index and
                        # sets is_write_index=true on it, and false on this one.
                        "index.lifecycle.rollover_alias": alias,
                    },
                    "mappings": LOG_EVENT_INDEX_MAPPING["mappings"],
                    "aliases": {
                        alias: {
                            "is_write_index": True
                        }
                    },
                },
            )
            logger.info(f"Initial index {initial_index} created with alias {alias}")
    except Exception as e:
        logger.error(f"Failed to create initial index: {e}")
        raise

    # ── Step 4: Verify cluster health ──────────────────────────────────────
    health = await es.cluster.health(wait_for_status="yellow", timeout="30s")
    logger.info(
        "Cluster health check",
        extra={
            "status": health["status"],
            "active_shards": health["active_shards"],
        },
    )

    if health["status"] == "red":
        raise RuntimeError("Elasticsearch cluster is RED — bootstrap aborted")

    logger.info("Bootstrap complete")


async def main():
    client_kwargs = {
        "hosts": ES_HOSTS,
    }
    
    # Only apply basic auth if credentials exist and we're not running with security disabled
    if ES_USERNAME and ES_PASSWORD:
        client_kwargs["basic_auth"] = (ES_USERNAME, ES_PASSWORD)
        
    # Only verify certs if we're connecting over HTTPS
    if any(host.startswith("https") for host in ES_HOSTS):
        client_kwargs["verify_certs"] = True
    else:
        client_kwargs["verify_certs"] = False

    es = AsyncElasticsearch(**client_kwargs)
    try:
        await bootstrap(es)
    finally:
        await es.close()


if __name__ == "__main__":
    asyncio.run(main())
