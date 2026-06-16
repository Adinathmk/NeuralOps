import os
import time

import boto3
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .serializers import RegisterDeviceTokenSerializer

# DynamoDB uses its own dedicated credentials (real AWS) so it never
# accidentally inherits the MinIO endpoint that S3 uses.
_dynamo_region = os.getenv("DYNAMODB_REGION") or os.getenv(
    "AWS_REGION_NAME", "us-east-1"
)
_dynamo_key = os.getenv("DYNAMODB_ACCESS_KEY_ID") or os.getenv("AWS_ACCESS_KEY_ID")
_dynamo_secret = os.getenv("DYNAMODB_SECRET_ACCESS_KEY") or os.getenv(
    "AWS_SECRET_ACCESS_KEY"
)
_dynamo_endpoint = os.getenv("DYNAMODB_ENDPOINT_URL") or None  # None = real AWS

dynamodb = boto3.resource(
    "dynamodb",
    region_name=_dynamo_region,
    endpoint_url=_dynamo_endpoint,
    aws_access_key_id=_dynamo_key,
    aws_secret_access_key=_dynamo_secret,
)
TABLE_NAME = "neuralops-device-tokens"


class DeviceTokenView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = RegisterDeviceTokenSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        data = serializer.validated_data
        table = dynamodb.Table(TABLE_NAME)

        user_id = str(request.user.id)
        tenant_id = str(request.user.tenant_id)
        role = getattr(request.user, "role", "engineer")

        # update_or_create logic: PutItem automatically overwrites if the Key exists
        table.put_item(
            Item={
                "tenant_id": tenant_id,
                "sk": f"{user_id}#{data['device_id']}",
                "role": role,
                "platform": data["platform"],
                "provider": "fcm",
                "device_token": data["device_token"],
                "is_active": True,
                "updated_at": int(time.time()),
            }
        )
        return Response({"status": "registered"}, status=status.HTTP_200_OK)

    def delete(self, request, device_id):
        table = dynamodb.Table(TABLE_NAME)
        # Instead of deleting, mark it inactive for audit history
        table.update_item(
            Key={
                "tenant_id": str(request.user.tenant_id),
                "sk": f"{request.user.id}#{device_id}",
            },
            UpdateExpression="SET is_active = :val, updated_at = :now",
            ExpressionAttributeValues={":val": False, ":now": int(time.time())},
        )
        return Response(status=status.HTTP_204_NO_CONTENT)
