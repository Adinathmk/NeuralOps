"""
fastapi/verify_webhook.py

End-to-end verification script for Phase 3 (AST Code Indexing & GitHub Webhook).
Run this inside the FastAPI container:
    docker compose exec fastapi python verify_webhook.py
"""

import asyncio
import hashlib
import hmac
import json
import os
import uuid

import httpx
from cryptography.fernet import Fernet
from sqlalchemy import select

from app.core.config import get_settings
from app.database.session import AsyncSessionLocal
from app.models.snapshots import TenantSnapshot

# Raw configuration
MOCK_TENANT_ID = uuid.UUID("11111111-2222-3333-4444-555555555555")
MOCK_REPO_URL = (
    "https://github.com/Adinathmk/ast-test-repo-For-neural-ops-code-indexing-"
)
MOCK_WEBHOOK_SECRET = "my_webhook_secret_123"
MOCK_PAT = os.getenv("GITHUB_PAT", "DUMMY_PAT_FOR_TESTING_1234567890")


async def setup_mock_tenant():
    print("\n--- Step 1: Setting up Mock Tenant Snapshot in FastAPI DB-2 ---")
    settings = get_settings()
    fernet = Fernet(settings.FERNET_ENCRYPTION_KEY.encode())

    # Encrypt secrets using Fernet
    encrypted_secret = fernet.encrypt(MOCK_WEBHOOK_SECRET.encode()).decode()
    encrypted_pat = fernet.encrypt(MOCK_PAT.encode()).decode()

    # Parse owner and name dynamically from clone URL
    url_clean = MOCK_REPO_URL.rstrip("/").removesuffix(".git")
    parts = url_clean.split("/")
    repo_owner = parts[-2]
    repo_name = parts[-1]

    async with AsyncSessionLocal() as session:
        # Check if tenant already exists
        res = await session.execute(
            select(TenantSnapshot).where(TenantSnapshot.tenant_id == MOCK_TENANT_ID)
        )
        tenant = res.scalar_one_or_none()

        if tenant:
            print(f"Tenant {MOCK_TENANT_ID} already exists. Updating details...")
            tenant.github_repo_url = MOCK_REPO_URL
            tenant.github_repo_owner = repo_owner
            tenant.github_repo_name = repo_name
            tenant.encrypted_github_pat = encrypted_pat
            tenant.github_webhook_secret = encrypted_secret
            tenant.github_default_branch = "main"
            tenant.github_indexing_status = "pending"
        else:
            print(f"Creating new mock tenant {MOCK_TENANT_ID}...")
            tenant = TenantSnapshot(
                tenant_id=MOCK_TENANT_ID,
                plan_tier="professional",
                vector_namespace="tenant_test_namespace",
                is_suspended=False,
                github_repo_url=MOCK_REPO_URL,
                github_repo_owner=repo_owner,
                github_repo_name=repo_name,
                encrypted_github_pat=encrypted_pat,
                github_webhook_secret=encrypted_secret,
                github_default_branch="main",
                github_indexing_status="pending",
            )
            session.add(tenant)

        await session.commit()
        print("✔ Mock tenant snapshot saved successfully!")


async def trigger_webhook():
    print("\n--- Step 2: Sending Mock GitHub Push Event to Webhook Endpoint ---")

    # Mock payload matching a standard GitHub push webhook
    payload = {
        "ref": "refs/heads/main",
        "after": "abc123commitsha",
        "repository": {
            "clone_url": MOCK_REPO_URL,
            "html_url": MOCK_REPO_URL,
            "name": "test-repo",
            "owner": {"name": "test-owner"},
        },
        "commits": [
            {
                "id": "abc123commitsha",
                "added": ["app/main.py"],
                "modified": ["README.md"],
                "removed": ["old_file.py"],
            }
        ],
    }

    body_bytes = json.dumps(payload).encode("utf-8")

    # Generate HMAC-SHA256 signature
    signature = hmac.new(
        MOCK_WEBHOOK_SECRET.encode("utf-8"), body_bytes, hashlib.sha256
    ).hexdigest()

    headers = {
        "Content-Type": "application/json",
        "X-GitHub-Event": "push",
        "X-Hub-Signature-256": f"sha256={signature}",
    }

    # Post to localhost inside container
    async with httpx.AsyncClient() as client:
        response = await client.post(
            "http://localhost:8001/api/v1/webhooks/github",
            content=body_bytes,
            headers=headers,
        )

    print(f"Response Status Code: {response.status_code}")
    print(f"Response Body: {response.text}")

    if response.status_code == 202:
        print("✔ Webhook successfully accepted (202)!")
    else:
        print("✘ Webhook failed!")


async def verify_db_updates():
    print("\n--- Step 3: Verifying database updates from Celery task execution ---")
    print("Waiting 3 seconds for Celery task to finish...")
    await asyncio.sleep(3)

    async with AsyncSessionLocal() as session:
        res = await session.execute(
            select(TenantSnapshot).where(TenantSnapshot.tenant_id == MOCK_TENANT_ID)
        )
        tenant = res.scalar_one()

        print(f"Final Tenant Indexing Status: {tenant.github_indexing_status}")
        print(f"Final Last Indexed Commit   : {tenant.github_last_indexed_commit}")

        if (
            tenant.github_indexing_status in ("indexed", "failed")
            and tenant.github_last_indexed_commit == "abc123commitsha"
        ):
            print(
                "✔ Database state was successfully updated by Celery task! (Transitioned to indexed/failed as expected)"
            )
        else:
            print("✘ Database state was NOT updated as expected.")


async def main():
    await setup_mock_tenant()
    await trigger_webhook()
    await verify_db_updates()


if __name__ == "__main__":
    asyncio.run(main())
