import asyncio
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.database.session import AsyncSessionLocal
from app.models.github_integration_snapshots import GitHubIntegrationSnapshot
from sqlalchemy import select
from app.models.snapshots import TenantSnapshot

async def run():
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(GitHubIntegrationSnapshot.id, GitHubIntegrationSnapshot.repo_url, GitHubIntegrationSnapshot.repo_name, GitHubIntegrationSnapshot.tenant_id))
        print("Snapshots:", result.fetchall())

asyncio.run(run())
