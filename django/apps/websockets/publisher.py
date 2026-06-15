import json
import uuid
import logging
import boto3
from botocore.exceptions import ClientError
from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync
from django.conf import settings

logger = logging.getLogger(__name__)

# ── SQS client (lazy-initialised, reused across calls) ─────────────────────
_sqs_client = None

def _get_sqs():
    global _sqs_client
    if _sqs_client is None:
        _sqs_client = boto3.client(
            'sqs',
            region_name=getattr(settings, 'SQS_REGION', 'ap-south-1'),
            aws_access_key_id=getattr(settings, 'DYNAMODB_ACCESS_KEY_ID', None),
            aws_secret_access_key=getattr(settings, 'DYNAMODB_SECRET_ACCESS_KEY', None),
        )
    return _sqs_client


def _publish_to_sqs(tenant_id: str, incident_id: str, severity: str = 'HIGH',
                    error_type: str = 'Alert', service_name: str = 'NeuralOps',
                    environment: str = 'production'):
    """
    Publish an event to the neuralops-push-incidents.fifo SQS queue.
    The lambda-push-router Lambda will pick this up, fan out to all device
    tokens for the tenant, and enqueue individual messages in push-dispatch.
    The lambda-push-dispatch Lambda then sends them via FCM/APNs.
    """
    queue_url = getattr(settings, 'SQS_PUSH_INCIDENTS_QUEUE_URL', '')
    if not queue_url:
        logger.warning("SQS_PUSH_INCIDENTS_QUEUE_URL not configured. Skipping SQS push.")
        return

    event_id = str(uuid.uuid4())
    message = {
        'event_id':       event_id,
        'tenant_id':      tenant_id,
        'incident_id':    incident_id,
        'severity':       severity,
        'error_type':     error_type,
        'service_name':   service_name,
        'environment':    environment,
    }

    try:
        _get_sqs().send_message(
            QueueUrl=queue_url,
            MessageBody=json.dumps(message),
            # FIFO queue requires these two attributes:
            MessageGroupId=tenant_id,
            MessageDeduplicationId=event_id,
        )
        logger.info(f"Queued push notification: incident={incident_id} tenant={tenant_id}")
    except ClientError as e:
        logger.error(f"Failed to publish push to SQS: {e}")


# ── WebSocket publishers ────────────────────────────────────────────────────

def push_incident_update(incident_id: str, data: dict):
    """Push a live incident update to all WebSocket subscribers."""
    channel_layer = get_channel_layer()
    async_to_sync(channel_layer.group_send)(
        f"incident_{incident_id}",
        {
            "type": "incident.update",
            "data": data
        }
    )


def push_incident_analysis_complete(incident_id: str, tenant_id: str, data: dict):
    """
    Push analysis completion to the incident WebSocket channel AND
    queue a background push notification via the SQS → Lambda pipeline.
    """
    channel_layer = get_channel_layer()
    async_to_sync(channel_layer.group_send)(
        f"incident_{incident_id}",
        {"type": "incident.analysis_complete", "data": data}
    )

    # Trigger background push via SQS → lambda-push-router → lambda-push-dispatch
    _publish_to_sqs(
        tenant_id=tenant_id,
        incident_id=incident_id,
        severity=data.get('severity', 'HIGH'),
        error_type=data.get('error_type', 'Analysis Complete'),
        service_name=data.get('service_name', 'NeuralOps AI'),
    )


def push_notification(tenant_id: str, user_id: str, data: dict):
    """
    Push a notification to a specific user via:
    1. WebSocket (live, if the user is online)
    2. SQS → Lambda push pipeline (background, even if browser is closed)
    """
    channel_layer = get_channel_layer()
    async_to_sync(channel_layer.group_send)(
        f"notifications_{tenant_id}_{user_id}",
        {"type": "notification.new", "data": data}
    )

    # Trigger background push via SQS → lambda-push-router → lambda-push-dispatch
    incident_id = data.get('incident_id', data.get('id', f"notif-{uuid.uuid4()}"))
    _publish_to_sqs(
        tenant_id=tenant_id,
        incident_id=str(incident_id),
        severity=data.get('severity', 'HIGH'),
        error_type=data.get('title', 'New Notification'),
        service_name=data.get('service_name', 'NeuralOps'),
    )


def push_collaboration_event(tenant_id: str, event_type: str, data: dict):
    """Push a collaboration event (message, mention, assignment) to a tenant."""
    channel_layer = get_channel_layer()
    async_to_sync(channel_layer.group_send)(
        f"collaboration_{tenant_id}",
        {"type": f"collaboration.{event_type}", "data": data}
    )
