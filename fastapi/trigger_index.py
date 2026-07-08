import sys
sys.path.insert(0, r'c:\Users\ASUS\OneDrive\Desktop\NeuralOps\Backend\fastapi')
from app.worker.tasks.index_code import index_code

tenant_id = "6654ef13-8b08-40fc-9baf-9e9713a361db"
repo_url = "https://github.com/Adinathmk/Sdk-Test-Repo-Neuralops"

print(f"Triggering index_code for tenant {tenant_id} and repo {repo_url}...")
# Note: we use .delay() to send it to celery worker
index_code.delay(
    tenant_id=tenant_id,
    repo_url=repo_url,
    commit_sha="HEAD", # Use HEAD or we can check the database for the last commit
    is_initial=True
)
print("Task triggered!")
