import hashlib
import hmac
import json
import uuid
from unittest.mock import MagicMock, patch

import pytest
from fastapi import status
from httpx import AsyncClient

from app.core.config import get_settings
from app.models.snapshots import TenantSnapshot
from app.models.github_integration_snapshots import GitHubIntegrationSnapshot

TEST_TENANT_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
TEST_REPO_URL = "https://github.com/neuralops/test-repo"
TEST_SECRET = "super_secure_webhook_secret"


@pytest.fixture(autouse=True)
def mock_webhook_secret(monkeypatch):
    """Set the global webhook secret for tests."""
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", TEST_SECRET)
    get_settings.cache_clear()


@pytest.fixture
async def register_tenant(db_session):
    """Seed a test tenant snapshot with connected GitHub details inside our transactional session."""
    tenant = TenantSnapshot(
        tenant_id=TEST_TENANT_ID,
        plan_tier="enterprise",
        vector_namespace="tenant-2-namespace",
        is_suspended=False,
    )
    db_session.add(tenant)
    await db_session.flush()

    integration = GitHubIntegrationSnapshot(
        id=uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
        tenant_id=TEST_TENANT_ID,
        repo_url=TEST_REPO_URL,
        repo_owner="neuralops",
        repo_name="test-repo",
        installation_id=123456,
        default_branch="main",
        indexing_status="indexed",
    )
    db_session.add(integration)
    await db_session.flush()
    yield tenant


# ── Webhook Tests ─────────────────────────────────────────────────────────────


async def test_receive_github_webhook_ping(client: AsyncClient):
    """Test that a ping event returns status pong immediately without signature verification."""
    payload = {"zen": "Keep it simple, stupid."}
    headers = {"x-github-event": "ping", "Content-Type": "application/json"}

    response = await client.post(
        "/api/v1/webhooks/github", json=payload, headers=headers
    )

    assert response.status_code == status.HTTP_202_ACCEPTED
    assert response.json()["status"] == "pong"


async def test_receive_github_webhook_push_success(
    client: AsyncClient, register_tenant, db_session
):
    """Test successful GitHub push event verifies HMAC-SHA256 signature and schedules Celery task."""
    payload = {
        "after": "sha_most_recent_commit_123",
        "repository": {"clone_url": TEST_REPO_URL, "html_url": TEST_REPO_URL},
        "commits": [
            {
                "id": "sha_most_recent_commit_123",
                "added": [
                    "apps/users/views.py",
                    "README.md",
                ],  # .py indexable, md ignored
                "modified": ["app.java"],  # .java indexable
                "removed": ["deleted_code.py"],  # .py indexable
            }
        ],
    }

    # Calculate valid HMAC-SHA256 signature
    raw_body = json.dumps(payload).encode("utf-8")
    signature = hmac.new(
        TEST_SECRET.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()

    headers = {
        "x-github-event": "push",
        "x-hub-signature-256": f"sha256={signature}",
        "Content-Type": "application/json",
    }

    with patch("app.worker.tasks.index_code.index_code") as mock_index_code:
        # We pass content directly since httpx automatically serializes dicts,
        # but we must ensure it matches our signature bytes exactly.
        response = await client.post(
            "/api/v1/webhooks/github", content=raw_body, headers=headers
        )

        assert response.status_code == status.HTTP_202_ACCEPTED
        data = response.json()
        assert data["status"] == "accepted"
        assert data["commit_sha"] == "sha_most_recent_commit_123"
        assert data["changed_files"] == 2  # views.py and app.java (.md ignored)
        assert data["removed_files"] == 1  # deleted_code.py

        # Verify that the async Celery task was successfully dispatched with correct parameters
        mock_index_code.delay.assert_called_once_with(
            tenant_id=str(TEST_TENANT_ID),
            repo_url=TEST_REPO_URL,
            commit_sha="sha_most_recent_commit_123",
            changed_files=["apps/users/views.py", "app.java"],
            removed_files=["deleted_code.py"],
            is_initial=False,
        )


async def test_receive_github_webhook_invalid_signature(
    client: AsyncClient, register_tenant
):
    """Test that webhooks with incorrect signatures are immediately rejected with 401 Unauthorized."""
    payload = {"after": "sha123", "repository": {"clone_url": TEST_REPO_URL}}
    raw_body = json.dumps(payload).encode("utf-8")

    # Send incorrect signature
    headers = {
        "x-github-event": "push",
        "x-hub-signature-256": "sha256=invalidsignaturehexstring",
        "Content-Type": "application/json",
    }

    with patch("app.worker.tasks.index_code.index_code") as mock_index_code:
        response = await client.post(
            "/api/v1/webhooks/github", content=raw_body, headers=headers
        )

        assert response.status_code == status.HTTP_401_UNAUTHORIZED
        mock_index_code.delay.assert_not_called()


async def test_receive_github_webhook_tenant_not_found(client: AsyncClient):
    """Test that push events from an unregistered repository return 404 Not Found."""
    payload = {
        "after": "sha123",
        "repository": {"clone_url": "https://github.com/unknown/unregistered-repo"},
    }
    raw_body = json.dumps(payload).encode("utf-8")

    headers = {
        "x-github-event": "push",
        "x-hub-signature-256": "sha256=dummysignature",
        "Content-Type": "application/json",
    }

    response = await client.post(
        "/api/v1/webhooks/github", content=raw_body, headers=headers
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND
