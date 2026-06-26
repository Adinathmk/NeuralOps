import asyncio
import os
import sys

# add path to sys
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from app.database.session import AsyncSessionLocal
from app.models.incidents import Incident, Analysis
from sqlalchemy import select

async def main():
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Incident).order_by(Incident.created_at.desc()).limit(1)
        )
        incident = result.scalar_one_or_none()
        if not incident:
            print("No incidents found")
            return
            
        print(f"Incident ID: {incident.id}")
        print(f"Error Type: {incident.error_type}")
        print(f"Status: {incident.status}")
        print(f"Is Draft: {incident.is_draft}")
        print(f"Has Structured Patch: {bool(incident.structured_patch)}")
        print(f"PR Status: {incident.pr_status}")
        
        analysis_result = await session.execute(
            select(Analysis).where(Analysis.incident_id == incident.id)
        )
        analysis = analysis_result.scalar_one_or_none()
        if analysis:
            print(f"\nPatch Generator Results:")
            print(analysis.node_results.get("patch_generator", "NOT PRESENT"))
            print(f"Code Retriever Results:")
            print(analysis.node_results.get("code_retriever", "NOT PRESENT"))

if __name__ == "__main__":
    asyncio.run(main())
