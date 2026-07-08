import asyncio
import sys
sys.path.insert(0, r'c:\Users\ASUS\OneDrive\Desktop\NeuralOps\Backend\fastapi')
from app.database.session import SessionLocal
from sqlalchemy import text

async def main():
    async with SessionLocal() as db:
        res = await db.execute(text('SELECT count(*), status FROM incidents GROUP BY status'))
        print(res.fetchall())

asyncio.run(main())
