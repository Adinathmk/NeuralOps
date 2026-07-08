import asyncio
import asyncpg
import sys

async def main():
    conn = await asyncpg.connect('postgresql://neuralops_django:django_password@localhost:5433/neuralops_django_db')
    tenant_id = '6654ef13-8b08-40fc-9baf-9e9713a361db'
    
    # Delete incident snapshots
    print("Deleting incident snapshots...")
    await conn.execute("DELETE FROM analytics_incidentsnapshot WHERE tenant_id = $1", tenant_id)
    
    print("Django DB cleanup successful!")
    await conn.close()

if __name__ == '__main__':
    asyncio.run(main())
