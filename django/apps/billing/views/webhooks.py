import hmac
import hashlib
import json
import redis
from django.conf import settings
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import AllowAny
from billing.models import BillingEvent
from tenants.models import Tenant

redis_client = redis.from_url(settings.REDIS_URL, decode_responses=True)

class RazorpayWebhookView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        # 1. Verify signature
        secret = settings.RAZORPAY_WEBHOOK_SECRET
        signature = request.headers.get('X-Razorpay-Signature', '')
        
        expected_signature = hmac.new(
            secret.encode('utf-8'),
            request.body,
            hashlib.sha256
        ).hexdigest()
        
        if not hmac.compare_digest(expected_signature, signature):
            return Response({"error": "Invalid signature"}, status=status.HTTP_400_BAD_REQUEST)
            
        payload = request.data
        event_name = payload.get('event')
        event_id = request.headers.get('X-Razorpay-Event-Id') or payload.get('account_id', '') + event_name
        
        # 2. Idempotency Check
        if BillingEvent.objects.filter(razorpay_event_id=event_id).exists():
            return Response({"status": "already processed"}, status=status.HTTP_200_OK)
            
        # Log event
        BillingEvent.objects.create(
            razorpay_event_id=event_id,
            event_type=event_name,
            payload=payload
        )
        
        # 3. Process event
        subscription_payload = payload.get('payload', {}).get('subscription', {}).get('entity', {})
        tenant_id = subscription_payload.get('notes', {}).get('tenant_id')
        
        if not tenant_id:
            # Maybe payment.failed doesn't have it directly. Let's try to find it.
            return Response({"status": "no tenant id"}, status=status.HTTP_200_OK)
            
        try:
            tenant = Tenant.objects.get(id=tenant_id)
        except Tenant.DoesNotExist:
            return Response({"error": "Tenant not found"}, status=status.HTTP_404_NOT_FOUND)
            
        if event_name in ['subscription.charged', 'subscription.activated']:
            plan_tier = subscription_payload.get('notes', {}).get('plan_tier', tenant.plan_tier)
            tenant.plan_tier = plan_tier
            tenant.status = 'active'
            tenant.save(update_fields=['plan_tier', 'status'])
            # Remove any prior suspension flag
            redis_client.delete(f"tenant:{tenant.id}:suspended")

        elif event_name == 'subscription.cancelled':
            # User cancelled voluntarily — downgrade to free, do NOT suspend.
            # They can still use the product within free tier limits.
            tenant.plan_tier = 'free'
            tenant.save(update_fields=['plan_tier'])

            # Downgrade resources to free tier limits
            from core.quotas import PLAN_QUOTAS
            from django.apps import apps
            User = apps.get_model('users', 'User')
            APIKey = apps.get_model('users', 'APIKey')

            free_quota = PLAN_QUOTAS['free']

            # Deactivate excess users (keep the oldest ones active)
            if free_quota.max_users != -1:
                active_users = list(User.objects.filter(tenant=tenant, is_active=True).order_by('created_at'))
                if len(active_users) > free_quota.max_users:
                    excess_users = active_users[free_quota.max_users:]
                    for u in excess_users:
                        u.is_active = False
                    User.objects.bulk_update(excess_users, ['is_active'])

            # Deactivate excess API keys (keep the oldest ones active)
            if free_quota.max_api_keys != -1:
                active_keys = list(APIKey.objects.filter(tenant=tenant, is_active=True).order_by('created_at'))
                if len(active_keys) > free_quota.max_api_keys:
                    excess_keys = active_keys[free_quota.max_api_keys:]
                    for k in excess_keys:
                        k.is_active = False
                    APIKey.objects.bulk_update(excess_keys, ['is_active'])

        elif event_name == 'subscription.halted':
            # Razorpay has exhausted all automatic payment retries and given up.
            # Only NOW do we suspend the account.
            tenant.status = 'suspended'
            tenant.save(update_fields=['status'])
            redis_client.set(f"tenant:{tenant.id}:suspended", "true")

        elif event_name == 'payment.failed':
            # A single payment failure — Razorpay will retry automatically.
            # Just log it (already done above). Do NOT suspend yet.
            # Optionally: send a "payment failed" email to the tenant here.
            pass

        elif event_name == 'subscription.resumed':
            # Tenant resumed after halting — restore to active + paid tier.
            plan_tier = subscription_payload.get('notes', {}).get('plan_tier', tenant.plan_tier)
            tenant.plan_tier = plan_tier
            tenant.status = 'active'
            tenant.save(update_fields=['plan_tier', 'status'])
            redis_client.delete(f"tenant:{tenant.id}:suspended")

        return Response({"status": "success"}, status=status.HTTP_200_OK)

