"""
django/apps/integrations/views.py

GitHub Integration API views.

Endpoints:
  GET  /api/integrations/github/   — retrieve current integration (or 404)
  POST /api/integrations/github/   — create or update integration (upsert)

Authorization:
  IsTenantAdmin — admin or owner role required.

On every successful write:
  1. The integration is saved inside an atomic DB transaction.
  2. An AuditLog entry is written.
  3. A transactional outbox event is written to config.tenants topic.
     Debezium picks this up and delivers it to FastAPI's config_sync
     consumer, which upserts the github_* columns in tenant_snapshots.

Architecture reference: NeuralOps Technical Documentation — Sections 3, 17
"""

from __future__ import annotations

import logging

from core.permissions import IsTenantAdmin
from core.responses import APIResponse
from django.db import transaction
from drf_spectacular.utils import extend_schema
from outbox.mixins import write_outbox
from rest_framework.views import APIView
from users.models import AuditLog

from .models import GitHubIntegration
from .serializers import GitHubIntegrationSerializer, GitHubIntegrationStatusSerializer, ServiceRepoMappingSerializer

logger = logging.getLogger(__name__)


def _build_outbox_payload(integration: GitHubIntegration, action: str) -> dict:
    """
    Build the config.tenants Kafka event payload for a GitHub integration change.

    `action` should be one of "created", "updated", or "deleted".
    """
    import time

    tenant = integration.tenant
    return {
        "event_type": "tenant.updated",
        "tenant": {
            "id": str(tenant.id),
            "plan_tier": tenant.plan_tier,
            "is_suspended": tenant.status == "suspended",
            "source_version": int(time.time() * 1000),
            "github_integration_event": {
                "action": action,
                "integration": {
                    "id": str(integration.id),
                    "repo_url": integration.repo_url,
                    "repo_owner": integration.repo_owner,
                    "repo_name": integration.repo_name,
                    "installation_id": str(integration.github_installation_id) if integration.github_installation_id else None,
                    "default_branch": integration.default_branch,
                    "indexing_status": integration.indexing_status,
                    "last_indexed_commit": integration.last_indexed_commit,
                },
            },
        },
    }


class GitHubIntegrationListCreateView(APIView):
    """
    GET  /api/integrations/github/  — list the tenant's GitHub integrations.
    POST /api/integrations/github/  — create a new integration.
    """

    permission_classes = [IsTenantAdmin]

    @extend_schema(
        summary="List GitHub Integrations",
        description="Returns a list of all repository connection metadata and indexing status for the authenticated tenant.",
        responses={200: GitHubIntegrationStatusSerializer(many=True)},
    )
    def get(self, request) -> APIResponse:
        integrations = GitHubIntegration.objects.filter(tenant_id=request.tenant_id)
        serializer = GitHubIntegrationStatusSerializer(integrations, many=True)
        return APIResponse.success(
            data=serializer.data,
            message="GitHub integrations retrieved successfully.",
        )

    @extend_schema(
        summary="Create GitHub Integration",
        description="Connect a new repository.",
        request=GitHubIntegrationSerializer,
        responses={201: GitHubIntegrationStatusSerializer},
    )
    def post(self, request) -> APIResponse:
        tenant_id = request.tenant_id

        # Unique constraint is on (tenant_id, repo_url). We can check it manually for better error message.
        repo_url = request.data.get("repo_url")
        if repo_url and GitHubIntegration.objects.filter(tenant_id=tenant_id, repo_url=repo_url).exists():
            return APIResponse.error(
                message="This repository is already connected.",
                status_code=409,
                code="duplicate_repo",
            )

        serializer = GitHubIntegrationSerializer(data=request.data)
        if not serializer.is_valid():
            return APIResponse.error(
                message="Validation failed.",
                status_code=400,
                code="validation_error",
                errors=serializer.errors,
            )

        with transaction.atomic():
            integration: GitHubIntegration = serializer.save(tenant_id=tenant_id)

            AuditLog.log(
                action="TENANT_CONFIG_UPDATED",
                user=request.user,
                tenant=integration.tenant,
                resource_type="GitHubIntegration",
                resource_id=str(integration.id),
                description=f"GitHub integration created for repo {integration.repo_owner}/{integration.repo_name}",
            )

            payload = _build_outbox_payload(integration, "created")
            write_outbox(
                topic="config.tenants",
                key=str(tenant_id),
                payload=payload,
                source_version=payload["tenant"]["source_version"],
            )

        logger.info(
            "github_integration_created",
            extra={
                "tenant_id": str(tenant_id),
                "repo": f"{integration.repo_owner}/{integration.repo_name}",
                "integration_id": str(integration.id),
            },
        )

        response_serializer = GitHubIntegrationStatusSerializer(integration)
        return APIResponse.success(
            data=response_serializer.data,
            message="GitHub integration connected successfully.",
            status_code=201,
        )


