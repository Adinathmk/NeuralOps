import asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from app.models.incidents import Incident
from app.core.config import get_settings

engine = create_async_engine(get_settings().DATABASE_URL)
async_session = sessionmaker(engine, class_=AsyncSession)

async def main():
    async with async_session() as session:
        result = await session.execute(select(Incident.id))
        ids = [str(r[0]) for r in result]
        print(",".join(ids))

asyncio.run(main())
