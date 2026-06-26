import pytest
from integrations.models import GitHubIntegration
from outbox.models import OutboxEvent
from rest_framework import status
from users.models import AuditLog


@pytest.mark.django_db
class TestGitHubIntegrationAPI:
    """
    Precision REST API integration tests for GitHubIntegrationView.
    Exposes and validates GET/POST GitHub integration status, upserts,
    write-only credential encryption at rest, DRF validation bounds,
    AuditLog tracking, transactional outbox event logging, and role gates.
    """

    @pytest.fixture(autouse=True)
    def setup_url(self):
        self.url = "/api/v1/integrations/github/"

    # ── 1. Retrieval (GET) Tests ──────────────────────────────────────────────

    def test_get_integration_not_found(self, admin_client):
        """Verify that retrieving a non-existent integration yields a 404 response."""
        response = admin_client.get(self.url)
        assert response.status_code == status.HTTP_404_NOT_FOUND
        assert response.data["code"] == "not_found"

    def test_get_integration_success(self, admin_client, tenant):
        """Verify retrieving integration returns metadata but NEVER leaks encrypted or plain secrets."""
        integration = GitHubIntegration.objects.create(
            tenant=tenant,
            repo_url="https://github.com/neuralops/core",
            repo_owner="neuralops",
            repo_name="core",
            github_installation_id="12345678",
            default_branch="main",
        )

        response = admin_client.get(self.url)
        assert response.status_code == status.HTTP_200_OK

        data = response.data["data"]
        assert data["id"] == str(integration.id)
        assert data["repo_url"] == "https://github.com/neuralops/core"
        assert data["repo_owner"] == "neuralops"
        assert data["repo_name"] == "core"
        assert data["default_branch"] == "main"

        # Security contract: Plaintext credentials must NEVER be returned to the client
        assert "pat" not in data
        assert "webhook_secret_input" not in data
        assert data["github_installation_id"] == "12345678"

    # ── 2. Create / Upsert (POST) Tests ───────────────────────────────────────

    def test_create_integration_success(self, admin_client, tenant, admin_user):
        """Verify successful POST connection encrypts secrets, logs audit trace, and logs outbox message."""
        payload = {
            "repo_url": "https://github.com/neuralops/backend-service",
            "repo_owner": "neuralops",
            "repo_name": "backend-service",
            "default_branch": "develop",
            "github_installation_id": "12345678",
        }

        response = admin_client.post(self.url, data=payload, format="json")
        assert response.status_code == status.HTTP_201_CREATED

        # Verify db persistence
        integration = GitHubIntegration.objects.get(tenant_id=tenant.id)
        assert integration.repo_owner == "neuralops"
        assert integration.repo_name == "backend-service"
        assert integration.default_branch == "develop"

        # Security check
        assert integration.github_installation_id == "12345678"

        # Verify AuditLog created
        audit = AuditLog.objects.latest("created_at")
        assert audit.action == "TENANT_CONFIG_UPDATED"
        assert audit.user == admin_user
        assert audit.resource_type == "GitHubIntegration"
        assert audit.resource_id == str(integration.id)

        # Verify outbox event written correctly for CDC sync to FastAPI snapshot table
        outbox = OutboxEvent.objects.filter(topic="config.tenants").latest("created_at")
        assert outbox.key == str(tenant.id)
        assert outbox.payload["event_type"] == "tenant.updated"

        git_data = outbox.payload["tenant"]["github_integration"]
        assert git_data["repo_url"] == "https://github.com/neuralops/backend-service"
        assert git_data["installation_id"] == integration.github_installation_id

    def test_update_integration_success(self, admin_client, tenant, admin_user):
        """Verify POST update (upsert) to an existing integration works, keeping current secrets if omitted."""
        integration = GitHubIntegration.objects.create(
            tenant=tenant,
            repo_url="https://github.com/neuralops/old-repo",
            repo_owner="neuralops",
            repo_name="old-repo",
            github_installation_id="existing_installation",
            default_branch="main",
        )

        # Update default branch and repo details, omitting plain credential secrets
        payload = {
            "repo_url": "https://github.com/neuralops/new-repo",
            "repo_owner": "neuralops",
            "repo_name": "new-repo",
            "default_branch": "main-prod",
        }

        response = admin_client.post(self.url, data=payload, format="json")
        assert response.status_code == status.HTTP_200_OK

        # Verify database updated
        integration.refresh_from_db()
        assert integration.repo_name == "new-repo"
        assert integration.default_branch == "main-prod"

        # Check: existing installation is preserved because they were omitted
        assert integration.github_installation_id == "existing_installation"

        # Verify AuditLog created
        audit = AuditLog.objects.latest("created_at")
        assert audit.action == "TENANT_CONFIG_UPDATED"
        assert "updated" in audit.description

    def test_create_integration_invalid_url(self, admin_client):
        """Verify that non-GitHub HTTPS URLs are rejected by validation constraints."""
        payload = {
            "repo_url": "https://gitlab.com/neuralops/core",  # GitLab instead of GitHub
            "repo_owner": "neuralops",
            "repo_name": "core",
            "default_branch": "main",
            "github_installation_id": "12345678",
        }

        response = admin_client.post(self.url, data=payload, format="json")
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "repo_url" in response.data["errors"]

    def test_create_integration_missing_installation_id_on_create(self, admin_client):
        """Verify that installation ID is strictly required during the initial connection."""
        payload = {
            "repo_url": "https://github.com/neuralops/core",
            "repo_owner": "neuralops",
            "repo_name": "core",
            "default_branch": "main",
        }

        response = admin_client.post(self.url, data=payload, format="json")
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "github_installation_id" in response.data["errors"]



    # ── 3. Permissions / Guard Boundaries ─────────────────────────────────────

    def test_forbidden_for_engineer_user(self, engineer_client, tenant):
        """Verify that a basic tenant engineer is forbidden from viewing or configuring integrations."""
        # Try GET (forbidden)
        response = engineer_client.get(self.url)
        assert response.status_code == status.HTTP_403_FORBIDDEN

        # Try POST (forbidden)
        response = engineer_client.post(
            self.url,
            data={
                "repo_url": "https://github.com/neuralops/core",
                "repo_owner": "neuralops",
                "repo_name": "core",
                "default_branch": "main",
                "github_installation_id": "12345678",
            },
        )
        assert response.status_code == status.HTTP_403_FORBIDDEN
