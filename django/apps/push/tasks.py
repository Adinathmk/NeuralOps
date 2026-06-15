import json
import logging
import boto3
from django.conf import settings
from celery import shared_task
from pywebpush import webpush, WebPushException

logger = logging.getLogger(__name__)

@shared_task(bind=True, max_retries=3)
def dispatch_web_push(self, tenant_id: str, payload: dict):
    """
    Sends a Web Push notification to all active devices registered under the given tenant_id.
    """
    if not hasattr(settings, 'VAPID_PRIVATE_KEY') or not settings.VAPID_PRIVATE_KEY:
        logger.warning("VAPID_PRIVATE_KEY not set. Cannot send web push.")
        return

    try:
        dynamodb = boto3.resource(
            'dynamodb',
            region_name=settings.DYNAMODB_REGION,
            aws_access_key_id=settings.DYNAMODB_ACCESS_KEY_ID,
            aws_secret_access_key=settings.DYNAMODB_SECRET_ACCESS_KEY,
            endpoint_url=None
        )
        table = dynamodb.Table('neuralops-device-tokens')
        
        # Query all active devices for the tenant
        response = table.query(
            KeyConditionExpression=boto3.dynamodb.conditions.Key('tenant_id').eq(tenant_id)
        )
        
        items = response.get('Items', [])
        
        for item in items:
            if not item.get('is_active', True):
                continue
                
            subscription_info = json.loads(item['device_token'])
            
            try:
                webpush(
                    subscription_info=subscription_info,
                    data=json.dumps(payload),
                    vapid_private_key=settings.VAPID_PRIVATE_KEY,
                    vapid_claims={
                        "sub": getattr(settings, 'VAPID_SUBJECT', 'mailto:admin@neuralops.com')
                    }
                )
                logger.info(f"Successfully sent push to device {item['sk']}")
            except WebPushException as ex:
                logger.error(f"Web Push failed: {repr(ex)}")
                # If subscription is expired or unsubscribed, we should mark it inactive or delete it
                if ex.response and ex.response.status_code in [404, 410]:
                    table.delete_item(
                        Key={
                            'tenant_id': tenant_id,
                            'sk': item['sk']
                        }
                    )
                    logger.info(f"Deleted expired token {item['sk']}")
            except Exception as e:
                logger.error(f"Failed to send web push: {e}")

    except Exception as exc:
        logger.error(f"Failed to process dispatch_web_push: {exc}")
        raise self.retry(exc=exc, countdown=10)
