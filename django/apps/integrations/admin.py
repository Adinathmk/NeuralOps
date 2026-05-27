"""
django/apps/integrations/admin.py

Admin registration for GitHubIntegration.

Encrypted fields are intentionally excluded from the list and detail
views so that admin users cannot extract PATs or webhook secrets.
"""

from django.contrib import admin

from .models import GitHubIntegration


@admin.register(GitHubIntegration)
class GitHubIntegrationAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "tenant",
        "repo_owner",
        "repo_name",
        "default_branch",
        "indexing_status",
        "source_version",
        "created_at",
        "updated_at",
    )
    list_filter = ("indexing_status", "tenant")
    search_fields = ("tenant__name", "repo_owner", "repo_name")
    readonly_fields = (
        "id",
        "source_version",
        "created_at",
        "updated_at",
        # Encrypted fields are read-only in admin — never expose or edit directly.
        "encrypted_pat",
        "webhook_secret",
    )
    ordering = ("-created_at",)

    fieldsets = (
        ("Identity", {"fields": ("id", "tenant")}),
        (
            "Repository",
            {
                "fields": (
                    "repo_url",
                    "repo_owner",
                    "repo_name",
                    "default_branch",
                    "webhook_id",
                )
            },
        ),
        (
            "Credentials (encrypted — read-only)",
            {
                "fields": ("encrypted_pat", "webhook_secret"),
                "classes": ("collapse",),
                "description": (
                    "These fields contain Fernet-encrypted ciphertext. "
                    "Do not copy or share the values here."
                ),
            },
        ),
        (
            "Indexing",
            {"fields": ("indexing_status", "last_indexed_commit")},
        ),
        (
            "Versioning & Timestamps",
            {"fields": ("source_version", "created_at", "updated_at")},
        ),
    )

    def has_add_permission(self, request) -> bool:
        # Integrations must be created via the API to enforce encryption.
        return False