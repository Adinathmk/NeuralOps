from rest_framework import serializers
from .models import AlertRule

VALID_SEVERITIES = {"critical", "high", "medium", "low", "info"}


class AlertRuleSerializer(serializers.ModelSerializer):
    class Meta:
        model = AlertRule
        fields = [
            "id",
            "tenant",
            "confidence_threshold",
            "severity_filter",
            "recipient_ids",
            "enabled",
            "source_version",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "tenant", "source_version", "created_at", "updated_at"]

    # ── Field-level validation ─────────────────────────────────────────────────

    def validate_confidence_threshold(self, value):
        if not (0.0 <= value <= 1.0):
            raise serializers.ValidationError(
                "confidence_threshold must be between 0.0 and 1.0."
            )
        return value

    def validate_severity_filter(self, value):
        if not isinstance(value, list):
            raise serializers.ValidationError("severity_filter must be a list.")
        invalid = [v for v in value if v not in VALID_SEVERITIES]
        if invalid:
            raise serializers.ValidationError(
                f"Invalid severity values: {invalid}. "
                f"Allowed: {sorted(VALID_SEVERITIES)}."
            )
        return value

    def validate_recipient_ids(self, value):
        if not isinstance(value, list):
            raise serializers.ValidationError("recipient_ids must be a list.")
        import uuid as _uuid
        for item in value:
            try:
                _uuid.UUID(str(item))
            except (ValueError, AttributeError):
                raise serializers.ValidationError(
                    f"'{item}' is not a valid UUID."
                )
        return [str(item) for item in value]