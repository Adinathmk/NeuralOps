import asyncio
import sys
import asyncpg
import uuid

async def main():
    conn = await asyncpg.connect('postgresql://neuralops:neuralops_password@localhost:5433/neuralops_db')
    
    tenant_id = '3cf86985-1d48-4e1b-90e6-a05d8f6d70bc'
    draft_id = '1697ef6e-2688-4326-8f1e-9d78f96b90de'
    
    await conn.execute('''
        INSERT INTO incident_snapshots (tenant_id, incident_id, fingerprint, status, severity, error_type, service_name, environment, source_version)
        VALUES ($1, $2, 'test_draft_fingerprint', 'draft', 'unknown', 'DatabaseConnectionError', 'auth-service', 'production', '1.0.0')
        ON CONFLICT DO NOTHING
    ''', str(tenant_id), str(draft_id))
    
    print(f"Inserted mock draft incident into Django DB {draft_id}")
    await conn.close()

if __name__ == '__main__':
    asyncio.run(main())