class GitHubIntegrationDetailView(APIView):
    """
    GET    /api/integrations/github/{id}/  — read a specific integration.
    PATCH  /api/integrations/github/{id}/  — update a specific integration.
    DELETE /api/integrations/github/{id}/  — delete a specific integration.
    """

    permission_classes = [IsTenantAdmin]

    def _get_object(self, tenant_id, pk):
        try:
            return GitHubIntegration.objects.get(id=pk, tenant_id=tenant_id)
        except GitHubIntegration.DoesNotExist:
            return None

    @extend_schema(
        summary="Retrieve GitHub Integration",
        description="Returns the repository connection metadata for a specific integration.",
        responses={200: GitHubIntegrationStatusSerializer},
    )
    def get(self, request, pk) -> APIResponse:
        integration = self._get_object(request.tenant_id, pk)
        if not integration:
            return APIResponse.error(
                message="Integration not found.",
                status_code=404,
                code="not_found",
            )
        serializer = GitHubIntegrationStatusSerializer(integration)
        return APIResponse.success(
            data=serializer.data,
            message="GitHub integration retrieved successfully.",
        )

    @extend_schema(
        summary="Update GitHub Integration",
        description="Update credentials or settings for a specific repository connection.",
        request=GitHubIntegrationSerializer,
        responses={200: GitHubIntegrationStatusSerializer},
    )
    def patch(self, request, pk) -> APIResponse:
        integration = self._get_object(request.tenant_id, pk)
        if not integration:
            return APIResponse.error(
                message="Integration not found.",
                status_code=404,
                code="not_found",
            )

        serializer = GitHubIntegrationSerializer(
            instance=integration,
            data=request.data,
            partial=True,
        )

        if not serializer.is_valid():
            return APIResponse.error(
                message="Validation failed.",
                status_code=400,
                code="validation_error",
                errors=serializer.errors,
            )

        with transaction.atomic():
            integration = serializer.save()

            AuditLog.log(
                action="TENANT_CONFIG_UPDATED",
                user=request.user,
                tenant=integration.tenant,
                resource_type="GitHubIntegration",
                resource_id=str(integration.id),
                description=f"GitHub integration updated for repo {integration.repo_owner}/{integration.repo_name}",
            )

            payload = _build_outbox_payload(integration, "updated")
            write_outbox(
                topic="config.tenants",
                key=str(request.tenant_id),
                payload=payload,
                source_version=payload["tenant"]["source_version"],
            )

        logger.info(
            "github_integration_updated",
            extra={
                "tenant_id": str(request.tenant_id),
                "repo": f"{integration.repo_owner}/{integration.repo_name}",
                "integration_id": str(integration.id),
            },
        )

        response_serializer = GitHubIntegrationStatusSerializer(integration)
        return APIResponse.success(
            data=response_serializer.data,
            message="GitHub integration updated successfully.",
            status_code=200,
        )

    @extend_schema(
        summary="Delete GitHub Integration",
        description="Removes the specific GitHub integration and credentials.",
        responses={200: dict},
    )
    def delete(self, request, pk) -> APIResponse:
        import time

        tenant_id = request.tenant_id
        integration = self._get_object(tenant_id, pk)
        if not integration:
            return APIResponse.error(
                message="Integration not found.",
                status_code=404,
                code="not_found",
            )

        with transaction.atomic():
            tenant = integration.tenant
            source_version = int(time.time() * 1000)
            
            # Send the outbox payload before deleting so we can read the properties
            payload = _build_outbox_payload(integration, "deleted")
            # Ensure the deleted action payload sends the proper event type
            payload["tenant"]["source_version"] = source_version

            integration_id_str = str(integration.id)
            repo_str = f"{integration.repo_owner}/{integration.repo_name}"
            
            # Check if this is the LAST integration for the tenant.
            is_last = GitHubIntegration.objects.filter(tenant_id=tenant_id).count() == 1

            integration.delete()

            # Wipe the analytics projection if it was the last integration
            if is_last:
                from analytics.models import IncidentSnapshot
                IncidentSnapshot.objects.filter(tenant=tenant).delete()

            AuditLog.log(
                action="TENANT_CONFIG_UPDATED",
                user=request.user,
                tenant=tenant,
                resource_type="GitHubIntegration",
                resource_id=integration_id_str,
                description=f"GitHub integration deleted for repo {repo_str}",
            )

            write_outbox(
                topic="config.tenants",
                key=str(tenant_id),
                payload=payload,
                source_version=source_version,
            )

        logger.info(
            "github_integration_deleted",
            extra={
                "tenant_id": str(tenant_id),
                "repo": repo_str,
                "integration_id": integration_id_str,
            },
        )

        return APIResponse.success(
            message="GitHub integration deleted successfully.",
            status_code=200,
        )


