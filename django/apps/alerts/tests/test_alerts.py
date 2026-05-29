import uuid

import pytest
from alerts.models import AlertRule
from outbox.models import OutboxEvent
from rest_framework import status


@pytest.mark.django_db
class TestAlertRuleAPI:
    """
    Precision REST API integration tests for AlertRuleViewSet.
    Exposes and validates CRUD operations, confidence bounds, severity lists,
    UUID checks on recipients, role security, and Kafka outbox event logging.
    """

    @pytest.fixture(autouse=True)
    def setup_url(self):
        self.list_url = "/api/alerts/alert-rules/"

    def get_detail_url(self, rule_id):
        return f"{self.list_url}{rule_id}/"

    # ── 1. Create Alert Rule Tests ─────────────────────────────────────────────

    def test_create_alert_rule_success(self, admin_client, tenant):
        """Verify tenant admin can successfully create an alert rule and write an outbox event."""
        payload = {
            "confidence_threshold": 0.85,
            "severity_filter": ["critical", "high"],
            "recipient_ids": [str(uuid.uuid4()), str(uuid.uuid4())],
            "enabled": True,
        }

        response = admin_client.post(self.list_url, data=payload, format="json")
        assert response.status_code == status.HTTP_201_CREATED

        data = response.data["data"]
        assert data["confidence_threshold"] == payload["confidence_threshold"]
        assert data["severity_filter"] == payload["severity_filter"]
        assert data["recipient_ids"] == payload["recipient_ids"]
        assert data["enabled"] is True

        # Verify db persistence
        rule = AlertRule.objects.get(pk=data["id"])
        assert rule.tenant == tenant

        # Verify transactional outbox event written correctly for CDC Kafka dispatch
        outbox = OutboxEvent.objects.filter(topic="config.alert_rules").latest(
            "created_at"
        )
        assert outbox.key == str(tenant.id)
        assert outbox.payload["event_type"] == "alert_rule.created"
        assert outbox.payload["alert_rule"]["id"] == str(rule.id)

    def test_create_alert_rule_invalid_confidence(self, admin_client):
        """Verify that confidence thresholds outside [0.0, 1.0] are rejected."""
        payload = {
            "confidence_threshold": 1.2,  # Invalid
            "severity_filter": ["critical"],
            "recipient_ids": [str(uuid.uuid4())],
        }

        response = admin_client.post(self.list_url, data=payload, format="json")
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "confidence_threshold" in response.data["errors"]

    def test_create_alert_rule_invalid_severity(self, admin_client):
        """Verify that invalid severity filter keys are rejected by validation constraints."""
        payload = {
            "confidence_threshold": 0.5,
            "severity_filter": ["critical", "super_alert"],  # "super_alert" invalid
            "recipient_ids": [str(uuid.uuid4())],
        }

        response = admin_client.post(self.list_url, data=payload, format="json")
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "severity_filter" in response.data["errors"]

    def test_create_alert_rule_invalid_recipients(self, admin_client):
        """Verify that malformed recipient UUIDs are rejected."""
        payload = {
            "confidence_threshold": 0.5,
            "severity_filter": ["medium"],
            "recipient_ids": [
                "not-a-valid-uuid",
                str(uuid.uuid4()),
            ],  # Invalid recipient
        }

        response = admin_client.post(self.list_url, data=payload, format="json")
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "recipient_ids" in response.data["errors"]

    # ── 2. List & Scope Tests ─────────────────────────────────────────────────

    def test_list_alert_rules_scoped_to_tenant(
        self, admin_client, owner_client, tenant, tenant_2
    ):
        """Verify list only returns alert rules owned by the authenticated tenant."""
        r1 = AlertRule.objects.create(
            tenant=tenant,
            confidence_threshold=0.90,
            severity_filter=["critical"],
            recipient_ids=[str(uuid.uuid4())],
        )
        r2 = AlertRule.objects.create(
            tenant=tenant_2,
            confidence_threshold=0.80,
            severity_filter=["low"],
            recipient_ids=[str(uuid.uuid4())],
        )

        # List as primary tenant admin
        response = admin_client.get(self.list_url)
        assert response.status_code == status.HTTP_200_OK
        data = response.data["data"]

        assert len(data) == 1
        assert data[0]["id"] == str(r1.id)
        assert data[0]["confidence_threshold"] == 0.90

    # ── 3. Retrieve Tests ─────────────────────────────────────────────────────

    def test_retrieve_alert_rule_success(self, owner_client, tenant):
        """Verify that a tenant owner can retrieve a specific alert rule."""
        rule = AlertRule.objects.create(
            tenant=tenant,
            confidence_threshold=0.50,
            severity_filter=["medium"],
            recipient_ids=[str(uuid.uuid4())],
        )

        url = self.get_detail_url(rule.id)
        response = owner_client.get(url)

        assert response.status_code == status.HTTP_200_OK
        assert response.data["data"]["id"] == str(rule.id)

    def test_retrieve_alert_rule_not_found(self, admin_client):
        """Verify retrieving non-existent PK yields a 404 response."""
        url = self.get_detail_url(uuid.uuid4())
        response = admin_client.get(url)
        assert response.status_code == status.HTTP_404_NOT_FOUND

    # ── 4. Update & Patch Tests ───────────────────────────────────────────────

    def test_update_alert_rule_success(self, admin_client, tenant):
        """Verify full update (PUT) triggers model rewrite and logs outbox update event."""
        rule = AlertRule.objects.create(
            tenant=tenant,
            confidence_threshold=0.70,
            severity_filter=["high"],
            recipient_ids=[str(uuid.uuid4())],
        )

        payload = {
            "confidence_threshold": 0.95,
            "severity_filter": ["critical"],
            "recipient_ids": [str(uuid.uuid4()), str(uuid.uuid4())],
            "enabled": False,
        }

        url = self.get_detail_url(rule.id)
        response = admin_client.put(url, data=payload, format="json")
        assert response.status_code == status.HTTP_200_OK

        rule.refresh_from_db()
        assert rule.confidence_threshold == payload["confidence_threshold"]
        assert rule.severity_filter == payload["severity_filter"]
        assert rule.enabled is False

        # Verify outbox event logged
        outbox = OutboxEvent.objects.filter(topic="config.alert_rules").latest(
            "created_at"
        )
        assert outbox.payload["event_type"] == "alert_rule.updated"
        assert outbox.payload["alert_rule"]["id"] == str(rule.id)

    def test_partial_update_alert_rule_success(self, admin_client, tenant):
        """Verify partial update (PATCH) rewrites single fields correctly."""
        rule = AlertRule.objects.create(
            tenant=tenant,
            confidence_threshold=0.70,
            severity_filter=["high"],
            recipient_ids=[str(uuid.uuid4())],
        )

        payload = {"enabled": False}

        url = self.get_detail_url(rule.id)
        response = admin_client.patch(url, data=payload, format="json")
        assert response.status_code == status.HTTP_200_OK

        rule.refresh_from_db()
        assert rule.confidence_threshold == 0.70  # Unchanged
        assert rule.enabled is False

    # ── 5. Destroy Tests ──────────────────────────────────────────────────────

    def test_delete_alert_rule_success(self, admin_client, tenant):
        """Verify delete removes row and publishes deletion event to outbox."""
        rule = AlertRule.objects.create(
            tenant=tenant,
            confidence_threshold=0.88,
            severity_filter=["low"],
            recipient_ids=[str(uuid.uuid4())],
        )

        url = self.get_detail_url(rule.id)
        response = admin_client.delete(url)
        assert response.status_code == status.HTTP_200_OK

        # Verify deleted from DB
        assert not AlertRule.objects.filter(pk=rule.id).exists()

        # Verify outbox deletion event logged
        outbox = OutboxEvent.objects.filter(topic="config.alert_rules").latest(
            "created_at"
        )
        assert outbox.payload["event_type"] == "alert_rule.deleted"
        assert outbox.payload["alert_rule"]["deleted"] is True
        assert outbox.payload["alert_rule"]["id"] == str(rule.id)

    # ── 6. Permissions / Guard Boundaries ─────────────────────────────────────

    def test_forbidden_for_engineer_user(self, engineer_client, tenant):
        """Verify that a basic tenant engineer is forbidden from viewing or writing alert rules."""
        rule = AlertRule.objects.create(
            tenant=tenant,
            confidence_threshold=0.50,
            severity_filter=["medium"],
            recipient_ids=[str(uuid.uuid4())],
        )

        # Try listing (forbidden)
        response = engineer_client.get(self.list_url)
        assert response.status_code == status.HTTP_403_FORBIDDEN

        # Try creating (forbidden)
        response = engineer_client.post(
            self.list_url,
            data={
                "confidence_threshold": 0.5,
                "severity_filter": ["medium"],
                "recipient_ids": [str(uuid.uuid4())],
            },
        )
        assert response.status_code == status.HTTP_403_FORBIDDEN

        # Try deleting (forbidden)
        url = self.get_detail_url(rule.id)
        response = engineer_client.delete(url)
        assert response.status_code == status.HTTP_403_FORBIDDEN
