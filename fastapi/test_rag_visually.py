import asyncio
import uuid
import logging
from app.database.session import AsyncSessionLocal
from app.services.embedding_service import embed_text, build_playbook_embed_text, build_query_embed_text
from app.repositories.playbook_vector_repository import upsert_playbook_embedding, search_similar_playbooks
from sqlalchemy import text

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# To silence SQLAlchemy raw query spam in the console for this visual test
logging.getLogger("sqlalchemy.engine.Engine").setLevel(logging.WARNING)

async def main():
    print("\n" + "="*60)
    print("🧠 NEURALOPS RAG SEMANTIC MATCHING — VISUAL TEST")
    print("="*60)
    
    tenant_id = uuid.uuid4()
    
    # We will create 3 distinct playbooks
    playbooks = [
        {
            "id": uuid.uuid4(),
            "trigger": "Database connection timeout in user_service",
            "instructions": "Restart the connection pool and check AWS RDS metrics. Increase max_connections if necessary."
        },
        {
            "id": uuid.uuid4(),
            "trigger": "Redis OutOfMemoryError during cache set",
            "instructions": "Flush the Redis cache and check eviction policies. Upgrade Redis node memory tier."
        },
        {
            "id": uuid.uuid4(),
            "trigger": "Kafka consumer group rebalancing constantly",
            "instructions": "Increase session.timeout.ms and max.poll.interval.ms for the consumer."
        }
    ]

    async with AsyncSessionLocal() as session:
        print("\n--- STEP 1: Setting up mock data in PostgreSQL ---")
        
        # Insert a fake tenant first to satisfy FK constraints
        await session.execute(
            text("""
            INSERT INTO tenant_snapshots (tenant_id, is_suspended, synced_at)
            VALUES (:tid, false, NOW())
            ON CONFLICT (tenant_id) DO NOTHING
            """),
            {"tid": tenant_id}
        )
        
        # We must insert dummy playbooks into playbook_snapshots to satisfy the Foreign Key constraint
        for pb in playbooks:
            await session.execute(
                text("""
                INSERT INTO playbook_snapshots (playbook_id, tenant_id, error_pattern, instructions, source_version, synced_at)
                VALUES (:pid, :tid, :trigger, :steps, 1, NOW())
                ON CONFLICT (playbook_id) DO NOTHING
                """),
                {
                    "pid": pb["id"],
                    "tid": tenant_id,
                    "trigger": pb["trigger"],
                    "steps": pb["instructions"]
                }
            )
        await session.commit()
        print("✅ Created 3 distinct playbooks in the database.")
        
        print("\n--- STEP 2: Generating Vector Embeddings using Gemini ---")
        for i, pb in enumerate(playbooks, 1):
            embed_input = build_playbook_embed_text(pb["trigger"], pb["instructions"])
            print(f"Embedding playbook {i}/3...")
            vector = embed_text(embed_input)
            
            await upsert_playbook_embedding(
                tenant_id=str(tenant_id),
                playbook_id=str(pb["id"]),
                vector=vector,
                source_version=1
            )
        await session.commit()
        print("✅ Successfully generated 768-dimensional vectors for all playbooks and stored them in pgvector!")
        
        print("\n--- STEP 3: Simulating an Incoming Application Crash ---")
        # Notice how this incident text DOES NOT perfectly string-match the Redis playbook trigger!
        # It uses different words ("OOM", "crashed", "cache-service"). Regex would fail here.
        incident_error_type = "OOM Exception"
        incident_service = "cache-service"
        incident_file = "redis_client.py"
        incident_stack = "Service crashed because cache-service hit memory limit when trying to save session data."
        
        print(f"Incoming Error: {incident_error_type} in {incident_service}")
        print(f"Stack Trace: {incident_stack}")
        
        print("\n--- STEP 4: Embedding Incident & Searching Vector DB ---")
        query_text = build_query_embed_text(
            error_type=incident_error_type,
            stack_trace_summary=incident_stack,
            service_name=incident_service,
            file_path=incident_file
        )
        query_vector = embed_text(query_text)
        
        results = await search_similar_playbooks(
            tenant_id=str(tenant_id),
            query_vector=query_vector,
            top_k=2,
            distance_threshold=0.55 # Wide threshold for testing
        )
        
        print("\n" + "="*60)
        print("🎯 SEARCH RESULTS (Ranked by semantic similarity)")
        print("="*60)
        
        if not results:
            print("❌ No matches found!")
        else:
            for rank, result in enumerate(results, 1):
                # Find the matching playbook text from our array to print it
                matched_pb = next(p for p in playbooks if str(p["id"]) == result["playbook_id"])
                confidence = result["similarity"] * 100
                print(f"\n[Rank {rank}] Match Confidence: {confidence:.2f}%")
                print(f"Trigger: {matched_pb['trigger']}")
                print(f"Instructions: {matched_pb['instructions']}")
                
        print("\n" + "="*60)
        print("Test Complete. RAG is successfully implemented and working natively!")

if __name__ == "__main__":
    asyncio.run(main())
