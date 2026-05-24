import logging
from django.db import transaction
from rest_framework import viewsets, status
from rest_framework.exceptions import NotFound, PermissionDenied
from drf_spectacular.utils import extend_schema_view, extend_schema

from core.permissions import IsTenantAdmin
from core.responses import APIResponse
from outbox.mixins import write_outbox

from .models import Playbook
from .serializers import PlaybookSerializer

logger = logging.getLogger(__name__)


@extend_schema_view(
    list=extend_schema(summary="List Playbooks"),
    retrieve=extend_schema(summary="Retrieve Playbook"),
    create=extend_schema(summary="Create Playbook", request=PlaybookSerializer, responses={201: PlaybookSerializer}),
    update=extend_schema(summary="Update Playbook", request=PlaybookSerializer, responses={200: PlaybookSerializer}),
    partial_update=extend_schema(summary="Partial Update Playbook", request=PlaybookSerializer, responses={200: PlaybookSerializer}),
    destroy=extend_schema(summary="Delete Playbook")
)
class PlaybookViewSet(viewsets.ViewSet):
    """
    CRUD ViewSet for Playbook resources scoped to the authenticated tenant.

    All reads are filtered by request.tenant_id.
    All writes are wrapped in a transaction and publish a Kafka event
    via the Transactional Outbox (topic: config.playbooks).

    Permissions: IsTenantAdmin (admin or owner role required).
    """

    permission_classes = [IsTenantAdmin]

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _get_tenant_id(self, request):
        tenant_id = getattr(request, "tenant_id", None)
        if not tenant_id:
            raise PermissionDenied("Tenant context is missing.")
        return tenant_id

    def _get_playbook_or_404(self, pk, tenant_id):
        try:
            return Playbook.objects.get(pk=pk, tenant_id=tenant_id)
        except Playbook.DoesNotExist:
            raise NotFound(f"Playbook '{pk}' not found.")

    @staticmethod
    def _outbox_payload(playbook, event_type):
        return {
            "event_type": event_type,
            "playbook": {
                "id": str(playbook.id),
                "tenant_id": str(playbook.tenant_id),
                "error_pattern": playbook.error_pattern,
                "instructions": playbook.instructions,
                "source_version": playbook.source_version,
            },
        }

    # ── List ──────────────────────────────────────────────────────────────────

    def list(self, request):
        tenant_id = self._get_tenant_id(request)
        playbooks = Playbook.objects.filter(tenant_id=tenant_id).order_by("-created_at")
        serializer = PlaybookSerializer(playbooks, many=True)
        return APIResponse.success(
            data=serializer.data,
            message=f"{playbooks.count()} playbook(s) found.",
        )

    # ── Retrieve ──────────────────────────────────────────────────────────────

    def retrieve(self, request, pk=None):
        tenant_id = self._get_tenant_id(request)
        playbook = self._get_playbook_or_404(pk, tenant_id)
        serializer = PlaybookSerializer(playbook)
        return APIResponse.success(data=serializer.data, message="Playbook retrieved.")

    # ── Create ────────────────────────────────────────────────────────────────

    def create(self, request):
        tenant_id = self._get_tenant_id(request)
        serializer = PlaybookSerializer(data=request.data)

        if not serializer.is_valid():
            return APIResponse.error(
                message="Validation failed.",
                status_code=400,
                code="validation_error",
                errors=serializer.errors,
            )

        with transaction.atomic():
            playbook = serializer.save(tenant_id=tenant_id)

            write_outbox(
                topic="config.playbooks",
                key=str(tenant_id),
                payload=self._outbox_payload(playbook, "playbook.created"),
                source_version=playbook.source_version,
            )

        logger.info("playbook_created", extra={"playbook_id": str(playbook.id), "tenant_id": str(tenant_id)})
        return APIResponse.success(
            data=PlaybookSerializer(playbook).data,
            message="Playbook created.",
            status_code=status.HTTP_201_CREATED,
        )

    # ── Update (full) ─────────────────────────────────────────────────────────

    def update(self, request, pk=None):
        return self._update(request, pk, partial=False)

    # ── Partial Update ────────────────────────────────────────────────────────

    def partial_update(self, request, pk=None):
        return self._update(request, pk, partial=True)

    def _update(self, request, pk, partial):
        tenant_id = self._get_tenant_id(request)
        playbook = self._get_playbook_or_404(pk, tenant_id)

        serializer = PlaybookSerializer(playbook, data=request.data, partial=partial)
        if not serializer.is_valid():
            return APIResponse.error(
                message="Validation failed.",
                status_code=400,
                code="validation_error",
                errors=serializer.errors,
            )

        with transaction.atomic():
            playbook = serializer.save()

            write_outbox(
                topic="config.playbooks",
                key=str(tenant_id),
                payload=self._outbox_payload(playbook, "playbook.updated"),
                source_version=playbook.source_version,
            )

        logger.info("playbook_updated", extra={"playbook_id": str(playbook.id), "tenant_id": str(tenant_id)})
        return APIResponse.success(data=PlaybookSerializer(playbook).data, message="Playbook updated.")

    # ── Destroy ───────────────────────────────────────────────────────────────

    def destroy(self, request, pk=None):
        tenant_id = self._get_tenant_id(request)
        playbook = self._get_playbook_or_404(pk, tenant_id)

        playbook_id = str(playbook.id)
        source_version = playbook.source_version

        with transaction.atomic():
            playbook.delete()

            write_outbox(
                topic="config.playbooks",
                key=str(tenant_id),
                payload={
                    "event_type": "playbook.deleted",
                    "playbook": {
                        "id": playbook_id,
                        "tenant_id": str(tenant_id),
                        "deleted": True,
                        "source_version": source_version,
                    },
                },
                source_version=source_version,
            )

        logger.info("playbook_deleted", extra={"playbook_id": playbook_id, "tenant_id": str(tenant_id)})
        return APIResponse.success(message="Playbook deleted.")





