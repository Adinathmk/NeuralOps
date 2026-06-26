import logging

import razorpay
from celery import shared_task
from django.conf import settings
from tenants.models import Tenant

logger = logging.getLogger(__name__)
client = razorpay.Client(auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET))


@shared_task
def sync_subscription_statuses():
    tenants = Tenant.objects.exclude(razorpay_subscription_id__isnull=True).exclude(
        razorpay_subscription_id=""
    )
    for tenant in tenants:
        try:
            subscription = client.subscription.fetch(tenant.razorpay_subscription_id)
            status = subscription.get("status")
            if status in ["active", "authenticated"]:
                if tenant.status != "active":
                    tenant.status = "active"
                    tenant.save(update_fields=["status"])
            elif status in ["halted", "cancelled", "expired"]:
                if tenant.status != "suspended":
                    tenant.status = "suspended"
                    tenant.save(update_fields=["status"])
        except Exception as e:
            logger.error(f"Failed to sync subscription for tenant {tenant.id}: {e}")
