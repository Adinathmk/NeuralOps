import json
import logging
from uuid import UUID

from django.core.cache import cache
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver
from tenants.models import Tenant
from outbox.models import OutboxEvent

logger = logging.getLogger(__name__)

# Need a redis client for token bucket keys that aren't in Django's default cache
import redis
from django.conf import settings
redis_client = redis.from_url(settings.REDIS_URL, decode_responses=True)

class UUIDEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, UUID):
            return str(obj)
        return super().default(obj)

@receiver(pre_save, sender=Tenant)
def capture_old_plan_tier(sender, instance, **kwargs):
    if instance.pk:
        try:
            old_instance = Tenant.objects.get(pk=instance.pk)
            instance._old_plan_tier = old_instance.plan_tier
        except Tenant.DoesNotExist:
            instance._old_plan_tier = None
    else:
        instance._old_plan_tier = None

@receiver(post_save, sender=Tenant)
def invalidate_cache_on_plan_change(sender, instance, created, **kwargs):
    old_plan = getattr(instance, "_old_plan_tier", None)
    
    # Invalidate cache if plan_tier changed
    if old_plan != instance.plan_tier:
        tenant_id_str = str(instance.id)
        
        # 1. Clear DRF throttle cache
        cache.delete(f"rate_limit_{tenant_id_str}")
        
        # 2. Clear Billing bucket
        redis_client.delete(f"rl:billing:{tenant_id_str}")
        
        # 3. Clear FastAPI L1 snapshot cache
        redis_client.delete(f"tenant:config:{tenant_id_str}")
        
        # 4. Write outbox event for Kafka (FastAPI will invalidate its own rl:ingest keys)
        payload = {
            "event_type": "tenant.updated",
            "tenant": {
                "id": tenant_id_str,
                "plan_tier": instance.plan_tier,
                "vector_namespace": instance.vector_namespace,
                "kafka_group_id": instance.kafka_group_id,
                "is_suspended": instance.is_suspended,
                "source_version": 1  # Simplified for signal, ideally comes from version field
            }
        }
        
        OutboxEvent.objects.create(
            topic="config.tenants",
            key=tenant_id_str,
            payload=json.loads(json.dumps(payload, cls=UUIDEncoder))
        )
        
        logger.info(f"Invalidated rate limit cache for tenant {tenant_id_str} due to plan tier change")
