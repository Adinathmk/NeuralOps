import boto3
import json
import base64
import os
import logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)

sqs = boto3.client('sqs')
QUEUE_URL = os.environ['INCIDENTS_QUEUE_URL']


def handler(event, context):
    """
    Triggered by MSK (Managed Kafka) event source mapping on topic incidents.created.
    event['records'] is keyed by 'topic-partition', value is a list of messages.
    Each message value is base64-encoded bytes of the Kafka message.
    """
    for topic_partition, messages in event['records'].items():
        for msg in messages:
            # Decode the Kafka message value from base64
            raw_bytes = base64.b64decode(msg['value'])
            kafka_envelope = json.loads(raw_bytes.decode('utf-8'))

            # The envelope follows the format defined in Section 9 of the docs:
            # { event_id, event_type, idempotency_key, source_version, occurred_at, payload }
            event_type = kafka_envelope.get('event_type', '')
            event_id   = kafka_envelope.get('event_id')

            # Only care about new incidents, not updates
            if event_type != 'incident.created':
                logger.info(f"Skipping {event_type} ({event_id})")
                continue

            incident = kafka_envelope.get('payload', {})
            tenant_id   = incident.get('tenant_id')
            incident_id = incident.get('id') or incident.get('incident_id')

            if not tenant_id or not incident_id:
                logger.error(f"Missing tenant_id or incident_id in event {event_id}")
                continue

            # Write to SQS FIFO.
            # MessageGroupId = tenant_id  → incidents from the same tenant stay ordered.
            # MessageDeduplicationId = event_id → Kafka at-least-once delivery
            #   can never cause the same incident to be pushed twice.
            sqs.send_message(
                QueueUrl=QUEUE_URL,
                MessageBody=json.dumps({
                    'event_id':       event_id,
                    'tenant_id':      tenant_id,
                    'incident_id':    incident_id,
                    'severity':       incident.get('severity', 'UNKNOWN'),
                    'error_type':     incident.get('error_type', 'Error'),
                    'service_name':   incident.get('service_name', 'unknown'),
                    'environment':    incident.get('environment', 'production'),
                    'confidence_score': incident.get('confidence_score'),
                }),
                MessageGroupId=tenant_id,
                MessageDeduplicationId=event_id,
            )

            logger.info(f"Queued push: incident={incident_id} tenant={tenant_id}")
