import pytest
from integrations.models import GitHubIntegration
from outbox.models import OutboxEvent
from rest_framework import status
from users.models import AuditLog


@pytest.mark.django_db
class TestGitHubIntegrationAPI:
    """
    Precision REST API integration tests for GitHubIntegration multi-repo views.
    """

    @pytest.fixture(autouse=True)
    def setup_urls(self):
        self.list_url = "/api/v1/integrations/github/"

    def get_detail_url(self, integration_id):
        return f"/api/v1/integrations/github/{integration_id}/"

    # ── 1. Retrieval (GET List & Detail) Tests ────────────────────────────────

    def test_get_integration_detail_not_found(self, admin_client):
        """Verify that retrieving a non-existent integration yields a 404 response."""
        import uuid
        response = admin_client.get(self.get_detail_url(uuid.uuid4()))
        assert response.status_code == status.HTTP_404_NOT_FOUND
        assert response.data["code"] == "not_found"

    def test_list_integrations_success(self, admin_client, tenant):
        """Verify retrieving the list of integrations."""
        GitHubIntegration.objects.create(
            tenant=tenant,
            repo_url="https://github.com/neuralops/core",
            repo_owner="neuralops",
            repo_name="core",
            github_installation_id="12345678",
            default_branch="main",
        )
        GitHubIntegration.objects.create(
            tenant=tenant,
            repo_url="https://github.com/neuralops/backend",
            repo_owner="neuralops",
            repo_name="backend",
            github_installation_id="12345678",
            default_branch="main",
        )

        response = admin_client.get(self.list_url)
        assert response.status_code == status.HTTP_200_OK
        data = response.data["data"]
        assert len(data) == 2

    def test_get_integration_detail_success(self, admin_client, tenant):
        """Verify retrieving a specific integration returns metadata."""
        integration = GitHubIntegration.objects.create(
            tenant=tenant,
            repo_url="https://github.com/neuralops/core",
            repo_owner="neuralops",
            repo_name="core",
            github_installation_id="12345678",
            default_branch="main",
        )

        response = admin_client.get(self.get_detail_url(integration.id))
        assert response.status_code == status.HTTP_200_OK

        data = response.data["data"]
        assert data["id"] == str(integration.id)
        assert data["repo_url"] == "https://github.com/neuralops/core"
        assert data["repo_owner"] == "neuralops"
        assert data["repo_name"] == "core"
        assert data["default_branch"] == "main"
        assert data["github_installation_id"] == "12345678"

    # ── 2. Create (POST) & Update (PATCH) Tests ───────────────────────────────

    def test_create_integration_success(self, admin_client, tenant, admin_user):
        """Verify successful POST connection encrypts secrets, logs audit trace, and logs outbox message."""
        payload = {
            "repo_url": "https://github.com/neuralops/backend-service",
            "repo_owner": "neuralops",
            "repo_name": "backend-service",
            "default_branch": "develop",
            "github_installation_id": "12345678",
        }

        response = admin_client.post(self.list_url, data=payload, format="json")
        assert response.status_code == status.HTTP_201_CREATED

        # Verify db persistence
        integration = GitHubIntegration.objects.get(tenant_id=tenant.id, repo_name="backend-service")
        assert integration.repo_owner == "neuralops"
        assert integration.default_branch == "develop"
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

        event = outbox.payload["tenant"]["github_integration_event"]
        assert event["action"] == "created"
        git_data = event["integration"]
        assert git_data["repo_url"] == "https://github.com/neuralops/backend-service"
        assert git_data["installation_id"] == integration.github_installation_id

    def test_create_duplicate_repo_url_rejected(self, admin_client, tenant):
        """Verify that duplicate repo_urls for the same tenant are rejected."""
        GitHubIntegration.objects.create(
            tenant=tenant,
            repo_url="https://github.com/neuralops/core",
            repo_owner="neuralops",
            repo_name="core",
            github_installation_id="12345678",
            default_branch="main",
        )

        payload = {
            "repo_url": "https://github.com/neuralops/core",
            "repo_owner": "neuralops",
            "repo_name": "core",
            "default_branch": "main",
            "github_installation_id": "12345678",
        }

        response = admin_client.post(self.list_url, data=payload, format="json")
        assert response.status_code == status.HTTP_409_CONFLICT
        assert response.data["code"] == "duplicate_repo"

    def test_patch_integration_success(self, admin_client, tenant, admin_user):
        """Verify PATCH update to an existing integration works."""
        integration = GitHubIntegration.objects.create(
            tenant=tenant,
            repo_url="https://github.com/neuralops/old-repo",
            repo_owner="neuralops",
            repo_name="old-repo",
            github_installation_id="existing_installation",
            default_branch="main",
        )

        payload = {
            "repo_name": "new-repo",
            "default_branch": "main-prod",
        }

        response = admin_client.patch(self.get_detail_url(integration.id), data=payload, format="json")
        assert response.status_code == status.HTTP_200_OK

        # Verify database updated
        integration.refresh_from_db()
        assert integration.repo_name == "new-repo"
        assert integration.default_branch == "main-prod"

        # Check: existing installation is preserved because they were omitted
        assert integration.github_installation_id == "existing_installation"

        # Verify outbox event
        outbox = OutboxEvent.objects.filter(topic="config.tenants").latest("created_at")
        event = outbox.payload["tenant"]["github_integration_event"]
        assert event["action"] == "updated"

    # ── 3. Delete Tests ───────────────────────────────────────────────────────

    def test_delete_specific_integration(self, admin_client, tenant):
        integration = GitHubIntegration.objects.create(
            tenant=tenant,
            repo_url="https://github.com/neuralops/core",
            repo_owner="neuralops",
            repo_name="core",
            github_installation_id="12345678",
            default_branch="main",
        )

        response = admin_client.delete(self.get_detail_url(integration.id))
        assert response.status_code == status.HTTP_200_OK
        assert not GitHubIntegration.objects.filter(id=integration.id).exists()

        outbox = OutboxEvent.objects.filter(topic="config.tenants").latest("created_at")
        event = outbox.payload["tenant"]["github_integration_event"]
        assert event["action"] == "deleted"

    def test_delete_does_not_affect_other_integrations(self, admin_client, tenant):
        integration1 = GitHubIntegration.objects.create(
            tenant=tenant,
            repo_url="https://github.com/neuralops/repo1",
            repo_owner="neuralops",
            repo_name="repo1",
            github_installation_id="12345678",
            default_branch="main",
        )
        integration2 = GitHubIntegration.objects.create(
            tenant=tenant,
            repo_url="https://github.com/neuralops/repo2",
            repo_owner="neuralops",
            repo_name="repo2",
            github_installation_id="12345678",
            default_branch="main",
        )

        response = admin_client.delete(self.get_detail_url(integration1.id))
        assert response.status_code == status.HTTP_200_OK

        assert not GitHubIntegration.objects.filter(id=integration1.id).exists()
        assert GitHubIntegration.objects.filter(id=integration2.id).exists()

    # ── 4. Permissions / Guard Boundaries ─────────────────────────────────────

    def test_forbidden_for_engineer_user(self, engineer_client, tenant):
        """Verify that a basic tenant engineer is forbidden from viewing or configuring integrations."""
        # Try GET list (forbidden)
        response = engineer_client.get(self.list_url)
        assert response.status_code == status.HTTP_403_FORBIDDEN

        # Try POST (forbidden)
        response = engineer_client.post(
            self.list_url,
            data={
                "repo_url": "https://github.com/neuralops/core",
                "repo_owner": "neuralops",
                "repo_name": "core",
                "default_branch": "main",
                "github_installation_id": "12345678",
            },
        )
        assert response.status_code == status.HTTP_403_FORBIDDEN
