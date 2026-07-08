import asyncio
from app.services.log_search_repository import LogSearchRepository
from app.database.elasticsearch_client import get_es_client, close_es_client

async def main():
    repo = LogSearchRepository()
    try:
        vol = await repo.count_volume(tenant_id='6654ef13-8b08-40fc-9baf-9e9713a361db', plan_tier='standard', time_window='24h')
        print('Volume:', vol)
    except Exception as e:
        print('Error:', e)
    await close_es_client()

asyncio.run(main())
