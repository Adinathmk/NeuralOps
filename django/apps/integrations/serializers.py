"""
django/apps/integrations/serializers.py

Serializers for the GitHub Integration API.

Security contract:
  - `pat` and `webhook_secret` are write-only — they are NEVER included
    in any serializer output (read responses).
  - Before the model is saved, these fields are encrypted via
    `integrations.encryption.encrypt_secret()` and stored in the model's
    `encrypted_pat` and `webhook_secret` fields respectively.
  - The encrypted ciphertext is also excluded from API responses; clients
    never receive raw or encrypted secrets through this endpoint.

Architecture reference: NeuralOps Technical Documentation — Section 20
(Security — GitHub PATs stored encrypted at rest using Vault-managed DEK).
"""

from __future__ import annotations

from rest_framework import serializers

from .models import GitHubIntegration


class GitHubIntegrationSerializer(serializers.ModelSerializer):
    """
    Full read/write serializer for GitHubIntegration.

    Write inputs
    -----------------
    github_installation_id: Required on create.

    Read outputs
    ------------
    Clients see safe metadata: repo URL/owner/name, default branch,
    installation ID, indexing lifecycle fields, and timestamps.
    """


    class Meta:
        model = GitHubIntegration
        fields = [
            # ── Safe read fields ──────────────────────────────────────────────
            "id",
            "tenant",
            "repo_url",
            "repo_owner",
            "repo_name",
            "default_branch",
            "webhook_id",
            "indexing_status",
            "last_indexed_commit",
            "source_version",
            "created_at",
            "updated_at",
            "github_installation_id",
        ]
        read_only_fields = [
            "id",
            "tenant",
            "indexing_status",
            "last_indexed_commit",
            "source_version",
            "created_at",
            "updated_at",
        ]

    # ── Field-level validation ────────────────────────────────────────────────

    def validate_repo_url(self, value: str) -> str:
        """Ensure the repo URL is a GitHub HTTPS URL."""
        if not value.startswith("https://github.com/"):
            raise serializers.ValidationError(
                "repo_url must be a GitHub HTTPS URL "
                "(e.g. https://github.com/my-org/my-repo)."
            )
        return value.rstrip("/")

    def validate_default_branch(self, value: str) -> str:
        if not value or not value.strip():
            raise serializers.ValidationError("default_branch cannot be blank.")
        return value.strip()

    def validate(self, attrs):
        # Enforce github_installation_id is required on create
        if not self.instance:
            if not attrs.get("github_installation_id"):
                raise serializers.ValidationError(
                    {"github_installation_id": "This field is required when creating a new integration."}
                )
        return attrs


class GitHubIntegrationStatusSerializer(serializers.ModelSerializer):
    """
    Lightweight read-only serializer for the indexing status.
    Used in GET responses to show the current integration health.
    """

    class Meta:
        model = GitHubIntegration
        fields = [
            "id",
            "repo_url",
            "repo_owner",
            "repo_name",
            "default_branch",
            "webhook_id",
            "indexing_status",
            "last_indexed_commit",
            "source_version",
            "created_at",
            "updated_at",
            "github_installation_id",
        ]
        read_only_fields = fields
