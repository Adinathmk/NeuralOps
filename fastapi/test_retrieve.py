import asyncio
import logging
from app.services.embedding_service import embed_text, build_query_embed_text
from app.repositories.playbook_vector_repository import search_similar_playbooks
from app.database.session import AsyncSessionLocal
from sqlalchemy import text

logging.getLogger("sqlalchemy.engine.Engine").setLevel(logging.WARNING)

async def main():
    tenant_id = "6654ef13-8b08-40fc-9baf-9e9713a361db"
    
    # Simulate an incident that should trigger the "None Type" playbook
    incident_error_type = "TypeError"
    incident_service = "api-gateway"
    incident_file = "utils.py"
    incident_stack = "TypeError: 'NoneType' object is not subscriptable"
    
    print("\n--- Simulating Incoming Error ---")
    print(f"Error: {incident_error_type} in {incident_service}")
    print(f"Stack Trace: {incident_stack}")
    
    print("\n--- Generating Query Embedding & Searching Vector DB ---")
    query_text = build_query_embed_text(
        error_type=incident_error_type,
        stack_trace_summary=incident_stack,
        service_name=incident_service,
        file_path=incident_file
    )
    query_vector = embed_text(query_text)
    
    results = await search_similar_playbooks(
        tenant_id=tenant_id,
        query_vector=query_vector,
        top_k=3,
        distance_threshold=0.60
    )
    
    print("\n" + "="*60)
    print("🎯 SEARCH RESULTS (Ranked by Semantic Match)")
    print("="*60)
    
    if not results:
        print("❌ No matches found!")
    else:
        async with AsyncSessionLocal() as session:
            for rank, result in enumerate(results, 1):
                # Fetch original playbook details
                db_res = await session.execute(
                    text("SELECT error_pattern, instructions FROM playbook_snapshots WHERE playbook_id = :pid"),
                    {"pid": result["playbook_id"]}
                )
                row = db_res.fetchone()
                if row:
                    confidence = result["similarity"] * 100
                    print(f"\n[Rank {rank}] Match Confidence: {confidence:.2f}%")
                    print(f"Playbook Trigger: {row.error_pattern}")
                    print(f"Instructions: {row.instructions}")

if __name__ == "__main__":
    asyncio.run(main())
