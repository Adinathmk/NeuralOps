import asyncio
import sys
import asyncpg
import uuid

async def main():
    conn = await asyncpg.connect('postgresql://neuralops_fastapi:fastapi_password@localhost:5434/neuralops_fastapi_db')
    
    tenant_id = '3cf86985-1d48-4e1b-90e6-a05d8f6d70bc' # Usually the first tenant
    
    # We will get a tenant ID from the existing incidents
    rows = await conn.fetch('SELECT tenant_id FROM incidents LIMIT 1')
    if rows:
        tenant_id = rows[0]['tenant_id']
        
    draft_id = str(uuid.uuid4())
    
    await conn.execute('''
        INSERT INTO incidents (id, tenant_id, fingerprint, status, severity, error_category, error_type, service_name, environment, occurrence_count)
        VALUES ($1, $2, 'test_draft_fingerprint', 'draft', 'unknown', 'unknown', 'DatabaseConnectionError', 'auth-service', 'production', 1)
    ''', draft_id, str(tenant_id))
    
    print(f"Inserted mock draft incident {draft_id}")
    await conn.close()

if __name__ == '__main__':
    asyncio.run(main())
