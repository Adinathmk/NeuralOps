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
from .serializers import GitHubIntegrationSerializer, GitHubIntegrationStatusSerializer

logger = logging.getLogger(__name__)


def _build_outbox_payload(integration: GitHubIntegration) -> dict:
    """
    Build the config.tenants Kafka event payload for a GitHub integration change.

    The nested `github_integration` block is consumed by FastAPI's
    _handle_tenant_event() to upsert the github_* columns in tenant_snapshots.

    IMPORTANT: The installation_id IS included so FastAPI can
    fetch tokens when authenticating against GitHub at index time.
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
            "github_integration": {
                "repo_url": integration.repo_url,
                "repo_owner": integration.repo_owner,
                "repo_name": integration.repo_name,
                "installation_id": int(integration.github_installation_id) if integration.github_installation_id else None,
                "default_branch": integration.default_branch,
                "indexing_status": integration.indexing_status,
                "last_indexed_commit": integration.last_indexed_commit,
            },
        },
    }


class GitHubIntegrationView(APIView):
    """
    GET  /api/integrations/github/  — read the tenant's GitHub integration.
    POST /api/integrations/github/  — create or update (upsert) the integration.

    Only tenant admins (admin | owner role) may access these endpoints.
    """

    permission_classes = [IsTenantAdmin]

    # ── GET ────────────────────────────────────────────────────────────────────

    @extend_schema(
        summary="Retrieve GitHub Integration",
        description="Returns the current repository connection metadata and indexing status for the authenticated tenant (credentials are excluded).",
        responses={200: GitHubIntegrationStatusSerializer},
    )
    def get(self, request) -> APIResponse:
        """
        Return the current GitHub integration for the authenticated tenant.

        Returns:
            200 with integration details (no credentials exposed).
            404 if no integration exists yet.
        """
        tenant_id = request.tenant_id

        try:
            integration = GitHubIntegration.objects.get(tenant_id=tenant_id)
        except GitHubIntegration.DoesNotExist:
            return APIResponse.error(
                message="No GitHub integration found for this tenant.",
                status_code=404,
                code="not_found",
            )

        serializer = GitHubIntegrationStatusSerializer(integration)
        return APIResponse.success(
            data=serializer.data,
            message="GitHub integration retrieved successfully.",
        )

    # ── POST (upsert) ──────────────────────────────────────────────────────────

    @extend_schema(
        summary="Create or Update GitHub Integration",
        description="Connect a repository or update credentials.",
        request=GitHubIntegrationSerializer,
        responses={
            200: GitHubIntegrationStatusSerializer,
            201: GitHubIntegrationStatusSerializer,
        },
    )
    def post(self, request) -> APIResponse:
        """
        Create or update (upsert) the GitHub integration for this tenant.

        On create:  `github_installation_id` is required.
        On update:  `github_installation_id` is optional.

        An outbox event is published so FastAPI's snapshot table stays in sync.

        Returns:
            201 on successful create.
            200 on successful update.
            400 on validation error.
        """
        tenant_id = request.tenant_id

        # ── Determine create vs update ────────────────────────────────────────
        try:
            existing = GitHubIntegration.objects.get(tenant_id=tenant_id)
            is_create = False
        except GitHubIntegration.DoesNotExist:
            existing = None
            is_create = True

        # ── Validate ──────────────────────────────────────────────────────────
        serializer = GitHubIntegrationSerializer(
            instance=existing,
            data=request.data,
            partial=not is_create,  # Full validation on create; partial on update
        )

        if not serializer.is_valid():
            return APIResponse.error(
                message="Validation failed.",
                status_code=400,
                code="validation_error",
                errors=serializer.errors,
            )

        # ── Atomic write: model + audit log + outbox ──────────────────────────
        with transaction.atomic():
            if is_create:
                integration: GitHubIntegration = serializer.save(tenant_id=tenant_id)
            else:
                integration: GitHubIntegration = serializer.save()

            # Audit trail
            AuditLog.log(
                action="TENANT_CONFIG_UPDATED",
                user=request.user,
                tenant=integration.tenant,
                resource_type="GitHubIntegration",
                resource_id=str(integration.id),
                description=(
                    f"GitHub integration {'created' if is_create else 'updated'} "
                    f"for repo {integration.repo_owner}/{integration.repo_name}"
                ),
            )

            # Transactional outbox event → config.tenants → FastAPI snapshot
            payload = _build_outbox_payload(integration)
            write_outbox(
                topic="config.tenants",
                key=str(tenant_id),
                payload=payload,
                source_version=payload["tenant"]["source_version"],
            )

        logger.info(
            "github_integration_%s",
            "created" if is_create else "updated",
            extra={
                "tenant_id": str(tenant_id),
                "repo": f"{integration.repo_owner}/{integration.repo_name}",
                "integration_id": str(integration.id),
            },
        )

        response_serializer = GitHubIntegrationStatusSerializer(integration)
        status_code = 201 if is_create else 200
        message = (
            "GitHub integration connected successfully."
            if is_create
            else "GitHub integration updated successfully."
        )

        return APIResponse.success(
            data=response_serializer.data,
            message=message,
            status_code=status_code,
        )

    # ── DELETE ────────────────────────────────────────────────────────────────

    @extend_schema(
        summary="Delete GitHub Integration",
        description="Removes the GitHub integration and credentials for this tenant.",
        responses={200: dict},
    )
    def delete(self, request) -> APIResponse:
        """
        Delete the GitHub integration for this tenant.

        An outbox event is published so FastAPI's snapshot table clears out
        the github_* columns.

        Returns:
            200 on successful deletion.
            404 if no integration exists.
        """
        import time

        tenant_id = request.tenant_id

        try:
            integration = GitHubIntegration.objects.get(tenant_id=tenant_id)
        except GitHubIntegration.DoesNotExist:
            return APIResponse.error(
                message="No GitHub integration found for this tenant.",
                status_code=404,
                code="not_found",
            )

        with transaction.atomic():
            # Build the outbox payload with github_integration = None
            tenant = integration.tenant
            source_version = int(time.time() * 1000)
            payload = {
                "event_type": "tenant.updated",
                "tenant": {
                    "id": str(tenant.id),
                    "plan_tier": tenant.plan_tier,
                    "is_suspended": tenant.status == "suspended",
                    "source_version": source_version,
                    "github_integration": None,
                },
            }

            # Delete the integration
            integration_id_str = str(integration.id)
            repo_str = f"{integration.repo_owner}/{integration.repo_name}"
            integration.delete()

            # Audit trail
            AuditLog.log(
                action="TENANT_CONFIG_UPDATED",
                user=request.user,
                tenant=tenant,
                resource_type="GitHubIntegration",
                resource_id=integration_id_str,
                description=f"GitHub integration deleted for repo {repo_str}",
            )

            # Transactional outbox event
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
