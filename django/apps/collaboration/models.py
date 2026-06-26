"""
Collaboration models — DB-1 (Django).

IncidentThread: one persistent thread per incident, auto-created on first access.
ThreadMessage:  individual messages within a thread, with reply nesting support.

Both tables are tenant-scoped. PostgreSQL Row-Level Security is enforced
automatically by the existing TenantMiddleware (sets app.current_tenant on
every connection). No additional RLS configuration is needed here.
"""

from __future__ import annotations

import uuid

from django.db import models


class IncidentThread(models.Model):
    """
    One discussion thread per incident.

    Threads are created lazily via CollaborationService.get_or_create_thread()
    on the first GET or POST request for that incident. They are never deleted —
    the thread persists for the full incident lifecycle including post-closure.

    `incident_id` is a plain UUIDField (not a FK to IncidentSnapshot) so that
    collaboration data remains intact even if the snapshot is re-synced or
    temporarily absent. Cross-service safety via UUID reference only.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.CASCADE,
        related_name="incident_threads",
        db_index=True,
    )

    # UUID reference to IncidentSnapshot.incident_id (no FK constraint intentionally)
    incident_id = models.UUIDField(
        db_index=True,
        help_text="Matches incidents.id in DB-2 / IncidentSnapshot.incident_id in DB-1.",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "incident_threads"
        unique_together = [("tenant", "incident_id")]
        indexes = [
            models.Index(
                fields=["tenant", "incident_id"],
                name="cth_tenant_incident_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"IncidentThread(incident={self.incident_id} tenant={self.tenant_id})"

    @property
    def message_count(self) -> int:
        """Total non-deleted messages in this thread."""
        return self.messages.filter(is_deleted=False).count()

    @property
    def participant_count(self) -> int:
        """Number of unique human authors who have posted at least one message."""
        return (
            self.messages.filter(is_system_message=False, is_deleted=False)
            .values("author_id")
            .distinct()
            .count()
        )


class ThreadMessage(models.Model):
    """
    A single message within an incident thread.

    Human messages have `author` set. System messages (status changes,
    assignments, AI analysis completion) have `author=None` and
    `is_system_message=True`.

    Soft-delete: `is_deleted=True` replaces content with a placeholder
    in the serializer. The row is never physically removed so thread
    chronology is preserved.

    Reply nesting: `parent` points to the direct parent message. Only
    one level of nesting is rendered in the UI (replies of replies are
    flat under the original parent).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    thread = models.ForeignKey(
        IncidentThread,
        on_delete=models.CASCADE,
        related_name="messages",
    )

    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.CASCADE,
        db_index=True,
    )

    # Null for system-generated messages
    author = models.ForeignKey(
        "users.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="thread_messages",
    )

    content = models.TextField(
        help_text="Plain-text message body. Preserves line breaks.",
    )

    # Optional reply parent (one level of nesting)
    parent = models.ForeignKey(
        "self",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="replies",
        help_text="Set when this message is a reply to another message.",
    )

    is_system_message = models.BooleanField(
        default=False,
        help_text=(
            "True for automated messages (status changes, assignments, AI events). "
            "System messages have author=None and a distinct visual style in the UI."
        ),
    )

    is_deleted = models.BooleanField(
        default=False,
        help_text=(
            "Soft delete flag. Deleted messages render as 'This message was deleted' "
            "in the UI. The row is never physically removed."
        ),
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "thread_messages"
        ordering = ["created_at"]
        indexes = [
            models.Index(
                fields=["thread", "created_at"],
                name="cmsg_thread_created_idx",
            ),
            models.Index(
                fields=["thread", "parent"],
                name="cmsg_thread_parent_idx",
            ),
            models.Index(
                fields=["tenant", "created_at"],
                name="cmsg_tenant_created_idx",
            ),
        ]

    def __str__(self) -> str:
        author_label = str(self.author_id) if self.author_id else "system"
        return (
            f"ThreadMessage(id={self.id} thread={self.thread_id} author={author_label})"
        )


class IncidentStatusTransition(models.Model):
    """
    Append-only record of a status change for an incident.
    Records who made the change, the from/to status, and an optional note.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.CASCADE,
        related_name="status_transitions",
        db_index=True,
    )
    incident_id = models.UUIDField(
        db_index=True,
        help_text="Matches incidents.id in DB-2.",
    )
    actor = models.ForeignKey(
        "users.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="status_transitions",
        help_text="The user who made the status change.",
    )
    from_status = models.CharField(max_length=32)
    to_status = models.CharField(max_length=32)
    note = models.TextField(
        null=True,
        blank=True,
        max_length=280,
        help_text="Optional explanation for the status change.",
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "incident_status_transitions"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["tenant", "incident_id"]),
        ]

    def __str__(self) -> str:
        return f"Transition(incident={self.incident_id} {self.from_status}->{self.to_status})"
