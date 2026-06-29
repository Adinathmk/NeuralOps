from rest_framework import serializers

class EmailDestinationSerializer(serializers.Serializer):
    type = serializers.ChoiceField(choices=["email"])
    address = serializers.EmailField()

class InAppDestinationSerializer(serializers.Serializer):
    type = serializers.ChoiceField(choices=["in_app"])
    user_id = serializers.UUIDField()

class PagerDutyDestinationSerializer(serializers.Serializer):
    type = serializers.ChoiceField(choices=["pagerduty"])
    integration_key = serializers.CharField(max_length=64)

class SlackDestinationSerializer(serializers.Serializer):
    type = serializers.ChoiceField(choices=["slack"])
    webhook_url = serializers.URLField()

    def validate_webhook_url(self, value):
        if "hooks.slack.com" not in value:
            raise serializers.ValidationError("Slack URL must be a hooks.slack.com domain")
        return value

def validate_destinations_list(destinations):
    if not isinstance(destinations, list):
        raise serializers.ValidationError("destinations must be a list of objects.")
    
    validated_data = []
    for item in destinations:
        if not isinstance(item, dict) or "type" not in item:
            raise serializers.ValidationError("Each destination must be an object with a 'type' field.")
        
        dtype = item["type"]
        if dtype == "email":
            serializer = EmailDestinationSerializer(data=item)
        elif dtype == "in_app":
            serializer = InAppDestinationSerializer(data=item)
        elif dtype == "pagerduty":
            serializer = PagerDutyDestinationSerializer(data=item)
        elif dtype == "slack":
            serializer = SlackDestinationSerializer(data=item)
        else:
            raise serializers.ValidationError(f"Unknown destination type: {dtype}")
        
        if not serializer.is_valid():
            raise serializers.ValidationError(f"Invalid {dtype} destination: {serializer.errors}")
        
        # Convert UUID to string for JSON serialization
        valid = serializer.validated_data
        if "user_id" in valid:
            valid["user_id"] = str(valid["user_id"])
            
        validated_data.append(valid)
        
    return validated_data
