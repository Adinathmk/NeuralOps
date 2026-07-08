import asyncio
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.worker.tasks.index_code import index_code

def run():
    tenant_id = "6654ef13-8b08-40fc-9baf-9e9713a361db"
    repo_url = "https://github.com/Adinathmk/ast-test-repo-For-neural-ops-code-indexing-"
    commit_sha = "61dfdae0d70b4060fc966ab0ef421cba6934ca68"
    
    print("Dispatching task...")
    result = index_code.delay(
        tenant_id=tenant_id,
        repo_url=repo_url,
        commit_sha=commit_sha,
        is_initial=True,
    )
    print(f"Dispatched task id: {result.id}")

if __name__ == "__main__":
    run()
