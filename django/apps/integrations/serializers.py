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

from .encryption import encrypt_secret
from .models import GitHubIntegration


class GitHubIntegrationSerializer(serializers.ModelSerializer):
    """
    Full read/write serializer for GitHubIntegration.

    Write-only inputs
    -----------------
    pat:            Plain-text GitHub Personal Access Token. Required on create.
                    Optional on partial update (PATCH) — omit to keep existing.
    webhook_secret: Plain-text webhook signing secret. Required on create.
                    Optional on partial update (PATCH) — omit to keep existing.

    Read outputs
    ------------
    Encrypted fields (encrypted_pat, webhook_secret stored on the model) are
    excluded entirely.  Clients see only safe metadata: repo URL/owner/name,
    default branch, indexing lifecycle fields, and timestamps.
    """

    # ── Write-only credential inputs ──────────────────────────────────────────
    pat = serializers.CharField(
        write_only=True,
        required=False,  # Optional on PATCH; enforced in validate() for POST
        allow_blank=False,
        style={"input_type": "password"},
        help_text=(
            "Plain-text GitHub Personal Access Token. "
            "Required when creating a new integration. "
            "On PATCH, omit to keep the current token."
        ),
    )
    webhook_secret_input = serializers.CharField(
        source="webhook_secret",  # maps to model field — overridden in create/update
        write_only=True,
        required=False,
        allow_blank=False,
        style={"input_type": "password"},
        help_text=(
            "Plain-text webhook signing secret. "
            "Required when creating a new integration. "
            "On PATCH, omit to keep the current secret."
        ),
    )

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
            # ── Write-only credential inputs ──────────────────────────────────
            "pat",
            "webhook_secret_input",
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
        # Encrypted storage fields are intentionally ABSENT from `fields` so
        # they are never serialised into API responses.

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

    # ── Object-level validation ───────────────────────────────────────────────

    def validate(self, attrs: dict) -> dict:
        """
        Enforce that `pat` and `webhook_secret_input` are present on CREATE
        (POST).  On partial updates (PATCH) they are optional — if omitted,
        the existing encrypted values are preserved in create() / update().
        """
        is_create = self.instance is None

        if is_create:
            if not attrs.get("pat"):
                raise serializers.ValidationError(
                    {"pat": "pat is required when creating a new GitHub integration."}
                )
            if not attrs.get("webhook_secret"):
                raise serializers.ValidationError(
                    {
                        "webhook_secret_input": (
                            "webhook_secret is required when creating "
                            "a new GitHub integration."
                        )
                    }
                )

        return attrs

    # ── Create / Update hooks (encrypt before model save) ─────────────────────

    def create(self, validated_data: dict) -> GitHubIntegration:
        """
        Encrypt credential fields before creating the model instance.

        Steps:
          1. Pop the plain-text `pat` from validated_data.
          2. Pop the plain-text `webhook_secret` (mapped from webhook_secret_input).
          3. Encrypt both and inject the ciphertext as `encrypted_pat` and
             `webhook_secret` on the model.
          4. Call super().create() which calls GitHubIntegration.objects.create().
        """
        plain_pat: str = validated_data.pop("pat")
        # `webhook_secret` is injected by the `source` mapping on the field
        plain_webhook_secret: str = validated_data.pop("webhook_secret", "")

        validated_data["encrypted_pat"] = encrypt_secret(plain_pat)
        validated_data["webhook_secret"] = encrypt_secret(plain_webhook_secret)

        return super().create(validated_data)

    def update(
        self, instance: GitHubIntegration, validated_data: dict
    ) -> GitHubIntegration:
        """
        Encrypt credential fields if provided; preserve existing values otherwise.

        Steps:
          1. If `pat` present, encrypt and overwrite encrypted_pat.
          2. If `webhook_secret` present, encrypt and overwrite webhook_secret.
          3. Apply remaining fields via super().update().
        """
        plain_pat: str | None = validated_data.pop("pat", None)
        plain_webhook_secret: str | None = validated_data.pop("webhook_secret", None)

        if plain_pat:
            validated_data["encrypted_pat"] = encrypt_secret(plain_pat)

        if plain_webhook_secret:
            validated_data["webhook_secret"] = encrypt_secret(plain_webhook_secret)

        return super().update(instance, validated_data)


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
        ]
        read_only_fields = fields
