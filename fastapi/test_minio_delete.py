import asyncio
import aioboto3
from botocore.exceptions import BotoCoreError, ClientError

async def test_cleanup():
    # Simulate the exact logic from _cleanup_index_async
    tenant_id_str = "6654ef13-8b08-40fc-9baf-9e9713a361db"
    repo_name = "Sdk-Test-Repo-Neuralops"
    prefix = f"code/{tenant_id_str}/{repo_name}/"
    
    boto_session = aioboto3.Session()
    try:
        async with boto_session.client(
            "s3", endpoint_url="http://minio:9000"
        ) as s3:
            print(f"Connected to S3. Checking prefix: {prefix}")
            paginator = s3.get_paginator("list_objects_v2")
            async for page in paginator.paginate(
                Bucket="neuralops-artifacts", Prefix=prefix
            ):
                if "Contents" in page:
                    print(f"Found {len(page['Contents'])} objects to delete.")
                    objects_to_delete = [
                        {"Key": obj["Key"]} for obj in page["Contents"]
                    ]
                    print(objects_to_delete[:2])
                    if objects_to_delete:
                        response = await s3.delete_objects(
                            Bucket="neuralops-artifacts",
                            Delete={"Objects": objects_to_delete},
                        )
                        print("Delete response:", response)
                else:
                    print("No Contents found in page.")
    except Exception as exc:
        print(f"Exception: {exc}")

if __name__ == "__main__":
    asyncio.run(test_cleanup())
