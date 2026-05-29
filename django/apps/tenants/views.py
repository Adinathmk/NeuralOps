from core.permissions import IsTenantAdmin  # adjust import path as needed
from core.responses import APIResponse
from django.db import transaction
from drf_spectacular.utils import extend_schema
from outbox.mixins import write_outbox
from rest_framework import serializers, status
from rest_framework.response import Response
from rest_framework.views import APIView


class TenantConfigSerializer(serializers.Serializer):
    alert_confidence_threshold = serializers.FloatField(
        min_value=0.0, max_value=1.0, required=False
    )
    log_retention_days = serializers.IntegerField(
        min_value=1, max_value=3650, required=False
    )
    enable_email_notifications = serializers.BooleanField(required=False)
    metadata = serializers.DictField(required=False)


class TenantConfigView(APIView):
    """
    GET  /api/tenant/config/  — read current tenant configuration.
    PATCH /api/tenant/config/ — update one or more config fields.

    Requires at least IsTenantAdmin.
    Redis cache is checked on GET; invalidated and rewritten on PATCH.
    """

    permission_classes = [IsTenantAdmin]

    @extend_schema(
        summary="Get Tenant Configuration", responses={200: TenantConfigSerializer}
    )
    def get(self, request):
        from tenants.models import TenantConfiguration
        from users.cache import cache_manager

        tenant_id = request.tenant_id

        # --- Try Redis cache first ---
        cached = cache_manager.get_tenant_config(tenant_id)
        if cached:
            return APIResponse.success(
                data=cached, message="Configuration retrieved from cache."
            )

        # --- Cache miss: read from DB and populate cache ---
        try:
            config = TenantConfiguration.objects.get(tenant_id=tenant_id)
        except TenantConfiguration.DoesNotExist:
            return APIResponse.error(
                message="Tenant configuration not found.",
                status_code=404,
                code="not_found",
            )

        data = TenantConfigSerializer(config).data

        from users.cache import cache_tenant_config

        cache_tenant_config(tenant_id, **data)

        return APIResponse.success(
            data=data, message="Configuration retrieved successfully."
        )

    @extend_schema(
        summary="Update Tenant Configuration",
        request=TenantConfigSerializer,
        responses={200: TenantConfigSerializer},
    )
    def patch(self, request):
        from tenants.models import TenantConfiguration
        from users.cache import cache_tenant_config, invalidate_tenant_config
        from users.models import AuditLog

        tenant_id = request.tenant_id

        serializer = TenantConfigSerializer(data=request.data, partial=True)

        serializer.is_valid(raise_exception=True)

        updates = serializer.validated_data

        try:
            config = TenantConfiguration.objects.get(tenant_id=tenant_id)

        except TenantConfiguration.DoesNotExist:
            return APIResponse.error(
                message="Tenant configuration not found.",
                status_code=404,
                code="not_found",
            )

        # -----------------------------
        # ATOMIC TRANSACTION
        # -----------------------------
        with transaction.atomic():

            # Update config
            for field, value in updates.items():
                setattr(config, field, value)

            config.save(update_fields=list(updates.keys()))

            # Fresh serialized state
            refreshed_data = TenantConfigSerializer(config).data

            # Audit log
            AuditLog.log(
                action="TENANT_CONFIG_UPDATED",
                user=request.user,
                tenant=config.tenant,
                description=f"updated_fields: {list(updates.keys())}",
            )

            # Transactional outbox event
            write_outbox(
                topic="config.tenants.updated",
                key=f"tenant:{tenant_id}",
                payload={
                    "tenant_id": str(tenant_id),
                    "updated_fields": list(updates.keys()),
                    "config": refreshed_data,
                },
            )

        # -----------------------------
        # CACHE OPERATIONS
        # OUTSIDE TRANSACTION
        # -----------------------------

        invalidate_tenant_config(tenant_id)

        cache_tenant_config(tenant_id, **refreshed_data)

        return APIResponse.success(
            data=refreshed_data, message="Configuration updated successfully."
        )
