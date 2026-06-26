import hashlib
import hmac
import json
from unittest.mock import MagicMock, patch

import razorpay
from billing.models import BillingEvent
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient
from tenants.models import Tenant


@override_settings(RAZORPAY_WEBHOOK_SECRET="test_secret")
class RazorpayWebhookTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.tenant = Tenant.objects.create(name="Test", slug="test", plan_tier="free")
        self.webhook_url = reverse("webhook_razorpay")

    def generate_signature(self, payload):
        secret = "test_secret"
        return hmac.new(
            secret.encode("utf-8"), json.dumps(payload).encode("utf-8"), hashlib.sha256
        ).hexdigest()

    def test_valid_signature_charged(self):
        payload = {
            "event": "subscription.charged",
            "payload": {
                "subscription": {
                    "entity": {
                        "notes": {"tenant_id": str(self.tenant.id), "plan_tier": "pro"}
                    }
                }
            },
        }
        signature = self.generate_signature(payload)

        response = self.client.post(
            self.webhook_url,
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_X_RAZORPAY_SIGNATURE=signature,
            HTTP_X_RAZORPAY_EVENT_ID="evt_123",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.plan_tier, "pro")
        self.assertTrue(
            BillingEvent.objects.filter(razorpay_event_id="evt_123").exists()
        )

    def test_invalid_signature(self):
        response = self.client.post(
            self.webhook_url,
            data=json.dumps({"event": "subscription.charged"}),
            content_type="application/json",
            HTTP_X_RAZORPAY_SIGNATURE="invalid",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_subscription_halted(self):
        payload = {
            "event": "subscription.halted",
            "payload": {
                "subscription": {
                    "entity": {"notes": {"tenant_id": str(self.tenant.id)}}
                }
            },
        }
        signature = self.generate_signature(payload)
        response = self.client.post(
            self.webhook_url,
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_X_RAZORPAY_SIGNATURE=signature,
            HTTP_X_RAZORPAY_EVENT_ID="evt_456",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.status, "suspended")
