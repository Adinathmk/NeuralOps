from rest_framework import serializers


class RegisterDeviceTokenSerializer(serializers.Serializer):
    platform = serializers.ChoiceField(choices=["ios", "android", "web"])
    device_token = serializers.CharField()  # The raw FCM or APNs token string
    device_id = serializers.CharField()  # Stable client UUID
