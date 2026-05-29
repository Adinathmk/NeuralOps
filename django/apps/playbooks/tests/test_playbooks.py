import pytest
from django.urls import reverse
from outbox.models import OutboxEvent
from playbooks.models import Playbook
from rest_framework import status


@pytest.mark.django_db
class TestPlaybookAPI:
    """
    Complete REST API integration tests for PlaybookViewSet.
    Exposes and validates CRUD operations, tenant scoping, role-based security,
    error regex validations, and transactional Kafka outbox publishing.
    """

    @pytest.fixture(autouse=True)
    def setup_url(self):
        # Retrieve URLs based on router naming: api/playbooks/playbooks/
        self.list_url = "/api/playbooks/playbooks/"

    def get_detail_url(self, playbook_id):
        return f"{self.list_url}{playbook_id}/"

    # ── 1. Create Playbook Tests ──────────────────────────────────────────────

    def test_create_playbook_success(self, admin_client, tenant):
        """Verify tenant admin can successfully create a playbook and write an outbox event."""
        payload = {
            "error_pattern": r"ZeroDivisionError.*division by zero",
            "instructions": "Ensure the denominator is non-zero before dividing.",
        }

        response = admin_client.post(self.list_url, data=payload, format="json")
        assert response.status_code == status.HTTP_201_CREATED

        data = response.data["data"]
        assert data["error_pattern"] == payload["error_pattern"]
        assert data["instructions"] == payload["instructions"]

        # Verify db persistence
        playbook = Playbook.objects.get(pk=data["id"])
        assert playbook.tenant == tenant

        # Verify transactional outbox event written correctly for CDC Kafka dispatch
        outbox = OutboxEvent.objects.filter(topic="config.playbooks").latest(
            "created_at"
        )
        assert outbox.key == str(tenant.id)
        assert outbox.payload["event_type"] == "playbook.created"
        assert outbox.payload["playbook"]["id"] == str(playbook.id)

    def test_create_playbook_invalid_regex(self, admin_client):
        """Verify that invalid regular expressions are caught by the serializer."""
        payload = {
            "error_pattern": "[invalid regex (unclosed group",
            "instructions": "Handle division by zero.",
        }

        response = admin_client.post(self.list_url, data=payload, format="json")
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "error_pattern" in response.data["errors"]

    def test_create_playbook_blank_fields(self, admin_client):
        """Verify that blank parameters are rejected by validation constraints."""
        payload = {
            "error_pattern": "",
            "instructions": "   ",
        }

        response = admin_client.post(self.list_url, data=payload, format="json")
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "error_pattern" in response.data["errors"]
        assert "instructions" in response.data["errors"]

    # ── 2. List & Scope Tests ─────────────────────────────────────────────────

    def test_list_playbooks_scoped_to_tenant(
        self, admin_client, owner_client, tenant, tenant_2
    ):
        """Verify list only returns playbooks owned by the authenticated tenant."""
        # Seed playbook for primary tenant
        p1 = Playbook.objects.create(
            tenant=tenant,
            error_pattern="ValueError",
            instructions="Check input format.",
        )
        # Seed playbook for secondary tenant
        p2 = Playbook.objects.create(
            tenant=tenant_2,
            error_pattern="KeyError",
            instructions="Check map key existence.",
        )

        # List as primary tenant admin
        response = admin_client.get(self.list_url)
        assert response.status_code == status.HTTP_200_OK
        data = response.data["data"]

        assert len(data) == 1
        assert data[0]["id"] == str(p1.id)
        assert data[0]["error_pattern"] == "ValueError"

    # ── 3. Retrieve Tests ─────────────────────────────────────────────────────

    def test_retrieve_playbook_success(self, owner_client, tenant):
        """Verify that a tenant owner can retrieve a specific playbook."""
        playbook = Playbook.objects.create(
            tenant=tenant,
            error_pattern="ConnectionError",
            instructions="Implement retry handler with exponential backoff.",
        )

        url = self.get_detail_url(playbook.id)
        response = owner_client.get(url)

        assert response.status_code == status.HTTP_200_OK
        assert response.data["data"]["id"] == str(playbook.id)

    def test_retrieve_playbook_not_found(self, admin_client):
        """Verify retrieving non-existent PK yields a 404 response."""
        import uuid

        url = self.get_detail_url(uuid.uuid4())
        response = admin_client.get(url)
        assert response.status_code == status.HTTP_404_NOT_FOUND

    # ── 4. Update & Patch Tests ───────────────────────────────────────────────

    def test_update_playbook_success(self, admin_client, tenant):
        """Verify full update (PUT) triggers model rewrite and logs outbox update event."""
        playbook = Playbook.objects.create(
            tenant=tenant,
            error_pattern="TimeoutError",
            instructions="Set socket timeout limit to 10s.",
        )

        payload = {
            "error_pattern": "TimeoutError.*socket",
            "instructions": "Increase timeout limit to 30s.",
        }

        url = self.get_detail_url(playbook.id)
        response = admin_client.put(url, data=payload, format="json")
        assert response.status_code == status.HTTP_200_OK

        # Assert database updated
        playbook.refresh_from_db()
        assert playbook.error_pattern == payload["error_pattern"]
        assert playbook.instructions == payload["instructions"]

        # Verify outbox event logged
        outbox = OutboxEvent.objects.filter(topic="config.playbooks").latest(
            "created_at"
        )
        assert outbox.payload["event_type"] == "playbook.updated"
        assert outbox.payload["playbook"]["id"] == str(playbook.id)

    def test_partial_update_playbook_success(self, admin_client, tenant):
        """Verify partial update (PATCH) rewrites single fields correctly."""
        playbook = Playbook.objects.create(
            tenant=tenant,
            error_pattern="DatabaseError",
            instructions="Close db connection pool.",
        )

        payload = {
            "instructions": "Recycle database connection pools in fallback blocks."
        }

        url = self.get_detail_url(playbook.id)
        response = admin_client.patch(url, data=payload, format="json")
        assert response.status_code == status.HTTP_200_OK

        playbook.refresh_from_db()
        assert playbook.error_pattern == "DatabaseError"  # Unchanged
        assert playbook.instructions == payload["instructions"]

    # ── 5. Destroy Tests ──────────────────────────────────────────────────────

    def test_delete_playbook_success(self, admin_client, tenant):
        """Verify delete removes row and publishes deletion event to outbox."""
        playbook = Playbook.objects.create(
            tenant=tenant,
            error_pattern="IndexError",
            instructions="Ensure list length satisfies indexing access.",
        )

        url = self.get_detail_url(playbook.id)
        response = admin_client.delete(url)
        assert response.status_code == status.HTTP_200_OK

        # Verify deleted from DB
        assert not Playbook.objects.filter(pk=playbook.id).exists()

        # Verify outbox deletion event logged
        outbox = OutboxEvent.objects.filter(topic="config.playbooks").latest(
            "created_at"
        )
        assert outbox.payload["event_type"] == "playbook.deleted"
        assert outbox.payload["playbook"]["deleted"] is True
        assert outbox.payload["playbook"]["id"] == str(playbook.id)

    # ── 6. Permissions / Guard Boundaries ─────────────────────────────────────

    def test_forbidden_for_engineer_user(self, engineer_client, tenant):
        """Verify that a basic tenant engineer is forbidden from viewing or writing playbooks."""
        # Seed a playbook
        playbook = Playbook.objects.create(
            tenant=tenant, error_pattern="SecurityError", instructions="Block IP range."
        )

        # Try listing (forbidden)
        response = engineer_client.get(self.list_url)
        assert response.status_code == status.HTTP_403_FORBIDDEN

        # Try creating (forbidden)
        response = engineer_client.post(
            self.list_url, data={"error_pattern": "Any", "instructions": "Any"}
        )
        assert response.status_code == status.HTTP_403_FORBIDDEN

        # Try deleting (forbidden)
        url = self.get_detail_url(playbook.id)
        response = engineer_client.delete(url)
        assert response.status_code == status.HTTP_403_FORBIDDEN
