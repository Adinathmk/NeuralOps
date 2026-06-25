"""
Collaboration API views.

ThreadMessageListCreateView  — GET / POST messages for an incident thread.
ThreadMessageDeleteView      — DELETE (soft) a specific message.

All views follow the same patterns as alerts/views.py:
  - APIView subclasses (not generics)
  - IsTenantUser permission (all tenant members can read; viewer check in POST)
  - APIResponse for all responses
  - Service layer delegation
"""

import logging
import uuid

from analytics.models import IncidentSnapshot
from core.permissions import IsTenantUser
from core.responses import APIResponse
from django.db import models as django_models
from rest_framework import status
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.views import APIView

from .models import IncidentThread, ThreadMessage
from .serializers import (
    CreateMessageSerializer,
    ThreadMessageSerializer,
    ThreadSerializer,
)
from .services import CollaborationService

logger = logging.getLogger(__name__)

# Messages returned per page (oldest → newest)
PAGE_SIZE = 100


class ThreadMessageListCreateView(APIView):
    """
    GET  /api/v1/collaboration/incidents/<incident_id>/messages/
         → Returns all messages for the incident thread, oldest first.
           Auto-creates the thread if it doesn't exist yet.
           Response includes thread metadata (message_count, participant_count).

    POST /api/v1/collaboration/incidents/<incident_id>/messages/
         → Posts a new message. Viewer-role users are rejected (403).
    """

    permission_classes = [IsTenantUser]

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _get_tenant_id(self, request):
        tenant_id = getattr(request, "tenant_id", None)
        if not tenant_id:
            raise PermissionDenied("Tenant context is missing.")
        return tenant_id

    def _get_user(self, request):
        """
        Load the User object from the DB using request.user_id.
        Raises PermissionDenied if not resolvable.
        """
        user_id = getattr(request, "user_id", None)
        if not user_id:
            raise PermissionDenied("User context is missing.")
        from users.models import User
        try:
            return User.objects.get(pk=user_id)
        except User.DoesNotExist:
            raise PermissionDenied("Authenticated user not found.")

    # ── GET ───────────────────────────────────────────────────────────────────

    def get(self, request, incident_id):
        """
        Fetch all messages for the incident thread (oldest → newest).

        Creates the thread on first access. Returns an empty message list
        (not 404) for a brand-new incident with no messages yet.
        """
        tenant_id = self._get_tenant_id(request)

        thread = CollaborationService.get_or_create_thread(
            tenant_id=tenant_id,
            incident_id=incident_id,
        )

        # Fetch top-level messages with author and replies prefetched
        messages_qs = (
            ThreadMessage.objects.filter(thread=thread, parent__isnull=True)
            .select_related("author", "thread")
            .prefetch_related(
                django_models.Prefetch(
                    "replies",
                    queryset=ThreadMessage.objects.select_related("author").order_by(
                        "created_at"
                    ),
                )
            )
            .order_by("created_at")
        )

        serialized_messages = ThreadMessageSerializer(messages_qs, many=True).data
        thread_meta = ThreadSerializer(thread).data

        return APIResponse.success(
            data=serialized_messages,
            message=f"{thread.message_count} message(s) in thread.",
            thread=thread_meta,
        )

    # ── POST ──────────────────────────────────────────────────────────────────

    def post(self, request, incident_id):
        """
        Post a new message to the incident thread.

        Viewer-role users receive 403. Validated content + optional parent_id.
        Returns 201 with the created message serialized.
        """
        tenant_id = self._get_tenant_id(request)
        user_role = getattr(request, "user_role", None)

        # Viewer-role guard
        if user_role == "viewer":
            raise PermissionDenied("Viewers cannot post messages.")

        serializer = CreateMessageSerializer(data=request.data)
        if not serializer.is_valid():
            return APIResponse.error(
                message="Validation failed.",
                status_code=400,
                code="validation_error",
                errors=serializer.errors,
            )

        content = serializer.validated_data["content"]
        parent_id = serializer.validated_data.get("parent_id")

        thread = CollaborationService.get_or_create_thread(
            tenant_id=tenant_id,
            incident_id=incident_id,
        )
        author = self._get_user(request)

        message = CollaborationService.post_message(
            thread=thread,
            author=author,
            content=content,
            parent_id=parent_id,
        )

        # Re-fetch with author relation for serialization
        message.refresh_from_db()
        message.author  # force FK load (select_related not available after refresh)

        return APIResponse.created(
            data=ThreadMessageSerializer(message).data,
            message="Message posted.",
        )


class ThreadMessageDeleteView(APIView):
    """
    DELETE /api/v1/collaboration/incidents/<incident_id>/messages/<message_id>/
           → Soft-deletes a message. Only the author or admin/owner may delete.
    """

    permission_classes = [IsTenantUser]

    def delete(self, request, incident_id, message_id):
        tenant_id = getattr(request, "tenant_id", None)
        if not tenant_id:
            raise PermissionDenied("Tenant context is missing.")

        user_id = getattr(request, "user_id", None)
        user_role = getattr(request, "user_role", None)

        try:
            message = (
                ThreadMessage.objects.select_related("thread", "author")
                .get(
                    id=message_id,
                    tenant_id=tenant_id,
                    thread__incident_id=incident_id,
                )
            )
        except ThreadMessage.DoesNotExist:
            raise NotFound("Message not found.")

        if message.is_deleted:
            return APIResponse.error(
                message="Message is already deleted.",
                status_code=400,
                code="already_deleted",
            )

        message = CollaborationService.soft_delete_message(
            message=message,
            requesting_user_id=user_id,
            requesting_role=user_role,
        )

        return APIResponse.success(
            data=ThreadMessageSerializer(message).data,
            message="Message deleted.",
        )


class ListIncidentStatusTransitionsView(APIView):
    """
    GET /api/v1/collaboration/incidents/<incident_id>/status_transitions/
        → Returns all status transitions for the incident, newest first.
    """

    permission_classes = [IsTenantUser]

    def get(self, request, incident_id):
        tenant_id = getattr(request, "tenant_id", None)
        if not tenant_id:
            raise PermissionDenied("Tenant context is missing.")

        from .models import IncidentStatusTransition
        from .serializers import IncidentStatusTransitionSerializer

        qs = IncidentStatusTransition.objects.filter(
            tenant_id=tenant_id,
            incident_id=incident_id,
        ).select_related("actor").order_by("-created_at")

        return APIResponse.success(
            data=IncidentStatusTransitionSerializer(qs, many=True).data,
        )
