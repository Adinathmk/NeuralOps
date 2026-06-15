"""
NeuralOps — Elasticsearch Client Setup (FastAPI service)

Uses the official `elasticsearch-py` async client.
Install: pip install elasticsearch[async]

Connection is initialised once at FastAPI startup and reused.
The client handles connection pooling, sniffing, and retries internally.
"""

import logging
from functools import lru_cache
from elasticsearch import AsyncElasticsearch
from app.core.config import get_settings

settings = get_settings()

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def get_es_client() -> AsyncElasticsearch:
    """
    Returns a singleton AsyncElasticsearch client.
    lru_cache ensures only one client instance exists per process.

    In production: Elasticsearch URL comes from Vault-injected env var,
    not hardcoded. The client never sees credentials in source code.
    """
    kwargs = {
        "hosts": settings.ELASTICSEARCH_HOSTS,
        "connections_per_node": 10,
        "retry_on_timeout": True,
        "max_retries": 3,
        "request_timeout": 10,
        "sniff_on_start": True,
        "sniff_on_node_failure": True,
        "min_delay_between_sniffing": 60,
    }

    if settings.ELASTICSEARCH_USERNAME and settings.ELASTICSEARCH_PASSWORD:
        kwargs["basic_auth"] = (
            settings.ELASTICSEARCH_USERNAME,
            settings.ELASTICSEARCH_PASSWORD,
        )

    if any(host.startswith("https") for host in settings.ELASTICSEARCH_HOSTS):
        kwargs["verify_certs"] = True
        if settings.ELASTICSEARCH_CA_CERT_PATH:
            kwargs["ca_certs"] = settings.ELASTICSEARCH_CA_CERT_PATH
    else:
        kwargs["verify_certs"] = False

    client = AsyncElasticsearch(**kwargs)
    logger.info(
        "Elasticsearch client initialised",
        extra={"hosts": settings.ELASTICSEARCH_HOSTS},
    )
    return client


async def close_es_client() -> None:
    """
    Call this on FastAPI shutdown to cleanly close all connections.
    Register in FastAPI lifespan context manager.
    """
    client = get_es_client()
    await client.close()


# ── FastAPI lifespan registration ──────────────────────────────────────────
# In your main.py:
#
# from contextlib import asynccontextmanager
# from app.database.elasticsearch import get_es_client, close_es_client
#
# @asynccontextmanager
# async def lifespan(app: FastAPI):
#     get_es_client()          # initialise on startup
#     yield
#     await close_es_client()  # clean close on shutdown
#
# app = FastAPI(lifespan=lifespan)
