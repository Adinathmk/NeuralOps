"""
Collaboration serializers.

AuthorSerializer    — compact user representation for message author display.
ThreadMessageSerializer  — full read serializer for a message (handles soft-delete).
CreateMessageSerializer  — write serializer for new message validation.
ThreadSerializer    — thread metadata (message_count, participant_count).
"""

from rest_framework import serializers

from .models import IncidentThread, ThreadMessage

# ── Avatar colour palette ────────────────────────────────────────────────────
# Deterministic colour derived from user UUID so the same user always gets the
# same colour across sessions, devices, and both human and system views.
_AVATAR_COLOURS = [
    "#6366F1",  # indigo
    "#8B5CF6",  # violet
    "#EC4899",  # pink
    "#F59E0B",  # amber
    "#10B981",  # emerald
    "#3B82F6",  # blue
    "#EF4444",  # red
    "#14B8A6",  # teal
]


def _avatar_colour_for(user_id) -> str:
    """Return a stable avatar colour from the user UUID."""
    if user_id is None:
        return "#94A3B8"  # slate — used for system messages
    # Use last 4 hex chars of UUID to pick a colour
    index = int(str(user_id).replace("-", "")[-4:], 16) % len(_AVATAR_COLOURS)
    return _AVATAR_COLOURS[index]


# ── AuthorSerializer ─────────────────────────────────────────────────────────


class AuthorSerializer(serializers.Serializer):
    """
    Compact author representation embedded in every message.
    Includes the avatar colour so the frontend doesn't need to recompute it.
    """

    id = serializers.UUIDField()
    first_name = serializers.CharField()
    last_name = serializers.CharField()
    full_name = serializers.SerializerMethodField()
    avatar_colour = serializers.SerializerMethodField()
    avatar_url = serializers.SerializerMethodField()

    def get_full_name(self, obj) -> str:
        return f"{obj.first_name} {obj.last_name}".strip() or obj.email

    def get_avatar_colour(self, obj) -> str:
        return _avatar_colour_for(obj.id)
        
    def get_avatar_url(self, obj):
        # Handle cases where the object might be a dict (if it's passed from some weird nested serializer)
        # or a Model instance.
        try:
            profile_picture_key = getattr(obj, "profile_picture_key", None)
            if not profile_picture_key:
                return None
            from apps.users.services.s3_service import S3Service
            return S3Service.generate_presigned_get_url(profile_picture_key)
        except Exception:
            return None


# ── ThreadMessageSerializer ──────────────────────────────────────────────────


class ThreadMessageSerializer(serializers.ModelSerializer):
    """
    Full read serializer for a ThreadMessage.

    - author: nested AuthorSerializer (null for system messages)
    - content: returns 'This message was deleted' placeholder for soft-deleted messages
    - replies: nested list of direct reply messages (one level only)
    """

    author = serializers.SerializerMethodField()
    content = serializers.SerializerMethodField()
    replies = serializers.SerializerMethodField()

    class Meta:
        model = ThreadMessage
        fields = [
            "id",
            "thread_id",
            "author",
            "content",
            "parent_id",
            "replies",
            "is_system_message",
            "is_deleted",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    def get_author(self, obj):
        if obj.author is None:
            return None
        return AuthorSerializer(obj.author).data

    def get_content(self, obj) -> str:
        if obj.is_deleted:
            return "This message was deleted."
        return obj.content

    def get_replies(self, obj):
        """
        Return direct replies to this message, excluding deleted ones' content
        but still including soft-deleted rows (so the thread structure is intact).
        Only called for top-level messages (parent_id=None) to avoid infinite recursion.
        """
        if obj.parent_id is not None:
            # Don't recurse — replies of replies are returned flat
            return []
        qs = obj.replies.select_related("author").order_by("created_at")
        return ThreadMessageSerializer(qs, many=True).data


# ── CreateMessageSerializer ──────────────────────────────────────────────────


class CreateMessageSerializer(serializers.Serializer):
    """
    Write serializer for posting a new message to a thread.
    Validates content length and optional parent_id.
    Role enforcement (viewer rejection) is done in the view layer.
    """

    content = serializers.CharField(
        min_length=1,
        max_length=10_000,
        trim_whitespace=False,
        error_messages={
            "blank": "Message content cannot be empty.",
            "max_length": "Message content cannot exceed 10,000 characters.",
        },
    )
    parent_id = serializers.UUIDField(
        required=False,
        allow_null=True,
        default=None,
        help_text="UUID of the parent message if this is a reply.",
    )


# ── ThreadSerializer ─────────────────────────────────────────────────────────


class ThreadSerializer(serializers.ModelSerializer):
    """
    Thread metadata returned in the response envelope alongside messages.
    """

    message_count = serializers.IntegerField(read_only=True)
    participant_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = IncidentThread
        fields = [
            "id",
            "incident_id",
            "message_count",
            "participant_count",
            "created_at",
        ]
        read_only_fields = fields


# ── IncidentStatusTransitionSerializer ────────────────────────────────────────

from .models import IncidentStatusTransition


class IncidentStatusTransitionSerializer(serializers.ModelSerializer):
    """
    Serializer for the incident status history.
    Includes the compact actor representation.
    """

    actor = serializers.SerializerMethodField()

    class Meta:
        model = IncidentStatusTransition
        fields = [
            "id",
            "incident_id",
            "actor",
            "from_status",
            "to_status",
            "note",
            "created_at",
        ]
        read_only_fields = fields

    def get_actor(self, obj):
        if obj.actor is None:
            return None
        return AuthorSerializer(obj.actor).data
