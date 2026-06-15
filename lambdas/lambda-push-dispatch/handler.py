import boto3
import json
import os
import time
import logging
import httpx
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

secrets_client = boto3.client('secretsmanager')
dynamodb = boto3.resource('dynamodb')

LOGS_TABLE = 'neuralops-push-logs'
TOKENS_TABLE = 'neuralops-device-tokens'

# ── Module-level cache (persists across warm invocations) ──────────────────
_fcm_token     = None
_fcm_token_exp = 0
_apns_key      = None
_apns_jwt      = None
_apns_jwt_exp  = 0
# ──────────────────────────────────────────────────────────────────────────

# ── FCM helpers ─────────────────────────────────────────────────────────────

def get_fcm_access_token() -> str:
    global _fcm_token, _fcm_token_exp
    if _fcm_token and time.time() < _fcm_token_exp:
        return _fcm_token

    from google.oauth2 import service_account
    import google.auth.transport.requests

    secret   = secrets_client.get_secret_value(SecretId=os.environ['FCM_SECRET_ARN'])
    sa_info  = json.loads(secret['SecretString'])
    creds    = service_account.Credentials.from_service_account_info(
        sa_info,
        scopes=['https://www.googleapis.com/auth/firebase.messaging'],
    )
    creds.refresh(google.auth.transport.requests.Request())

    _fcm_token     = creds.token
    _fcm_token_exp = time.time() + 3300
    return _fcm_token

def send_fcm(device_token: str, notification: dict) -> tuple:
    project_id = os.environ['FCM_PROJECT_ID']
    url = f'https://fcm.googleapis.com/v1/projects/{project_id}/messages:send'

    body = {
        'message': {
            'token': device_token,
            'notification': {
                'title': notification['title'],
                'body':  notification['body'],
            },
            'data': {k: str(v) for k, v in notification['data'].items()},
            'android': {
                'priority': 'high',
                'notification': {
                    'channel_id': 'neuralops_incidents',
                    'sound':      'default',
                },
            },
            'webpush': {
                'fcm_options': {
                    'link': notification['data'].get('deep_link', '/'),
                },
            },
        }
    }

    try:
        resp = httpx.post(
            url,
            json=body,
            headers={'Authorization': f'Bearer {get_fcm_access_token()}'},
            timeout=10.0,
        )
    except httpx.TimeoutException:
        return False, None, 'FCM_TIMEOUT'

    if resp.status_code == 200:
        return True, resp.json().get('name'), None

    error      = resp.json().get('error', {})
    error_code = error.get('details', [{}])[0].get('errorCode', 'UNKNOWN')

    if resp.status_code == 404 or error_code == 'UNREGISTERED':
        return False, None, 'TOKEN_INVALID'

    return False, None, f'FCM_{resp.status_code}_{error_code}'

# ── APNs helpers ─────────────────────────────────────────────────────────────

def get_apns_jwt() -> str:
    global _apns_key, _apns_jwt, _apns_jwt_exp
    if _apns_jwt and time.time() < _apns_jwt_exp:
        return _apns_jwt

    if _apns_key is None:
        apns_arn = os.environ.get('APNS_SECRET_ARN')
        if not apns_arn:
            raise ValueError("APNS_SECRET_ARN is not configured in environment variables.")
            
        secret   = secrets_client.get_secret_value(SecretId=apns_arn)
        _apns_key = json.loads(secret['SecretString'])

    import jwt as pyjwt
    now = int(time.time())
    _apns_jwt = pyjwt.encode(
        {'iss': _apns_key['team_id'], 'iat': now},
        _apns_key['private_key'],
        algorithm='ES256',
        headers={'kid': _apns_key['key_id']},
    )
    _apns_jwt_exp = now + 3300
    return _apns_jwt

def send_apns(device_token: str, notification: dict) -> tuple:
    bundle_id = _apns_key['bundle_id']
    url = f'https://api.push.apple.com/3/device/{device_token}'

    payload = {
        'aps': {
            'alert': {
                'title': notification['title'],
                'body':  notification['body'],
            },
            'sound': 'default',
            'badge': 1,
            'interruption-level': 'critical',
            'content-available':  1,
        },
        **{k: str(v) for k, v in notification['data'].items()},
    }

    try:
        resp = httpx.post(
            url,
            json=payload,
            headers={
                'authorization':   f'bearer {get_apns_jwt()}',
                'apns-topic':      bundle_id,
                'apns-push-type':  'alert',
                'apns-priority':   '10',
                'apns-expiration': str(int(time.time()) + 3600),
            },
            timeout=10.0,
            http2=True,
        )
    except httpx.TimeoutException:
        return False, None, 'APNS_TIMEOUT'

    if resp.status_code == 200:
        return True, resp.headers.get('apns-id'), None

    reason = (resp.json().get('reason') if resp.content else None) or 'Unknown'
    if reason in ('BadDeviceToken', 'Unregistered'):
        return False, None, 'TOKEN_INVALID'

    return False, None, f'APNS_{reason}'

