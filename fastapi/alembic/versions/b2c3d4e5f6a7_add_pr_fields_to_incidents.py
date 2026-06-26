"""
alembic/versions/b2c3d4e5f6a7_add_pr_fields_to_incidents.py

Add pr_url, pr_number, pr_status, structured_patch columns to incidents.

Revision:      b2c3d4e5f6a7
Down revision: a1b2c3d4e5f6
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b2c3d4e5f6a7"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "incidents",
        sa.Column(
            "pr_url",
            sa.Text(),
            nullable=True,
            comment="HTML URL of the GitHub PR created by NeuralOps.",
        ),
    )
    op.add_column(
        "incidents",
        sa.Column(
            "pr_number",
            sa.Integer(),
            nullable=True,
            comment="GitHub PR number.",
        ),
    )
    op.add_column(
        "incidents",
        sa.Column(
            "pr_status",
            sa.String(length=32),
            nullable=True,
            comment=(
                "PR lifecycle status: "
                "open | skipped | no_patch | syntax_error | failed."
            ),
        ),
    )
    op.add_column(
        "incidents",
        sa.Column(
            "structured_patch",
            sa.Text(),
            nullable=True,
            comment=(
                "JSON string of validated search/replace patches "
                "from PatchGeneratorNode."
            ),
        ),
    )


def downgrade() -> None:
    op.drop_column("incidents", "structured_patch")
    op.drop_column("incidents", "pr_status")
    op.drop_column("incidents", "pr_number")
    op.drop_column("incidents", "pr_url")
