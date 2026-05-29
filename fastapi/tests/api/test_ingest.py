import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import status
from httpx import AsyncClient
from sqlalchemy import select

from app.api.dependencies.tenant import get_validated_tenant
from app.models.logs import IngestedLogMetadata
from app.models.outbox import OutboxEvent
from app.models.snapshots import TenantSnapshot
from main import app

# Target tenant UUID
TEST_TENANT_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
TEST_USER_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")


@pytest.fixture
def mock_s3():
    """Mock the aioboto3 S3 async client context manager."""
    with patch("app.api.v1.ingest.aioboto3.Session") as mock_session_class:
        mock_session = MagicMock()
        mock_client = AsyncMock()
        mock_client.put_object = AsyncMock()

        mock_client_context = AsyncMock()
        mock_client_context.__aenter__.return_value = mock_client
        mock_session.client.return_value = mock_client_context

        mock_session_class.return_value = mock_session
        yield mock_client


@pytest.fixture
def active_tenant():
    """Provide a validated active tenant snapshot."""
    tenant = TenantSnapshot()
    tenant.tenant_id = TEST_TENANT_ID
    tenant.plan_tier = "professional"
    tenant.is_suspended = False
    return tenant


@pytest.fixture(autouse=True)
def override_tenant(active_tenant):
    """Override tenant dependency injection to return our active tenant snapshot."""

    async def mock_get_validated_tenant():
        return active_tenant

    app.dependency_overrides[get_validated_tenant] = mock_get_validated_tenant
    yield
    app.dependency_overrides.pop(get_validated_tenant, None)


@pytest.fixture
def auth_headers():
    """Provide production-like API Gateway authentication headers."""
    return {
        "X-Tenant-ID": str(TEST_TENANT_ID),
        "X-User-ID": str(TEST_USER_ID),
        "X-User-Role": "admin",
    }


@pytest.fixture
async def register_tenant(db_session):
    """Seed a test tenant snapshot in DB-2 to satisfy database foreign key integrity constraints."""
    tenant = TenantSnapshot(
        tenant_id=TEST_TENANT_ID,
        plan_tier="professional",
        vector_namespace="tenant-1-namespace",
        is_suspended=False,
    )
    db_session.add(tenant)
    await db_session.flush()
    yield tenant


# ── Ingestion Tests ───────────────────────────────────────────────────────────


async def test_ingest_logs_success(
    client: AsyncClient, mock_s3, db_session, auth_headers, register_tenant
):
    """Test successful log ingestion uploads context logs to S3 and writes DB records."""
    incident_uuid = uuid.uuid4()
    payload = {
        "incident_id": str(incident_uuid),
        "service_name": "payment-api",
        "environment": "production",
        "context_logs": [
            {
                "seq": 1,
                "level": "info",
                "message": "Starting request",
                "timestamp": "2026-05-29T10:00:00Z",
            },
            {
                "seq": 2,
                "level": "error",
                "message": "DbTimeout",
                "timestamp": "2026-05-29T10:00:01Z",
            },
        ],
    }

    response = await client.post(
        "/api/v1/ingest/logs", json=payload, headers=auth_headers
    )

    assert response.status_code == status.HTTP_202_ACCEPTED
    data = response.json()
    assert data["incident_id"] == str(incident_uuid)
    assert f"logs/{TEST_TENANT_ID}/context/{incident_uuid}.json.gz" in data["s3_path"]

    # Assert S3 upload was initiated with compressed payload
    mock_s3.put_object.assert_called_once()

    # Verify metadata and outbox records committed in our transactional session
    db_session.expire_all()

    # Query database to assert IngestedLogMetadata exists
    meta_stmt = select(IngestedLogMetadata).where(
        IngestedLogMetadata.incident_id == incident_uuid
    )
    meta_row = (await db_session.execute(meta_stmt)).scalar_one_or_none()
    assert meta_row is not None
    assert meta_row.service_name == "payment-api"
    assert meta_row.environment == "production"

    # Query outbox event to confirm synchronization event is created
    outbox_stmt = select(OutboxEvent).where(OutboxEvent.key == str(incident_uuid))
    outbox_row = (await db_session.execute(outbox_stmt)).scalar_one_or_none()
    assert outbox_row is not None
    assert outbox_row.topic == f"raw.logs.{TEST_TENANT_ID}"


