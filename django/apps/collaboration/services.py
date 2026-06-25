"""
CollaborationService — business logic layer for the collaboration feature.

All database writes happen inside transactions. Outbox events are written
atomically in the same transaction so Debezium can deliver them to Kafka
without the risk of a missed event.

Public API:
    get_or_create_thread(tenant_id, incident_id) → IncidentThread
    post_message(thread, author, content, parent_id) → ThreadMessage
    post_system_message(thread, content) → ThreadMessage
    soft_delete_message(message, requesting_user_id, requesting_role) → ThreadMessage
"""

import logging
import uuid

from analytics.models import IncidentSnapshot
from django.db import transaction
from outbox.mixins import write_outbox
from rest_framework.exceptions import NotFound, PermissionDenied
from websockets.publisher import push_collaboration_event

from .models import IncidentThread, ThreadMessage
from .serializers import ThreadMessageSerializer

logger = logging.getLogger(__name__)


class CollaborationService:
    """
    Service layer for incident discussion thread operations.
    All methods are static — no instance state is needed.
    """

    # ── Thread management ────────────────────────────────────────────────────

    @staticmethod
    def get_or_create_thread(
        tenant_id: uuid.UUID, incident_id: uuid.UUID
    ) -> IncidentThread:
        """
        Return the existing thread for this incident, or create one atomically.

        Validates that the incident belongs to the tenant before creating a thread.
        Raises NotFound if the incident snapshot doesn't exist for this tenant.
        """
        # Validate the incident exists and belongs to this tenant
        exists = IncidentSnapshot.objects.filter(
            incident_id=incident_id, tenant_id=tenant_id
        ).exists()
        if not exists:
            raise NotFound(
                f"Incident '{incident_id}' not found for this tenant."
            )

        thread, created = IncidentThread.objects.get_or_create(
            tenant_id=tenant_id,
            incident_id=incident_id,
        )

        if created:
            logger.info(
                "collaboration_thread_created",
                extra={
                    "thread_id": str(thread.id),
                    "incident_id": str(incident_id),
                    "tenant_id": str(tenant_id),
                },
            )

        return thread

    # ── Message creation ─────────────────────────────────────────────────────

    @staticmethod
    def post_message(
        thread: IncidentThread,
        author,  # User instance
        content: str,
        parent_id: uuid.UUID | None = None,
    ) -> ThreadMessage:
        """
        Create a human message in a thread.

        Validates the parent_id belongs to the same thread.
        Writes the Outbox event atomically with the message row.
        Pushes the event to the WebSocket channel layer after the transaction commits.
        """
        parent = None
        if parent_id:
            try:
                parent = ThreadMessage.objects.get(
                    id=parent_id, thread=thread
                )
            except ThreadMessage.DoesNotExist:
                raise NotFound(
                    f"Parent message '{parent_id}' not found in this thread."
                )

        with transaction.atomic():
            message = ThreadMessage.objects.create(
                thread=thread,
                tenant_id=thread.tenant_id,
                author=author,
                content=content,
                parent=parent,
                is_system_message=False,
            )

            # Process @mentions syntax: @[Name](uuid)
            import re
            from users.models import Notification
            
            mention_pattern = re.compile(r"@\[.*?\]\(([0-9a-fA-F\-]{36})\)")
            mentioned_uuids = set(mention_pattern.findall(content))
            
            for m_uuid in mentioned_uuids:
                # Don't notify oneself
                if str(m_uuid) == str(author.id):
                    continue
                
                # Check if user exists in the tenant
                from users.models import User
                try:
                    m_user = User.objects.get(id=m_uuid, tenant_id=thread.tenant_id)
                except User.DoesNotExist:
                    continue
                
                # Strip markdown mentions for the notification body
                import re
                plain_body = re.sub(r'@\[([^\]]+)\]\([^\)]+\)', r'@\1', content)
                excerpt = (plain_body[:80] + '...') if len(plain_body) > 80 else plain_body
                
                notif = Notification.objects.create(
                    tenant_id=thread.tenant_id,
                    user=m_user,
                    type="mention",
                    title=f"{author.get_full_name()} mentioned you",
                    body=excerpt,
                    incident_id=thread.incident_id,
                )
                
                # Push the new notification via WebSockets
                from websockets.publisher import push_notification
                from users.serializers import NotificationSerializer
                push_notification(
                    tenant_id=thread.tenant_id,
                    user_id=m_user.id,
                    data=NotificationSerializer(notif).data
                )

            payload = CollaborationService._message_payload(
                thread, message, event_type="collaboration.message"
            )
            write_outbox(
                topic=f"collaboration.events.{thread.tenant_id}",
                key=str(thread.incident_id),
                payload=payload,
            )

        # Push directly to channel layer after commit for immediate delivery
        # (Debezium/Kafka path handles durability; this handles latency)
        CollaborationService._push_ws(thread.tenant_id, message)

        logger.info(
            "collaboration_message_posted",
            extra={
                "message_id": str(message.id),
                "thread_id": str(thread.id),
                "tenant_id": str(thread.tenant_id),
                "author_id": str(author.id),
            },
        )
        return message

    @staticmethod
    def post_system_message(thread: IncidentThread, content: str) -> ThreadMessage:
        """
        Create an automated system message (status change, assignment, AI event).
        Called by Features 3, 4, and the AI pipeline. No author, no outbox event
        needed for system messages (they are not user-generated collaboration events).
        """
        with transaction.atomic():
            message = ThreadMessage.objects.create(
                thread=thread,
                tenant_id=thread.tenant_id,
                author=None,
                content=content,
                is_system_message=True,
            )

            write_outbox(
                topic=f"collaboration.events.{thread.tenant_id}",
                key=str(thread.incident_id),
                payload=CollaborationService._message_payload(
                    thread, message, event_type="collaboration.message"
                ),
            )

        CollaborationService._push_ws(thread.tenant_id, message)

        logger.info(
            "collaboration_system_message_posted",
            extra={
                "message_id": str(message.id),
                "thread_id": str(thread.id),
                "tenant_id": str(thread.tenant_id),
            },
        )
        return message

    # ── Message deletion ─────────────────────────────────────────────────────

    @staticmethod
    def soft_delete_message(
        message: ThreadMessage,
        requesting_user_id: uuid.UUID,
        requesting_role: str,
    ) -> ThreadMessage:
        """
        Soft-delete a message. Only the original author or an admin/owner may delete.

        The row is NOT physically deleted — is_deleted=True causes the serializer
        to return 'This message was deleted.' instead of the original content.
        Thread chronology is preserved.
        """
        if message.is_system_message:
            raise PermissionDenied("System messages cannot be deleted.")

        is_author = str(message.author_id) == str(requesting_user_id)
        is_admin = requesting_role in ("admin", "owner")

        if not (is_author or is_admin):
            raise PermissionDenied(
                "You can only delete your own messages."
            )

        with transaction.atomic():
            message.is_deleted = True
            message.save(update_fields=["is_deleted", "updated_at"])

            write_outbox(
                topic=f"collaboration.events.{message.tenant_id}",
                key=str(message.thread.incident_id),
                payload=CollaborationService._message_payload(
                    message.thread, message, event_type="collaboration.message_deleted"
                ),
            )

        CollaborationService._push_ws(message.tenant_id, message)

        logger.info(
            "collaboration_message_deleted",
            extra={
                "message_id": str(message.id),
                "requester_id": str(requesting_user_id),
            },
        )
        return message

    # ── Internal helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _message_payload(
        thread: IncidentThread, message: ThreadMessage, event_type: str
    ) -> dict:
        """Build the Outbox payload for a message event."""
        import json
        from django.core.serializers.json import DjangoJSONEncoder

        # Reload author for serializer if needed
        if message.author_id and not hasattr(message, "_author_loaded"):
            try:
                message.author  # access the FK to trigger lazy load (or use select_related)
            except Exception:
                pass

        payload = {
            "event_id": str(uuid.uuid4()),
            "event_type": event_type,
            "incident_id": str(thread.incident_id),
            "tenant_id": str(thread.tenant_id),
            "message": ThreadMessageSerializer(message).data,
        }
        
        # psycopg3's JSON dumper fails on raw UUIDs inside dicts.
        # Run it through DjangoJSONEncoder to stringify all UUIDs/datetimes gracefully.
        return json.loads(json.dumps(payload, cls=DjangoJSONEncoder))

    @staticmethod
    def _push_ws(tenant_id, message: ThreadMessage) -> None:
        """Push message event directly to WebSocket channel layer."""
        import json
        from django.core.serializers.json import DjangoJSONEncoder

        try:
            serialized = ThreadMessageSerializer(message).data
            payload = {
                "incident_id": str(message.thread.incident_id),
                "message": serialized,
            }
            safe_payload = json.loads(json.dumps(payload, cls=DjangoJSONEncoder))

            push_collaboration_event(
                tenant_id=str(tenant_id),
                event_type="message",
                data=safe_payload,
            )
        except Exception as exc:  # noqa: BLE001
            # WS push failure must never break the HTTP response
            logger.warning(
                "collaboration_ws_push_failed",
                extra={"error": str(exc), "message_id": str(message.id)},
            )
