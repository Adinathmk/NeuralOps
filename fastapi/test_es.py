import asyncio

from elasticsearch import AsyncElasticsearch

from app.services.log_event_indexer import LogEventIndexer


async def main():
    es = AsyncElasticsearch("http://elasticsearch:9200")
    idx = LogEventIndexer(es_client=es)
    try:
        await idx.update_parsed_fields(
            "84fd3233-50fc-4acb-86bf-5a7bb444ef30",
            "6654ef13-8b08-40fc-9baf-9e9713a361db",
            "standard",
            "TypeError",
            "services.py",
            12,
            "error",
        )
        print("success")
    except Exception as e:
        print("ERROR IS:", repr(e))
    finally:
        await es.close()


if __name__ == "__main__":
    asyncio.run(main())
