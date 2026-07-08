import asyncio
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.worker.tasks.index_code import cleanup_code_index

def run():
    tenant_id = "6654ef13-8b08-40fc-9baf-9e9713a361db"
    repo_url = "https://github.com/Adinathmk/ast-test-repo-For-neural-ops-code-indexing-"
    repo_name = "ast-test-repo-For-neural-ops-code-indexing-"
    
    print("Dispatching cleanup task for specific repo...")
    result = cleanup_code_index.delay(
        tenant_id=tenant_id,
        repo_url=repo_url,
        repo_name=repo_name,
    )
    print(f"Dispatched task id: {result.id}")

if __name__ == "__main__":
    run()
