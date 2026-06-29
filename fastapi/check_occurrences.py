import asyncio
from app.database.session import AsyncSessionLocal
from app.models.incidents import Incident
from sqlalchemy import select

async def main():
    async with AsyncSessionLocal() as session:
        stmt = select(Incident.id, Incident.occurrences).order_by(Incident.created_at.desc())
        result = await session.execute(stmt)
        for row in result:
            print(row)

asyncio.run(main())
