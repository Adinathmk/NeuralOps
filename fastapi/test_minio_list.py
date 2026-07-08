import asyncio
import aioboto3

async def test_cleanup():
    tenant_id_str = '6654ef13-8b08-40fc-9baf-9e9713a361db'
    repo_name = 'Sdk-Test-Repo-Neuralops'
    prefix = f'code/{tenant_id_str}/{repo_name}/'
    
    boto_session = aioboto3.Session()
    try:
        async with boto_session.client('s3', endpoint_url='http://minio:9000') as s3:
            paginator = s3.get_paginator('list_objects_v2')
            async for page in paginator.paginate(Bucket='neuralops-artifacts', Prefix=prefix):
                if 'Contents' in page:
                    print(f"Found {len(page['Contents'])} objects in Minio!")
                else:
                    print('No Contents found in page.')
    except Exception as exc:
        print(f'Exception: {exc}')

asyncio.run(test_cleanup())
