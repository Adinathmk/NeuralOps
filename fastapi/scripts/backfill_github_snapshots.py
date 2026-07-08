import asyncio
import sys
import uuid
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

# Adjust the path so we can import from app
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.database.session import AsyncSessionLocal
from app.models.snapshots import TenantSnapshot
from app.models.github_integration_snapshots import GitHubIntegrationSnapshot


async def backfill():
    async with AsyncSessionLocal() as session:
        async with session.begin():
            # Query tenants that have the old github_repo_url set
            result = await session.execute(
                select(TenantSnapshot).where(TenantSnapshot.github_repo_url.isnot(None))
            )
            tenants = result.scalars().all()

            for tenant in tenants:
                # Check if an integration snapshot already exists for this tenant and repo
                existing = await session.execute(
                    select(GitHubIntegrationSnapshot).where(
                        GitHubIntegrationSnapshot.tenant_id == tenant.tenant_id,
                        GitHubIntegrationSnapshot.repo_url == tenant.github_repo_url,
                    )
                )
                if existing.scalar_one_or_none():
                    continue

                # Create the new integration snapshot
                integration = GitHubIntegrationSnapshot(
                    id=uuid.uuid4(),
                    tenant_id=tenant.tenant_id,
                    repo_url=tenant.github_repo_url,
                    repo_owner=tenant.github_repo_owner,
                    repo_name=tenant.github_repo_name,
                    installation_id=tenant.github_installation_id,
                    default_branch=tenant.github_default_branch or "main",
                    indexing_status=tenant.github_indexing_status or "pending",
                    last_indexed_commit=tenant.github_last_indexed_commit,
                    source_version=tenant.source_version,
                )
                session.add(integration)
                print(f"Migrated integration for tenant {tenant.tenant_id} - {tenant.github_repo_url}")
            
            print(f"Backfilled {len(tenants)} integrations.")


if __name__ == "__main__":
    asyncio.run(backfill())
