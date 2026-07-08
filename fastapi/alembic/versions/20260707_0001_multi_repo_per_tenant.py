"""multi_repo_per_tenant

Revision ID: c4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-07-07 00:01:00.000000+00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'c4e5f6a7b8c9'
down_revision: Union[str, None] = 'c3d4e5f6a7b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Create table
    op.create_table('github_integration_snapshots',
    sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column('tenant_id', postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column('repo_url', sa.Text(), nullable=False),
    sa.Column('repo_owner', sa.String(length=255), nullable=False),
    sa.Column('repo_name', sa.String(length=255), nullable=False),
    sa.Column('installation_id', sa.BigInteger(), nullable=True),
    sa.Column('default_branch', sa.String(length=255), nullable=False),
    sa.Column('indexing_status', sa.String(length=20), nullable=False, server_default='pending'),
    sa.Column('last_indexed_commit', sa.String(length=40), nullable=True),
    sa.Column('source_version', sa.BigInteger(), nullable=False, server_default='1'),
    sa.Column('synced_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenant_snapshots.tenant_id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    
    # 2. Create index for webhook lookups
    op.create_index(op.f('ix_github_integration_snapshots_tenant_id'), 'github_integration_snapshots', ['tenant_id'], unique=False)
    op.create_index('ix_github_integration_snapshots_tenant_repo', 'github_integration_snapshots', ['tenant_id', 'repo_url'], unique=True)
    
    # 3. Enable RLS
    op.execute("ALTER TABLE github_integration_snapshots ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE github_integration_snapshots FORCE ROW LEVEL SECURITY;")
    op.execute("""
        CREATE POLICY tenant_isolation_policy ON github_integration_snapshots
        AS PERMISSIVE FOR ALL
        USING (
            current_setting('app.bypass_rls', true) = 'on'
            OR tenant_id::text = current_setting('app.tenant_id', true)
        )
        WITH CHECK (
            current_setting('app.bypass_rls', true) = 'on'
            OR tenant_id::text = current_setting('app.tenant_id', true)
        );
    """)


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation_policy ON github_integration_snapshots;")
    op.execute("ALTER TABLE github_integration_snapshots DISABLE ROW LEVEL SECURITY;")
    op.drop_index('ix_github_integration_snapshots_tenant_repo', table_name='github_integration_snapshots')
    op.drop_index(op.f('ix_github_integration_snapshots_tenant_id'), table_name='github_integration_snapshots')
    op.drop_table('github_integration_snapshots')
