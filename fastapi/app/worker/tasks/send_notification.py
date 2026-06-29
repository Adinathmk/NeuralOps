"""
fastapi/app/worker/tasks/send_notification.py

Celery task for executing HTTP webhooks to external services like PagerDuty and Slack.
"""

import asyncio
import os
import uuid
import logging

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.database.session import AsyncSessionLocal
from app.models.incidents import Incident, NotificationDelivery
from app.utils.security import is_safe_webhook_url
from app.worker.celery_app import celery_app

logger = get_logger(__name__)

FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://localhost:3000")


async def _execute_send_notification(delivery_id_str: str) -> None:
    try:
        delivery_id = uuid.UUID(delivery_id_str)
    except ValueError:
        logger.error("invalid_delivery_id_format", extra={"delivery_id": delivery_id_str})
        return

    async with AsyncSessionLocal() as session:
        stmt = select(NotificationDelivery).where(NotificationDelivery.id == delivery_id)
        result = await session.execute(stmt)
        delivery = result.scalar_one_or_none()

        if not delivery:
            logger.error("notification_delivery_not_found", extra={"delivery_id": delivery_id_str})
            return

        if delivery.status in ("delivered", "failed"):
            return

        # Fetch incident
        stmt = select(Incident).where(Incident.id == delivery.incident_id)
        incident = (await session.execute(stmt)).scalar_one_or_none()

        if not incident:
            logger.error("incident_not_found_for_delivery", extra={"delivery_id": delivery_id_str})
            delivery.status = "failed"
            await session.commit()
            return

        dtype = delivery.destination_type
        config = delivery.destination_config or {}

        if dtype not in ("pagerduty", "slack"):
            logger.warning("unsupported_destination_type", extra={"type": dtype})
            delivery.status = "failed"
            await session.commit()
            return

        webhook_url = config.get("webhook_url")
        integration_key = config.get("integration_key")
        
        target_url = ""
        if dtype == "slack":
            if not webhook_url or not is_safe_webhook_url(webhook_url):
                logger.error("unsafe_or_missing_webhook_url", extra={"url": webhook_url})
                delivery.status = "failed"
                await session.commit()
                return
            target_url = webhook_url
        elif dtype == "pagerduty":
            if not integration_key:
                logger.error("missing_integration_key")
                delivery.status = "failed"
                await session.commit()
                return
            target_url = "https://events.pagerduty.com/v2/enqueue"

        incident_url = f"{FRONTEND_URL}/dashboard/incidents/{incident.id}"

        # ── PagerDuty ──────────────────────────────────────────────────────────
        if dtype == "pagerduty":
            routing_key = integration_key

            payload = {
                "routing_key": routing_key,
                "event_action": "trigger",
                "payload": {
                    "summary": f"[{incident.severity.upper()}] {incident.error_type} in {incident.service_name}",
                    "severity": incident.severity if incident.severity in ("critical", "error", "warning", "info") else "error",
                    "source": incident.service_name,
                    "custom_details": {
                        "environment": incident.environment,
                        "incident_id": str(incident.id),
                        "neuralops_url": incident_url,
                    }
                },
                "links": [
                    {
                        "href": incident_url,
                        "text": "Open in NeuralOps"
                    }
                ]
            }
            # For standard webhooks not using the native routing_key enqueue URL
            if not payload["routing_key"]:
                payload = {
                    "incident": {
                        "type": "incident",
                        "title": f"[{incident.severity.upper()}] {incident.error_type} in {incident.service_name}",
                        "service": {"id": "neuralops", "type": "service_reference"},
                        "body": {
                            "type": "incident_body",
                            "details": f"Environment: {incident.environment}\nLink: {incident_url}"
                        }
                    }
                }

        # ── Slack ──────────────────────────────────────────────────────────────
        elif dtype == "slack":
            short_error_msg = incident.error_message or "No message"
            if len(short_error_msg) > 500:
                lines = short_error_msg.strip().split("\n")
                short_error_msg = "...\n" + "\n".join(lines[-7:]) if len(lines) > 7 else short_error_msg[-490:]
                
            blocks = [
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": f"🚨 NeuralOps Alert: {incident.error_type}"[:150]
                    }
                },
                {
                    "type": "section",
                    "fields": [
                        {
                            "type": "mrkdwn",
                            "text": f"*Service:*\n`{incident.service_name}`"
                        },
                        {
                            "type": "mrkdwn",
                            "text": f"*Environment:*\n`{incident.environment}`"
                        },
                        {
                            "type": "mrkdwn",
                            "text": f"*Severity:*\n*{incident.severity.upper()}*"
                        },
                        {
                            "type": "mrkdwn",
                            "text": f"*Occurrences:*\n{incident.occurrence_count}"
                        }
                    ]
                }
            ]
            
            if incident.crash_file:
                file_info = f"`{incident.crash_file}`"
                if incident.crash_line:
                    file_info += f" at line `{incident.crash_line}`"
                blocks.append({
                    "type": "context",
                    "elements": [
                        {
                            "type": "mrkdwn",
                            "text": f"📍 *Location:* {file_info}"
                        }
                    ]
                })

            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*{incident.error_type}*\n```\n{short_error_msg}\n```"
                }
            })
            
            blocks.append({
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {
                            "type": "plain_text",
                            "text": "🔍 View in Dashboard",
                            "emoji": True
                        },
                        "style": "primary",
                        "url": incident_url
                    }
                ]
            })

            payload = {"blocks": blocks}

        # ── Dispatch HTTP Request ─────────────────────────────────────────────
        status_code = None
        status = "pending"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(target_url, json=payload)
                status_code = resp.status_code
                if resp.is_success:
                    status = "delivered"
                else:
                    status = "failed"
                    logger.error("webhook_failed_status", extra={"status": status_code, "body": resp.text})
        except Exception as exc:
            logger.error("webhook_exception", extra={"error": str(exc)})
            status = "failed"

        delivery.status = status
        delivery.http_status_code = status_code
        await session.commit()


@celery_app.task(
    name="app.worker.tasks.send_notification.send_notification",
    bind=True,
    acks_late=True,
    max_retries=3,
    default_retry_delay=5,
)
def send_notification(self, delivery_id: str) -> None:
    """
    Celery task to dispatch a single NotificationDelivery.
    """
    try:
        asyncio.run(_execute_send_notification(delivery_id))
    except Exception as exc:
        logger.error(
            "send_notification_task_error",
            extra={"error": str(exc), "delivery_id": delivery_id},
            exc_info=True
        )
        raise self.retry(exc=exc)
