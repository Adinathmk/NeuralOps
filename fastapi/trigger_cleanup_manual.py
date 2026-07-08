import asyncio
import asyncpg
import sys

async def main():
    conn = await asyncpg.connect('postgresql://neuralops_fastapi:fastapi_password@localhost:5434/neuralops_fastapi_db')
    tenant_id = '6654ef13-8b08-40fc-9baf-9e9713a361db'
    
    # 1. Delete incidents
    print("Deleting incidents...")
    await conn.execute("DELETE FROM incidents WHERE tenant_id = $1", tenant_id)
    
    # 2. Delete code index
    print("Deleting code index...")
    await conn.execute("DELETE FROM code_index WHERE tenant_id = $1", tenant_id)
    
    print("DB cleanup successful!")
    await conn.close()

if __name__ == '__main__':
    asyncio.run(main())