class GitHubAvailableReposView(APIView):
    """
    GET /api/integrations/github/available-repos/

    Fetches the repositories a user authorized during the GitHub App installation.
    """
    permission_classes = [IsTenantAdmin]

    @extend_schema(
        summary="Fetch Available Repositories",
        description="Returns a list of repositories available for a given installation ID.",
        responses={200: dict},
    )
    def get(self, request) -> APIResponse:
        import time
        import jwt
        import requests
        from django.conf import settings

        installation_id = request.query_params.get("installation_id")
        if not installation_id:
            return APIResponse.error(
                message="installation_id query parameter is required.",
                status_code=400,
                code="missing_installation_id",
            )

        app_id = settings.GITHUB_APP_ID
        private_key = settings.GITHUB_APP_PRIVATE_KEY

        if not app_id or not private_key:
            return APIResponse.error(
                message="GitHub App credentials are not configured on the server.",
                status_code=500,
                code="github_app_unconfigured",
            )

        # 1. Generate JWT (using 5 mins for exp to prevent clock skew issues)
        now = int(time.time())
        payload = {
            "iat": now - 60,
            "exp": now + (5 * 60),
            "iss": str(app_id),
        }
        try:
            encoded_jwt = jwt.encode(payload, private_key, algorithm="RS256")
        except Exception as e:
            logger.error(f"Failed to encode GitHub App JWT: {e}")
            return APIResponse.error(
                message="Failed to authenticate as GitHub App.",
                status_code=500,
                code="github_app_auth_failed",
            )

        # 2. Get Installation Access Token
        headers = {
            "Authorization": f"Bearer {encoded_jwt}",
            "Accept": "application/vnd.github.v3+json",
        }
        token_url = f"https://api.github.com/app/installations/{installation_id}/access_tokens"
        try:
            resp = requests.post(token_url, headers=headers, timeout=10)
            resp.raise_for_status()
            access_token = resp.json()["token"]
        except Exception as e:
            logger.error(f"Failed to get installation access token: {e}")
            return APIResponse.error(
                message="Failed to retrieve access token for the given installation.",
                status_code=400,
                code="github_token_exchange_failed",
            )

        # 3. Fetch Repositories
        repos_headers = {
            "Authorization": f"token {access_token}",
            "Accept": "application/vnd.github.v3+json",
        }
        repos_url = "https://api.github.com/installation/repositories"
        try:
            repos_resp = requests.get(repos_url, headers=repos_headers, timeout=10)
            repos_resp.raise_for_status()
            repositories = repos_resp.json().get("repositories", [])
        except Exception as e:
            logger.error(f"Failed to fetch repositories: {e}")
            return APIResponse.error(
                message="Failed to fetch available repositories.",
                status_code=500,
                code="github_fetch_repos_failed",
            )

        formatted_repos = [
            {
                "id": repo["id"],
                "name": repo["name"],
                "full_name": repo["full_name"],
                "owner": repo["owner"]["login"],
                "html_url": repo["html_url"],
            }
            for repo in repositories
        ]

        return APIResponse.success(
            data={"repositories": formatted_repos},
            message="Repositories fetched successfully.",
        )


