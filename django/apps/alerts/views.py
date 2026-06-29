import logging

from core.permissions import IsTenantAdmin
from core.responses import APIResponse
from django.db import transaction
from drf_spectacular.utils import extend_schema, extend_schema_view
from outbox.mixins import write_outbox
from rest_framework import status, viewsets
from rest_framework.exceptions import NotFound, PermissionDenied

from .models import AlertRule
from .serializers import AlertRuleSerializer

logger = logging.getLogger(__name__)


@extend_schema_view(
    list=extend_schema(summary="List Alert Rules"),
    retrieve=extend_schema(summary="Retrieve Alert Rule"),
    create=extend_schema(
        summary="Create Alert Rule",
        request=AlertRuleSerializer,
        responses={201: AlertRuleSerializer},
    ),
    update=extend_schema(
        summary="Update Alert Rule",
        request=AlertRuleSerializer,
        responses={200: AlertRuleSerializer},
    ),
    partial_update=extend_schema(
        summary="Partial Update Alert Rule",
        request=AlertRuleSerializer,
        responses={200: AlertRuleSerializer},
    ),
    destroy=extend_schema(summary="Delete Alert Rule"),
)
class AlertRuleViewSet(viewsets.ViewSet):
    """
    CRUD ViewSet for AlertRule resources scoped to the authenticated tenant.

    All reads are filtered by request.tenant_id.
    All writes are wrapped in a transaction and publish a Kafka event
    via the Transactional Outbox (topic: config.alert_rules).

    Permissions: IsTenantAdmin (admin or owner role required).
    """

    permission_classes = [IsTenantAdmin]

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _get_tenant_id(self, request):
        tenant_id = getattr(request, "tenant_id", None)
        if not tenant_id:
            raise PermissionDenied("Tenant context is missing.")
        return tenant_id

    def _get_rule_or_404(self, pk, tenant_id):
        try:
            return AlertRule.objects.get(pk=pk, tenant_id=tenant_id)
        except AlertRule.DoesNotExist:
            raise NotFound(f"AlertRule '{pk}' not found.")

    @staticmethod
    def _outbox_payload(rule, event_type):
        return {
            "event_type": event_type,
            "alert_rule": {
                "id": str(rule.id),
                "tenant_id": str(rule.tenant_id),
                "confidence_threshold": rule.confidence_threshold,
                "severity_filter": rule.severity_filter,
                "destinations": rule.destinations,
                "enabled": rule.enabled,
                "source_version": rule.source_version,
            },
        }

    # ── List ──────────────────────────────────────────────────────────────────

    def list(self, request):
        tenant_id = self._get_tenant_id(request)
        rules = AlertRule.objects.filter(tenant_id=tenant_id).order_by("-created_at")
        serializer = AlertRuleSerializer(rules, many=True)
        return APIResponse.success(
            data=serializer.data,
            message=f"{rules.count()} alert rule(s) found.",
        )

    # ── Retrieve ──────────────────────────────────────────────────────────────

    def retrieve(self, request, pk=None):
        tenant_id = self._get_tenant_id(request)
        rule = self._get_rule_or_404(pk, tenant_id)
        serializer = AlertRuleSerializer(rule)
        return APIResponse.success(
            data=serializer.data, message="Alert rule retrieved."
        )

    # ── Create ────────────────────────────────────────────────────────────────

    def create(self, request):
        tenant_id = self._get_tenant_id(request)
        serializer = AlertRuleSerializer(data=request.data)

        if not serializer.is_valid():
            return APIResponse.error(
                message="Validation failed.",
                status_code=400,
                code="validation_error",
                errors=serializer.errors,
            )

        with transaction.atomic():
            rule = serializer.save(tenant_id=tenant_id)

            write_outbox(
                topic="config.alert_rules",
                key=str(tenant_id),
                payload=self._outbox_payload(rule, "alert_rule.created"),
                source_version=rule.source_version,
            )

        logger.info(
            "alert_rule_created",
            extra={"rule_id": str(rule.id), "tenant_id": str(tenant_id)},
        )
        return APIResponse.success(
            data=AlertRuleSerializer(rule).data,
            message="Alert rule created.",
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
        rule = self._get_rule_or_404(pk, tenant_id)

        serializer = AlertRuleSerializer(rule, data=request.data, partial=partial)
        if not serializer.is_valid():
            return APIResponse.error(
                message="Validation failed.",
                status_code=400,
                code="validation_error",
                errors=serializer.errors,
            )

        with transaction.atomic():
            rule = serializer.save()

            write_outbox(
                topic="config.alert_rules",
                key=str(tenant_id),
                payload=self._outbox_payload(rule, "alert_rule.updated"),
                source_version=rule.source_version,
            )

        logger.info(
            "alert_rule_updated",
            extra={"rule_id": str(rule.id), "tenant_id": str(tenant_id)},
        )
        return APIResponse.success(
            data=AlertRuleSerializer(rule).data, message="Alert rule updated."
        )

    # ── Destroy ───────────────────────────────────────────────────────────────

    def destroy(self, request, pk=None):
        tenant_id = self._get_tenant_id(request)
        rule = self._get_rule_or_404(pk, tenant_id)

        rule_id = str(rule.id)
        source_version = rule.source_version

        with transaction.atomic():
            rule.delete()

            write_outbox(
                topic="config.alert_rules",
                key=str(tenant_id),
                payload={
                    "event_type": "alert_rule.deleted",
                    "alert_rule": {
                        "id": rule_id,
                        "tenant_id": str(tenant_id),
                        "deleted": True,
                        "source_version": source_version,
                    },
                },
                source_version=source_version,
            )

        logger.info(
            "alert_rule_deleted",
            extra={"rule_id": rule_id, "tenant_id": str(tenant_id)},
        )
        return APIResponse.success(message="Alert rule deleted.")
