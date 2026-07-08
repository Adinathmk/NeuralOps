import asyncio
import asyncpg
import sys

async def main():
    conn = await asyncpg.connect('postgresql://neuralops_fastapi:fastapi_password@localhost:5434/neuralops_fastapi_db')
    rows = await conn.fetch('SELECT id, repo_url FROM github_integration_snapshots')
    print([dict(r) for r in rows])
    await conn.close()

if __name__ == '__main__':
    asyncio.run(main())
