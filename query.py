import asyncio
import asyncpg

async def main():
    conn = await asyncpg.connect('postgresql://neuralops_fastapi:fastapi_password@db-fastapi:5432/neuralops_fastapi_db')
    rows = await conn.fetch('SELECT id, severity_filter, destinations FROM alert_rule_snapshots')
    for r in rows:
        print(dict(r))
    await conn.close()

if __name__ == "__main__":
    asyncio.run(main())
