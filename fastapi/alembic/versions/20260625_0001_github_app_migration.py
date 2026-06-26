"""github_app_migration

Revision ID: a1b2c3d4e5f6
Revises: 98fe46d10645
Create Date: 2026-06-25 15:30:00.000000+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, None] = "98fe46d10645"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # DROP orphaned PAT columns
    op.drop_column("tenant_snapshots", "encrypted_github_pat")
    op.drop_column("tenant_snapshots", "github_webhook_secret")

    # ADD new installation ID column
    op.add_column(
        "tenant_snapshots",
        sa.Column(
            "github_installation_id",
            sa.BigInteger(),
            nullable=True,
            comment="GitHub App installation ID assigned when tenant installs the NeuralOps GitHub App",
        ),
    )

    op.create_index(
        "ix_tenant_snapshots_installation_id",
        "tenant_snapshots",
        ["github_installation_id"],
    )


def downgrade() -> None:
    # DROP installation ID column
    op.drop_index("ix_tenant_snapshots_installation_id", table_name="tenant_snapshots")
    op.drop_column("tenant_snapshots", "github_installation_id")

    # RESTORE PAT columns
    op.add_column(
        "tenant_snapshots",
        sa.Column(
            "github_webhook_secret",
            sa.Text(),
            nullable=True,
            comment="Fernet-encrypted webhook signing secret. Used to validate incoming push events from GitHub.",
        ),
    )
    op.add_column(
        "tenant_snapshots",
        sa.Column(
            "encrypted_github_pat",
            sa.Text(),
            nullable=True,
            comment="Fernet-encrypted GitHub Personal Access Token. Decrypt with the shared FERNET_ENCRYPTION_KEY at runtime. NEVER log or expose this value.",
        ),
    )
