import boto3
import json
import os
import logging
from boto3.dynamodb.conditions import Key, Attr

logger = logging.getLogger()
logger.setLevel(logging.INFO)

sqs = boto3.client('sqs')
DISPATCH_QUEUE_URL = os.environ['DISPATCH_QUEUE_URL']

dynamodb = boto3.resource('dynamodb')
TOKENS_TABLE = 'neuralops-device-tokens'

def get_active_tokens_for_tenant(tenant_id: str) -> list[dict]:
    table = dynamodb.Table(TOKENS_TABLE)
    
    response = table.query(
        KeyConditionExpression=Key('tenant_id').eq(tenant_id),
        FilterExpression=Attr('is_active').eq(True) & Attr('role').is_in(['engineer', 'admin', 'owner'])
    )
    tokens = response.get('Items', [])
    
    while 'LastEvaluatedKey' in response:
        response = table.query(
            KeyConditionExpression=Key('tenant_id').eq(tenant_id),
            FilterExpression=Attr('is_active').eq(True) & Attr('role').is_in(['engineer', 'admin', 'owner']),
            ExclusiveStartKey=response['LastEvaluatedKey']
        )
        tokens.extend(response.get('Items', []))
        
    formatted_tokens = []
    for t in tokens:
        user_id, device_id = t['sk'].split('#', 1)
        formatted_tokens.append({
            'user_id': user_id,
            'token_id': device_id,
            'platform': t.get('platform', 'unknown'),
            'provider': t.get('provider', 'fcm'),
            'device_token': t.get('device_token')
        })
        
    return formatted_tokens


def build_notification_payload(body: dict) -> dict:
    severity      = body.get('severity', 'UNKNOWN').upper()
    error_type    = body.get('error_type', 'Error')
    service_name  = body.get('service_name', 'service')
    incident_id   = body.get('incident_id', '')

    emoji = {'CRITICAL': '🔴', 'HIGH': '🟠', 'MEDIUM': '🟡', 'LOW': '🟢'}.get(severity, '🚨')

    return {
        'title': f"{emoji} [{severity}] Production Incident",
        'body':  f"{error_type} in {service_name}",
        'data': {
            'type':        'incident.created',
            'incident_id': incident_id,
            'deep_link':   f"/incidents/{incident_id}",
        },
    }


def handler(event, context):
    failed = []

    for record in event['Records']:
        message_id = record['messageId']
        try:
            body        = json.loads(record['body'])
            tenant_id   = body['tenant_id']
            incident_id = body['incident_id']
            event_id    = body['event_id']

            tokens = get_active_tokens_for_tenant(tenant_id)

            if not tokens:
                logger.info(f"No active tokens for tenant {tenant_id}, incident {incident_id}. Nothing to send.")
                continue

            notification = build_notification_payload(body)
            logger.info(f"Fanning out to {len(tokens)} device(s) for incident {incident_id}")

            dispatch_messages = [
                {
                    'source_event_id': event_id,
                    'incident_id':     incident_id,
                    'tenant_id':       tenant_id,
                    'user_id':         t['user_id'],
                    'token_id':        t['token_id'],
                    'platform':        t['platform'],
                    'provider':        t['provider'],
                    'device_token':    t['device_token'],
                    'notification':    notification,
                }
                for t in tokens
            ]

            for i in range(0, len(dispatch_messages), 10):
                batch = dispatch_messages[i:i + 10]
                sqs.send_message_batch(
                    QueueUrl=DISPATCH_QUEUE_URL,
                    Entries=[
                        {
                            'Id':          str(j),
                            'MessageBody': json.dumps(msg),
                        }
                        for j, msg in enumerate(batch)
                    ],
                )

        except Exception as exc:
            logger.error(f"Router failed for message {message_id}: {exc}", exc_info=True)
            failed.append({'itemIdentifier': message_id})

    return {'batchItemFailures': failed}
