"""Recreate playbook_embeddings table (accidentally dropped by 98fe46d10645)

Revision ID: a1f9c3e7d2b8
Revises: 0fcd2cd2d1e6
Create Date: 2026-07-17

"""
from typing import Sequence, Union

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from alembic import op

revision: str = "a1f9c3e7d2b8"
down_revision: Union[str, None] = "0fcd2cd2d1e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.execute(
        """
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
    """
    )

    op.execute("CREATE INDEX ON playbook_embeddings (tenant_id)")
    op.execute("CREATE INDEX ON playbook_embeddings (playbook_id)")
    op.execute("CREATE INDEX ON playbook_embeddings (tenant_id, source_version)")

    op.execute(
        """
        CREATE INDEX IF NOT EXISTS playbook_embeddings_hnsw_idx
        ON playbook_embeddings
        USING hnsw (embedding vector_cosine_ops)
        WITH (m = 16, ef_construction = 128)
    """
    )

    op.execute("ALTER TABLE playbook_embeddings ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY playbook_embeddings_tenant_isolation
        ON playbook_embeddings
        USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
    """
    )


def downgrade() -> None:
    op.execute(
        "DROP POLICY IF EXISTS playbook_embeddings_tenant_isolation ON playbook_embeddings"
    )
    op.execute("ALTER TABLE playbook_embeddings DISABLE ROW LEVEL SECURITY")
    op.execute("DROP TABLE IF EXISTS playbook_embeddings CASCADE")