def _build_service_mapping_payload(mapping, action: str) -> dict:
    import time
    tenant = mapping.tenant
    return {
        "event_type": "tenant.updated",
        "tenant": {
            "id": str(tenant.id),
            "plan_tier": tenant.plan_tier,
            "is_suspended": tenant.status == "suspended",
            "source_version": int(time.time() * 1000),
            "service_repo_mapping_event": {
                "action": action,
                "mapping": {
                    "id": str(mapping.id),
                    "service_name": mapping.service_name,
                    "repo_url": mapping.github_integration.repo_url,
                },
            },
        },
    }


class ServiceRepoMappingListCreateView(APIView):
    permission_classes = [IsTenantAdmin]

    @extend_schema(
        summary="List Service Repo Mappings",
        responses={200: ServiceRepoMappingSerializer(many=True)},
    )
    def get(self, request) -> APIResponse:
        from .models import ServiceRepoMapping
        mappings = ServiceRepoMapping.objects.filter(tenant_id=request.tenant_id)
        serializer = ServiceRepoMappingSerializer(mappings, many=True)
        return APIResponse.success(data=serializer.data)

    @extend_schema(
        summary="Create Service Repo Mapping",
        request=ServiceRepoMappingSerializer,
        responses={201: ServiceRepoMappingSerializer},
    )
    def post(self, request) -> APIResponse:
        import time
        serializer = ServiceRepoMappingSerializer(
            data=request.data, context={"request": request}
        )
        if not serializer.is_valid():
            return APIResponse.error(
                message="Validation failed", errors=serializer.errors, status_code=400
            )

        with transaction.atomic():
            mapping = serializer.save()
            AuditLog.log(
                tenant=mapping.tenant,
                user=request.user,
                action="TENANT_CONFIG_UPDATED",
                resource_type="ServiceRepoMapping",
                resource_id=str(mapping.id),
                description=f"Created mapping {mapping.service_name} -> {mapping.github_integration.repo_name}",
                ip_address=request.META.get("REMOTE_ADDR"),
            )
            payload = _build_service_mapping_payload(mapping, "created")
            write_outbox(
                topic="config.tenants",
                key=str(mapping.tenant_id),
                payload=payload,
                source_version=int(time.time() * 1000),
            )

        return APIResponse.success(
            data=serializer.data,
            message="Mapping created successfully.",
            status_code=201,
        )


class ServiceRepoMappingDetailView(APIView):
    permission_classes = [IsTenantAdmin]

    @extend_schema(
        summary="Delete Service Repo Mapping",
        responses={200: dict},
    )
    def delete(self, request, mapping_id: str) -> APIResponse:
        import time
        from .models import ServiceRepoMapping
        try:
            mapping = ServiceRepoMapping.objects.get(
                id=mapping_id, tenant_id=request.tenant_id
            )
        except ServiceRepoMapping.DoesNotExist:
            return APIResponse.error(message="Mapping not found.", status_code=404)

        with transaction.atomic():
            payload = _build_service_mapping_payload(mapping, "deleted")
            mapping.delete()
            AuditLog.log(
                user=request.user,
                action="TENANT_CONFIG_UPDATED",
                resource_type="ServiceRepoMapping",
                resource_id=mapping_id,
                description=f"Deleted mapping for {mapping.service_name}",
                ip_address=request.META.get("REMOTE_ADDR"),
            )
            write_outbox(
                topic="config.tenants",
                key=str(request.tenant_id),
                payload=payload,
                source_version=int(time.time() * 1000),
            )

        return APIResponse.success(message="Mapping deleted successfully.")
