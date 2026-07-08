import asyncio
import asyncpg
import sys

async def main():
    conn = await asyncpg.connect('postgresql://neuralops_fastapi:fastapi_password@localhost:5434/neuralops_fastapi_db')
    rows = await conn.fetch("SELECT id, error_type FROM incidents WHERE tenant_id = '6654ef13-8b08-40fc-9baf-9e9713a361db'")
    print([dict(r) for r in rows])
    await conn.close()

if __name__ == '__main__':
    asyncio.run(main())
