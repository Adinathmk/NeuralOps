"""add error_category to incidents

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-07-03 00:01:00.000000+00:00

Adds a pipeline-internal error_category column to the incidents table.
The column gates patch generation and security auto-promotion:

    code_bug            — auto-patchable programmer / logic errors
    database            — auto-patchable but complex DB layer errors
    infra_config        — ops intervention required; no patch generated
    external_dependency — upstream flakiness; no patch generated
    security            — always requires human triage before promotion
    unknown             — fallback for unrecognised error types

The column is NOT NULL with server_default='unknown' so the migration
is zero-downtime safe: existing rows receive 'unknown' immediately.
No backfill is needed because historical rows will simply show 'unknown'.

Zero-downtime ordering
-----------------------
1. Run this migration (adds nullable-with-default column + index).
2. Deploy FastAPI (classifier starts writing error_category).
3. Deploy Django consumer restart (starts consuming error_category from payload).
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# ---------------------------------------------------------------------------
# Revision identifiers
# ---------------------------------------------------------------------------

revision: str = "c3d4e5f6a7b8"
down_revision: Union[str, None] = "5fe1733c648b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ---------------------------------------------------------------------------
# Migrations
# ---------------------------------------------------------------------------


def upgrade() -> None:
    # Step 1: Add the column.
    # NOT NULL + server_default makes this safe on large tables — Postgres
    # rewrites no existing pages; existing rows read back 'unknown' via the
    # page-level default stored in pg_attrdef.
    op.add_column(
        "incidents",
        sa.Column(
            "error_category",
            sa.String(length=32),
            nullable=False,
            server_default="unknown",
            comment=(
                "Pipeline classification used to gate patch generation: "
                "code_bug | database | infra_config | external_dependency | "
                "security | unknown."
            ),
        ),
    )

    # Step 2: Create the composite index for filtered list queries
    # (e.g. GET /incidents?error_category=code_bug).
    # Using sa.text() for the DESC expression mirrors the existing severity index.
    op.create_index(
        "ix_incidents_tenant_category",
        "incidents",
        ["tenant_id", "error_category", sa.text("created_at DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_incidents_tenant_category", table_name="incidents")
    op.drop_column("incidents", "error_category")
