import asyncio
import uuid
from sqlalchemy import select
from app.database.session import AsyncSessionLocal
from app.models.snapshots import TenantSnapshot
from app.worker.tasks.index_code import index_code

async def main():
    tenant_id_str = '6654ef13-8b08-40fc-9baf-9e9713a361db'
    tenant_id = uuid.UUID(tenant_id_str)
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(TenantSnapshot).where(TenantSnapshot.tenant_id == tenant_id))
        snap = result.scalar_one_or_none()
        
    if not snap:
        print("Tenant not found")
        return
        
    repo_url = snap.github_repo_url
    print(f"Dispatching index_code for {repo_url}")
    
    result = index_code.delay(
        tenant_id=tenant_id_str,
        repo_url=repo_url,
        commit_sha='d89103b13f03e166381578b9085056474c348aee',
        changed_files=['app/services/shipping_service.py'],
        removed_files=[],
        is_initial=False
    )
    print(f"Task dispatched! Task ID: {result.id}")

if __name__ == '__main__':
    asyncio.run(main())
