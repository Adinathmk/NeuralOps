import re
from rest_framework import serializers
from .models import Playbook


class PlaybookSerializer(serializers.ModelSerializer):
    class Meta:
        model = Playbook
        fields = [
            "id",
            "tenant",
            "error_pattern",
            "instructions",
            "source_version",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "tenant", "source_version", "created_at", "updated_at"]

    def validate_error_pattern(self, value):
        """Ensure error_pattern is a compilable regular expression."""
        if not value or not value.strip():
            raise serializers.ValidationError("error_pattern cannot be blank.")
        try:
            re.compile(value)
        except re.error as exc:
            raise serializers.ValidationError(
                f"error_pattern is not a valid regular expression: {exc}"
            )
        return value

    def validate_instructions(self, value):
        if not value or not value.strip():
            raise serializers.ValidationError("instructions cannot be blank.")
        return value