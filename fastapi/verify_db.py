"""
fastapi/verify_db.py
Simple database query script to verify the indexed symbols in DB-2.
"""

import asyncio
from sqlalchemy import select
from app.database.session import AsyncSessionLocal
from app.models.code_index import CodeIndex
from app.models.snapshots import TenantSnapshot

async def run():
    async with AsyncSessionLocal() as session:
        # Check Tenant status
        res_t = await session.execute(select(TenantSnapshot))
        tenants = res_t.scalars().all()
        print("\n--- Seeded Tenants ---")
        for t in tenants:
            print(f"ID: {t.tenant_id} | Repo: {t.github_repo_owner}/{t.github_repo_name} | Status: {t.github_indexing_status} | Commit: {t.github_last_indexed_commit}")

        # Check Code Index symbols
        res_c = await session.execute(select(CodeIndex))
        symbols = res_c.scalars().all()
        print(f"\n--- Extracted AST Symbols ({len(symbols)}) ---")
        for s in symbols:
            print(f"- Symbol: {s.symbol_name:18} | Type: {s.chunk_type:8} | File: {s.file_path} | Commit: {s.last_commit}")

if __name__ == "__main__":
    asyncio.run(run())
