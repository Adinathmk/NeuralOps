import pytest
from outbox.models import OutboxEvent
from rest_framework import status
from tenants.models import TenantConfiguration
from users.models import AuditLog


@pytest.mark.django_db
class TestTenantConfigAPI:
    """
    Precision REST API integration tests for TenantConfigView.
    Validates GET/PATCH tenant configuration, Redis read-through and invalidation,
    validation boundaries, AuditLog tracking, Outbox event logging, and role gates.
    """

    @pytest.fixture(autouse=True)
    def setup_url(self):
        self.url = "/api/v1/tenant/config/"

    # ── 1. Retrieval (GET) Tests ──────────────────────────────────────────────

    def test_get_config_not_found(self, admin_client):
        """Verify that retrieving a non-existent configuration yields a 404 response."""
        response = admin_client.get(self.url)
        assert response.status_code == status.HTTP_404_NOT_FOUND
        assert response.data["code"] == "not_found"

    def test_get_config_success_and_cache(self, admin_client, tenant):
        """Verify GET config retrieves database settings and populates the Redis cache."""
        config = TenantConfiguration.objects.create(
            tenant=tenant,
            alert_confidence_threshold=0.85,
            log_retention_days=30,
            enable_email_notifications=True,
            metadata={"environment": "production"},
        )

        # First request (Cache Miss -> Read from DB -> Populate cache)
        response = admin_client.get(self.url)
        assert response.status_code == status.HTTP_200_OK
        assert "retrieved successfully" in response.data["message"]

        data = response.data["data"]
        assert data["alert_confidence_threshold"] == 0.85
        assert data["log_retention_days"] == 30
        assert data["enable_email_notifications"] is True

        # Second request (Cache Hit -> Read from Redis)
        response2 = admin_client.get(self.url)
        assert response2.status_code == status.HTTP_200_OK
        assert "retrieved from cache" in response2.data["message"]

    # ── 2. Updates (PATCH) Tests ──────────────────────────────────────────────

    def test_patch_config_success(self, admin_client, tenant, admin_user):
        """Verify successful PATCH updates DB, writes AuditLog, publishes Outbox, and updates cache."""
        config = TenantConfiguration.objects.create(
            tenant=tenant,
            alert_confidence_threshold=0.50,
            log_retention_days=15,
            enable_email_notifications=False,
        )

        payload = {
            "alert_confidence_threshold": 0.95,
            "log_retention_days": 90,
            "enable_email_notifications": True,
        }

        response = admin_client.patch(self.url, data=payload, format="json")
        assert response.status_code == status.HTTP_200_OK

        # Verify db updated
        config.refresh_from_db()
        assert config.alert_confidence_threshold == 0.95
        assert config.log_retention_days == 90
        assert config.enable_email_notifications is True

        # Verify AuditLog created
        audit = AuditLog.objects.latest("created_at")
        assert audit.action == "TENANT_CONFIG_UPDATED"
        assert audit.user == admin_user
        assert "alert_confidence_threshold" in audit.description

        # Verify Outbox CDC event
        outbox = OutboxEvent.objects.filter(topic="config.tenants.updated").latest(
            "created_at"
        )
        assert outbox.key == f"tenant:{tenant.id}"
        assert outbox.payload["config"]["log_retention_days"] == 90

        # Verify subsequent GET hits updated Redis cache
        response_get = admin_client.get(self.url)
        assert response_get.status_code == status.HTTP_200_OK
        assert "retrieved from cache" in response_get.data["message"]
        assert response_get.data["data"]["log_retention_days"] == 90

    def test_patch_config_invalid_threshold(self, admin_client, tenant):
        """Verify that confidence thresholds outside [0.0, 1.0] are rejected by validation constraints."""
        TenantConfiguration.objects.create(
            tenant=tenant,
            alert_confidence_threshold=0.50,
            log_retention_days=15,
            enable_email_notifications=False,
        )

        payload = {"alert_confidence_threshold": -0.5}  # Invalid threshold

        response = admin_client.patch(self.url, data=payload, format="json")
        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_patch_config_invalid_retention(self, admin_client, tenant):
        """Verify that retention days outside [1, 3650] are rejected."""
        TenantConfiguration.objects.create(
            tenant=tenant,
            alert_confidence_threshold=0.50,
            log_retention_days=15,
            enable_email_notifications=False,
        )

        payload = {"log_retention_days": 0}  # Invalid retention (min is 1)

        response = admin_client.patch(self.url, data=payload, format="json")
        assert response.status_code == status.HTTP_400_BAD_REQUEST

    # ── 3. Permissions / Guard Boundaries ─────────────────────────────────────

    def test_forbidden_for_engineer_user(self, engineer_client, tenant):
        """Verify that a basic tenant engineer is forbidden from viewing or changing config."""
        TenantConfiguration.objects.create(
            tenant=tenant,
            alert_confidence_threshold=0.50,
            log_retention_days=15,
            enable_email_notifications=False,
        )

        # Try GET (forbidden)
        response = engineer_client.get(self.url)
        assert response.status_code == status.HTTP_403_FORBIDDEN

        # Try PATCH (forbidden)
        response = engineer_client.patch(self.url, data={"log_retention_days": 30})
        assert response.status_code == status.HTTP_403_FORBIDDEN
