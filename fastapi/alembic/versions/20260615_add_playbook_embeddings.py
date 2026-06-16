"""Add playbook_embeddings table with pgvector HNSW index and RLS policy.

Revision ID: c8d4f3e0b2f5
Revises: b7c3f2d9a1e4
Create Date: 2026-06-15
"""
from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector

revision = "c8d4f3e0b2f5"
down_revision = "b7c3f2d9a1e4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Enable pgvector extension
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # 2. Create table
    op.execute("""
        CREATE TABLE playbook_embeddings (
            id             UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
            playbook_id    UUID         NOT NULL UNIQUE,
            tenant_id      UUID         NOT NULL,
            embedding      vector(768)  NOT NULL,
            source_version BIGINT       NOT NULL,
            embedded_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

            CONSTRAINT fk_pb_playbook_snapshots
                FOREIGN KEY (playbook_id)
                REFERENCES playbook_snapshots(playbook_id)
                ON DELETE CASCADE
        )
    """)

    # 3. Supporting B-tree indexes
    op.execute("CREATE INDEX ON playbook_embeddings (tenant_id)")
    op.execute("CREATE INDEX ON playbook_embeddings (playbook_id)")
    op.execute("CREATE INDEX ON playbook_embeddings (tenant_id, source_version)")

    # 4. HNSW index for ANN search
    op.execute("""
        CREATE INDEX IF NOT EXISTS playbook_embeddings_hnsw_idx
        ON playbook_embeddings
        USING hnsw (embedding vector_cosine_ops)
        WITH (m = 16, ef_construction = 128)
    """)

    # 5. Row-Level Security
    op.execute("ALTER TABLE playbook_embeddings ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY playbook_embeddings_tenant_isolation
        ON playbook_embeddings
        USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
    """)


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS playbook_embeddings_tenant_isolation ON playbook_embeddings")
    op.execute("ALTER TABLE playbook_embeddings DISABLE ROW LEVEL SECURITY")
    op.execute("DROP TABLE IF EXISTS playbook_embeddings CASCADE")
