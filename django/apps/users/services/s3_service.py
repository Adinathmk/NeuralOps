import logging
import boto3
from botocore.exceptions import ClientError
from django.conf import settings

logger = logging.getLogger(__name__)

class S3Service:
    @staticmethod
    def get_s3_client():
        return boto3.client(
            "s3",
            aws_access_key_id=getattr(settings, "AWS_ACCESS_KEY_ID", "minioadmin"),
            aws_secret_access_key=getattr(settings, "AWS_SECRET_ACCESS_KEY", "minioadminpassword"),
            endpoint_url=getattr(settings, "AWS_S3_ENDPOINT_URL", "http://minio:9000"),
            region_name=getattr(settings, "AWS_S3_REGION_NAME", "us-east-1"),
        )

    @staticmethod
    def get_presign_s3_client():
        # Uses the public endpoint URL so the frontend browser can resolve it.
        # In development, this is localhost:9000 instead of minio:9000.
        public_endpoint = getattr(settings, "AWS_S3_PUBLIC_ENDPOINT_URL", "http://localhost:9000")
        return boto3.client(
            "s3",
            aws_access_key_id=getattr(settings, "AWS_ACCESS_KEY_ID", "minioadmin"),
            aws_secret_access_key=getattr(settings, "AWS_SECRET_ACCESS_KEY", "minioadminpassword"),
            endpoint_url=public_endpoint,
            region_name=getattr(settings, "AWS_S3_REGION_NAME", "us-east-1"),
        )

    @staticmethod
    def get_bucket_name():
        return getattr(settings, "AWS_S3_BUCKET_NAME", "neuralops-artifacts")

    @classmethod
    def ensure_bucket_exists(cls):
        """
        Check if the bucket exists, and create it if not (useful for local MinIO).
        """
        s3_client = cls.get_s3_client()
        bucket_name = cls.get_bucket_name()
        try:
            s3_client.head_bucket(Bucket=bucket_name)
        except ClientError:
            try:
                s3_client.create_bucket(Bucket=bucket_name)
            except ClientError as e:
                logger.error(f"Could not create bucket {bucket_name}: {e}")

    @classmethod
    def generate_presigned_upload_url(cls, object_key: str, content_type: str, expiration=3600):
        """
        Generate a presigned PUT URL for uploading a file directly to S3/MinIO.
        """
        cls.ensure_bucket_exists()
        s3_client = cls.get_presign_s3_client()
        bucket_name = cls.get_bucket_name()

        try:
            # Using generate_presigned_url for PUT
            response = s3_client.generate_presigned_url(
                'put_object',
                Params={
                    'Bucket': bucket_name,
                    'Key': object_key,
                    'ContentType': content_type,
                },
                ExpiresIn=expiration,
            )
            return response
        except ClientError as e:
            logger.error(f"Failed to generate presigned upload URL: {e}")
            return None

    @classmethod
    def generate_presigned_get_url(cls, object_key: str, expiration=3600):
        """
        Generate a presigned GET URL for viewing a file.
        """
        if not object_key:
            return None
            
        s3_client = cls.get_presign_s3_client()
        bucket_name = cls.get_bucket_name()

        try:
            response = s3_client.generate_presigned_url(
                'get_object',
                Params={
                    'Bucket': bucket_name,
                    'Key': object_key,
                },
                ExpiresIn=expiration,
            )
            return response
        except ClientError as e:
            logger.error(f"Failed to generate presigned GET URL: {e}")
            return None

    @classmethod
    def delete_object(cls, object_key: str):
        """
        Delete an object from S3/MinIO.
        """
        if not object_key:
            return False
            
        s3_client = cls.get_s3_client()
        bucket_name = cls.get_bucket_name()

        try:
            s3_client.delete_object(
                Bucket=bucket_name,
                Key=object_key
            )
            return True
        except ClientError as e:
            logger.error(f"Failed to delete object from S3: {e}")
            return False