# ── DynamoDB write helpers ─────────────────────────────────────────────────────────

def check_and_reserve_delivery(event_id: str, token_id: str) -> bool:
    """
    Attempts to insert a pending record. If it already exists, returns False.
    This provides SQS idempotency.
    """
    table = dynamodb.Table(LOGS_TABLE)
    try:
        table.put_item(
            Item={
                'event_id': event_id,
                'token_id': token_id,
                'status': 'pending',
                'sent_at': int(time.time())
            },
            ConditionExpression="attribute_not_exists(event_id)"
        )
        return True
    except ClientError as e:
        if e.response['Error']['Code'] == 'ConditionalCheckFailedException':
            return False
        raise

def finalize_delivery(data: dict) -> None:
    """
    Updates the log with the final result.
    """
    table = dynamodb.Table(LOGS_TABLE)
    table.update_item(
        Key={
            'event_id': data['source_event_id'],
            'token_id': data['token_id']
        },
        UpdateExpression="SET tenant_id = :t, user_id = :u, incident_id = :i, #s = :st, provider_message_id = :m, failure_reason = :f, sent_at = :now",
        ExpressionAttributeNames={'#s': 'status'},
        ExpressionAttributeValues={
            ':t': data['tenant_id'],
            ':u': data['user_id'],
            ':i': data['incident_id'],
            ':st': data['status'],
            ':m': data.get('provider_message_id', ''),
            ':f': data.get('failure_reason', ''),
            ':now': int(time.time())
        }
    )

def mark_token_invalid(tenant_id: str, user_id: str, token_id: str) -> None:
    table = dynamodb.Table(TOKENS_TABLE)
    table.update_item(
        Key={
            'tenant_id': tenant_id,
            'sk': f"{user_id}#{token_id}"
        },
        UpdateExpression="SET is_active = :val, updated_at = :now",
        ExpressionAttributeValues={
            ':val': False,
            ':now': int(time.time())
        }
    )

# ── Handler ──────────────────────────────────────────────────────────────────

def handler(event, context):
    failed = []

    for record in event['Records']:
        message_id = record['messageId']
        try:
            body         = json.loads(record['body'])
            provider     = body['provider']
            device_token = body['device_token']
            notification = body['notification']
            token_id     = body['token_id']
            event_id     = body['source_event_id']
            tenant_id    = body['tenant_id']
            user_id      = body['user_id']

            # Idempotency Check
            if not check_and_reserve_delivery(event_id, token_id):
                logger.info(f"Already processing/delivered event {event_id} to token {token_id[:8]}..., skipping")
                continue

            # Call Provider
            if provider == 'fcm':
                success, msg_id, reason = send_fcm(device_token, notification)
            elif provider == 'apns':
                try:
                    success, msg_id, reason = send_apns(device_token, notification)
                except ValueError as e:
                    logger.warning(f"Skipping APNs push: {e}")
                    success, msg_id, reason = False, None, "APNS_NOT_CONFIGURED"
            else:
                logger.error(f"Unknown provider: {provider}")
                continue

            if success:
                status_val = 'sent'
            elif reason == 'TOKEN_INVALID':
                status_val = 'token_invalid'
            else:
                status_val = 'failed'

            # Finalize Log
            finalize_delivery({
                'tenant_id':          tenant_id,
                'user_id':            user_id,
                'token_id':           token_id,
                'source_event_id':    event_id,
                'incident_id':        body['incident_id'],
                'status':             status_val,
                'provider_message_id': msg_id,
                'failure_reason':     reason,
            })

            if status_val == 'token_invalid':
                mark_token_invalid(tenant_id, user_id, token_id)
                logger.info(f"Marked token {token_id[:8]}... as invalid ({reason})")

            elif status_val == 'failed':
                logger.warning(f"Transient failure for token {token_id[:8]}...: {reason}")
                raise RuntimeError(f"Transient push failure: {reason}")

            else:
                logger.info(f"Push sent via {provider} to token {token_id[:8]}... for incident {body['incident_id']}")

        except RuntimeError:
            failed.append({'itemIdentifier': message_id})
        except Exception as exc:
            logger.error(f"Unexpected error for message {message_id}: {exc}", exc_info=True)
            failed.append({'itemIdentifier': message_id})

    return {'batchItemFailures': failed}