async def test_ingest_logs_suspended_tenant(
    client: AsyncClient, active_tenant, auth_headers
):
    """Test that suspended tenants receive a 403 response and logs are rejected."""
    # Modify active tenant fixture dynamically to be suspended
    active_tenant.is_suspended = True

    async def mock_suspended_tenant():
        # Raise HTTP 403 representing suspended tenant state
        from fastapi import HTTPException

        raise HTTPException(status_code=403, detail="Tenant is suspended.")

    app.dependency_overrides[get_validated_tenant] = mock_suspended_tenant

    payload = {
        "incident_id": str(uuid.uuid4()),
        "service_name": "auth-service",
        "environment": "staging",
        "context_logs": [
            {
                "seq": 1,
                "level": "info",
                "message": "logs",
                "timestamp": "2026-05-29T10:00:00Z",
            }
        ],
    }

    response = await client.post(
        "/api/v1/ingest/logs", json=payload, headers=auth_headers
    )
    assert response.status_code == status.HTTP_403_FORBIDDEN


async def test_ingest_logs_s3_client_failure(
    client: AsyncClient, mock_s3, db_session, auth_headers, register_tenant
):
    """Test that S3 upload failure returns a 502 and creates NO records in the DB."""
    incident_uuid = uuid.uuid4()
    payload = {
        "incident_id": str(incident_uuid),
        "service_name": "billing-service",
        "environment": "production",
        "context_logs": [
            {
                "seq": 1,
                "level": "info",
                "message": "logs",
                "timestamp": "2026-05-29T10:00:00Z",
            }
        ],
    }

    # Simulate S3 ClientError
    error_response = {"Error": {"Code": "AccessDenied", "Message": "Access Denied"}}
    mock_s3.put_object.side_effect = ClientError(error_response, "PutObject")

    response = await client.post(
        "/api/v1/ingest/logs", json=payload, headers=auth_headers
    )
    assert response.status_code == status.HTTP_502_BAD_GATEWAY

    # Verify no database records are written (rolled back / never created)
    db_session.expire_all()
    meta_stmt = select(IngestedLogMetadata).where(
        IngestedLogMetadata.incident_id == incident_uuid
    )
    meta_row = (await db_session.execute(meta_stmt)).scalar_one_or_none()
    assert meta_row is None


async def test_ingest_logs_s3_service_failure(
    client: AsyncClient, mock_s3, db_session, auth_headers, register_tenant
):
    """Test that S3 connection failure returns a 503 and creates NO records in the DB."""
    incident_uuid = uuid.uuid4()
    payload = {
        "incident_id": str(incident_uuid),
        "service_name": "billing-service",
        "environment": "production",
        "context_logs": [
            {
                "seq": 1,
                "level": "info",
                "message": "logs",
                "timestamp": "2026-05-29T10:00:00Z",
            }
        ],
    }

    # Simulate S3 Connection failure (BotoCoreError)
    mock_s3.put_object.side_effect = BotoCoreError()

    response = await client.post(
        "/api/v1/ingest/logs", json=payload, headers=auth_headers
    )
    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE

    # Verify no database records are written
    db_session.expire_all()
    meta_stmt = select(IngestedLogMetadata).where(
        IngestedLogMetadata.incident_id == incident_uuid
    )
    meta_row = (await db_session.execute(meta_stmt)).scalar_one_or_none()
    assert meta_row is None


async def test_ingest_logs_invalid_payload(client: AsyncClient, auth_headers):
    """Test that a malformed payload returns a 422 validation error."""
    payload = {
        "incident_id": "not-a-uuid",  # Malformed UUID
        "service_name": "billing-service",
        "environment": "production",
        "context_logs": [],  # Empty log lines list (schema minimum is 1)
    }

    response = await client.post(
        "/api/v1/ingest/logs", json=payload, headers=auth_headers
    )
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
