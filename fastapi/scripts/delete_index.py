import asyncio
import sys
import os
import uuid

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.database.session import AsyncSessionLocal
from app.models.code_index import CodeIndex
from sqlalchemy import delete

async def run():
    tenant_id = uuid.UUID("6654ef13-8b08-40fc-9baf-9e9713a361db")
    repo_url = "https://github.com/Adinathmk/ast-test-repo-For-neural-ops-code-indexing-"
    
    async with AsyncSessionLocal() as session:
        async with session.begin():
            await session.execute(
                delete(CodeIndex).where(
                    CodeIndex.tenant_id == tenant_id,
                    CodeIndex.repo_url == repo_url,
                )
            )
        print("Deleted rows for repo.")

asyncio.run(run())
