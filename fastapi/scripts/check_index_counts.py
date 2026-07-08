import asyncio
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.database.session import AsyncSessionLocal
from app.models.code_index import CodeIndex
from sqlalchemy import select, func

async def run():
    async with AsyncSessionLocal() as session:
        res = await session.execute(select(CodeIndex.repo_url, func.count(CodeIndex.id)).group_by(CodeIndex.repo_url))
        print("Index counts:", res.fetchall())

asyncio.run(run())
