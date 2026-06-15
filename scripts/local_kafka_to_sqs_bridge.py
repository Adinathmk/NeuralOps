#!/usr/bin/env python3
import os
import json
import logging
import time
import boto3
from confluent_kafka import Consumer, KafkaException, KafkaError

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("kafka-sqs-bridge")

KAFKA_BROKER = os.getenv('KAFKA_BOOTSTRAP_SERVERS', 'localhost:9092')
TOPIC = 'incidents.created'
QUEUE_URL = os.getenv('INCIDENTS_QUEUE_URL')

if not QUEUE_URL:
    logger.error("INCIDENTS_QUEUE_URL environment variable is required.")
    exit(1)

sqs = boto3.client('sqs', region_name=os.getenv('AWS_REGION', 'us-east-1'))

def main():
    consumer = Consumer({
        'bootstrap.servers': KAFKA_BROKER,
        'group.id': 'local-kafka-sqs-bridge',
        'auto.offset.reset': 'earliest'
    })
    
    consumer.subscribe([TOPIC])
    logger.info(f"Subscribed to {TOPIC} on {KAFKA_BROKER}. Forwarding to {QUEUE_URL}")

    try:
        while True:
            msg = consumer.poll(timeout=1.0)
            if msg is None:
                continue
            if msg.error():
                if msg.error().code() == KafkaError._PARTITION_EOF:
                    continue
                else:
                    logger.error(msg.error())
                    break

            try:
                payload = json.loads(msg.value().decode('utf-8'))
                event_type = payload.get('event_type')
                event_id = payload.get('event_id')

                if event_type != 'incident.created':
                    continue

                incident = payload.get('payload', {})
                tenant_id = incident.get('tenant_id')
                incident_id = incident.get('id') or incident.get('incident_id')

                if not tenant_id or not incident_id:
                    continue

                # Forward to SQS
                sqs.send_message(
                    QueueUrl=QUEUE_URL,
                    MessageBody=json.dumps({
                        'event_id': event_id,
                        'tenant_id': tenant_id,
                        'incident_id': incident_id,
                        'severity': incident.get('severity', 'UNKNOWN'),
                        'error_type': incident.get('error_type', 'Error'),
                        'service_name': incident.get('service_name', 'unknown'),
                        'environment': incident.get('environment', 'production'),
                        'confidence_score': incident.get('confidence_score'),
                    }),
                    MessageGroupId=tenant_id,
                    MessageDeduplicationId=event_id,
                )
                logger.info(f"Forwarded incident {incident_id} to SQS.")
            except Exception as e:
                logger.error(f"Failed to process message: {e}")
                
    except KeyboardInterrupt:
        logger.info("Stopping bridge...")
    finally:
        consumer.close()

if __name__ == '__main__':
    main()
