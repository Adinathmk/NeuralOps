import asyncio

from sqlalchemy import text

from app.database.session import AsyncSessionLocal
from app.worker.tasks.embed_playbook import embed_playbook


async def main():
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(
                """
                SELECT p.playbook_id, p.tenant_id, p.source_version, p.error_pattern, p.instructions
                FROM playbook_snapshots p
                LEFT JOIN playbook_embeddings e ON p.playbook_id = e.playbook_id
                WHERE e.embedded_at IS NULL
            """
            )
        )
        rows = result.fetchall()
        for row in rows:
            print(f"Triggering embed for {row.playbook_id}...")
            embed_playbook.delay(
                playbook_id=str(row.playbook_id),
                tenant_id=str(row.tenant_id),
                plan_tier="standard",
                error_pattern=str(row.error_pattern),
                instructions=str(row.instructions),
                source_version=int(row.source_version) if row.source_version else 1,
            )
            print(f"Successfully embedded {row.playbook_id}!")


if __name__ == "__main__":
    asyncio.run(main())
