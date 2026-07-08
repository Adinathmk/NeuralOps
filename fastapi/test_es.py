import asyncio
from app.database.elasticsearch_client import get_es_client

async def main():
    es = get_es_client()
    try:
        print(await es.info())
    except Exception as e:
        print('Error:', e)
    await es.close()

asyncio.run(main())
