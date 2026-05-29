"""
fastapi/app/models/code_index.py

DB-2 model: CodeIndex

Per-function / per-class AST metadata index for all GitHub-connected
repositories.  One row per symbol (function or class) per file.

The raw source file is stored in S3 (referenced via `s3_key`).  This
table stores only the *parsed structural metadata* — symbol names, line
ranges, call graphs, and import lists — so that the code retriever node
can perform fully deterministic lookups without vector arithmetic.

Retrieval pattern at incident time
------------------------------------
1. Query by `file_path` + `start_line`/`end_line` to find the crashed
   function.
2. Query by `file_path` for all functions in the call stack frames.
3. Query `calls[]` from the crashed function to fetch direct helpers.
4. Fetch the full source file from S3 using `s3_key` (or Redis L1 cache).
5. Slice `file_content.split('\\n')[start_line-1:end_line]` using the
   stored line ranges — no vector search, no GitHub API call.

Row-Level Security
------------------
TenantRLSMiddleware sets `app.tenant_id` on every Postgres connection.
The RLS policy (attached via the `after_create` DDL event listener at
the bottom of this module) enforces tenant isolation at the database
engine level, independently of any ORM-level filtering.

Architecture reference: NeuralOps Technical Documentation — Section 5
(DB-2 Schema — code_index), Section 17 (Code Indexing — Background).
"""

from __future__ import annotations

import uuid

from sqlalchemy import (
    Column,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.sql import func

from app.database.base import Base


class CodeIndex(Base):
    """
    Per-symbol AST metadata row for a single function or class.

    Columns
    -------
    id : UUID (PK)
        Globally unique row identifier.

    tenant_id : UUID
        Owning tenant.  Enforced via RLS policy — every query is
        automatically filtered to the current connection's tenant context.

    repo_url : Text
        Full HTTPS clone URL of the repository (e.g.
        ``https://github.com/my-org/my-repo``).

    file_path : Text
        Relative path of the source file within the repository root
        (e.g. ``src/payment/charge_service.py``).

    symbol_name : Text
        Fully-qualified symbol name as extracted by the AST parser
        (e.g. ``ChargeService`` or ``ChargeService.charge``).

    chunk_type : String(32)
        Either ``'function'`` or ``'class'``.

    start_line : Integer
        1-based line number where this symbol definition begins.

    end_line : Integer
        1-based line number where this symbol definition ends (inclusive).

    calls : ARRAY(Text)
        List of symbol names directly invoked inside this block,
        as extracted by the AST parser (e.g. ``['validate_card',
        'send_receipt']``).  External library calls are excluded by
        the indexer via the import list.

    called_by : ARRAY(Text)
        Reserved for future reverse call-graph population.  Currently
        written as an empty array by the indexer.

    imports : ARRAY(Text)
        Module-level import statements visible to this symbol, extracted
        by the AST parser (e.g. ``['decimal', 'stripe', 'myapp.models']``).
        Used by the retriever to filter out external-library calls from
        `calls[]`.

    s3_key : Text
        Full S3 object key of the raw source file.
        Format: ``code/{tenant_id}/{repo_name}/{commit_sha}/{file_path}``
        The full source file is fetched from this key by the code retriever
        node and cached in Redis (``code:{s3_key}``, TTL 24h).

    last_commit : Text
        40-character SHA of the commit at which this row was last indexed.
        Used by the incremental indexer to skip files that have not changed
        since the last push event.

    indexed_at : DateTime (timezone-aware)
        Server-side timestamp of the most recent upsert into this table.
    """

    __tablename__ = "code_index"

    # ── Primary key ───────────────────────────────────────────────────────────
    id: Column = Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        nullable=False,
        comment="Globally unique row identifier.",
    )

    # ── Tenant isolation ──────────────────────────────────────────────────────
    tenant_id: Column = Column(
        UUID(as_uuid=True),
        nullable=False,
        index=True,
        comment=(
            "Owning tenant UUID.  Enforced at the database layer by the RLS "
            "policy attached via the after_create DDL event listener."
        ),
    )

    # ── Repository & file location ────────────────────────────────────────────
    repo_url: Column = Column(
        Text,
        nullable=False,
        comment="Full HTTPS clone URL of the connected repository.",
    )
    file_path: Column = Column(
        Text,
        nullable=False,
        comment=(
            "Relative path of the source file within the repository root, "
            "e.g. src/payment/charge_service.py"
        ),
    )

    # ── Symbol metadata ───────────────────────────────────────────────────────
    symbol_name: Column = Column(
        Text,
        nullable=False,
        comment=(
            "Fully-qualified symbol name as extracted by the AST parser, "
            "e.g. ChargeService or ChargeService.charge"
        ),
    )
    chunk_type: Column = Column(
        String(32),
        nullable=True,
        comment="Symbol kind: 'function' or 'class'.",
    )

    # ── Line-range pointers ───────────────────────────────────────────────────
    start_line: Column = Column(
        Integer,
        nullable=False,
        comment="1-based line number where this symbol definition begins.",
    )
    end_line: Column = Column(
        Integer,
        nullable=False,
        comment=("1-based line number where this symbol definition ends (inclusive)."),
    )

    # ── Call-graph arrays ─────────────────────────────────────────────────────
    calls: Column = Column(
        ARRAY(Text),
        nullable=True,
        comment=(
            "Symbol names directly invoked inside this block, extracted by "
            "the AST parser.  External library calls are filtered out via "
            "the imports list before storage."
        ),
    )
    called_by: Column = Column(
        ARRAY(Text),
        nullable=True,
        comment=(
            "Reserved for future reverse call-graph population.  Written as "
            "an empty array by the indexer until Phase 4+ populates it."
        ),
    )
    imports: Column = Column(
        ARRAY(Text),
        nullable=True,
        comment=(
            "Module-level import statements visible to this symbol, used to "
            "distinguish project-internal calls from external library calls."
        ),
    )

    # ── S3 pointer & commit SHA ───────────────────────────────────────────────
    s3_key: Column = Column(
        Text,
        nullable=False,
        comment=(
            "Full S3 object key of the raw source file.  "
            "Format: code/{tenant_id}/{repo_name}/{commit_sha}/{file_path}"
        ),
    )
    last_commit: Column = Column(
        Text,
        nullable=False,
        comment=(
            "40-character SHA of the commit at which this row was last indexed. "
            "Used by the incremental indexer to skip unchanged files."
        ),
    )

    # ── Timestamp ─────────────────────────────────────────────────────────────
    indexed_at: Column = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
        comment="Server-side UTC timestamp of the most recent upsert.",
    )

    # ── Table-level constraints & indexes ─────────────────────────────────────
    __table_args__ = (
        # A single file cannot have duplicate symbols in the same repo / tenant.
        UniqueConstraint(
            "tenant_id",
            "repo_url",
            "file_path",
            "symbol_name",
            name="uq_code_index_tenant_repo_file_symbol",
        ),
        # Fast lookup by file path (used when indexing a pushed file and when
        # the code retriever locates the crashed function by file_path).
        Index(
            "ix_code_index_tenant_filepath",
            "tenant_id",
            "file_path",
        ),
        # Fast lookup by symbol name (used when resolving call-graph symbols).
        Index(
            "ix_code_index_tenant_symbol",
            "tenant_id",
            "symbol_name",
        ),
        # Combined lookup used by the incremental indexer to delete stale rows
        # for a specific file and by the retriever to list all symbols in a file.
        Index(
            "ix_code_index_tenant_repo_filepath",
            "tenant_id",
            "repo_url",
            "file_path",
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<CodeIndex "
            f"tenant={self.tenant_id} "
            f"symbol={self.symbol_name} "
            f"file={self.file_path} "
            f"lines={self.start_line}-{self.end_line}>"
        )


# ── Row-Level Security DDL listener ──────────────────────────────────────────
#
# Mirrors the exact pattern used for tenant_snapshots, alert_rule_snapshots,
# and playbook_snapshots in app/models/snapshots.py.
#
# When Alembic (or SQLAlchemy's create_all) creates the `code_index` table,
# this listener fires and immediately enables RLS + creates the isolation
# policy.  The policy enforces:
#
#     tenant_id::text = current_setting('app.tenant_id', true)
#
# `current_setting('app.tenant_id', true)` returns an empty string (not an
# error) when the setting is absent, so unauthenticated connections see zero
# rows rather than raising an exception — safe fail-closed behaviour.


def _create_rls_policies(target, connection, **kwargs) -> None:
    """
    DDL post-hook: enable RLS and create the tenant isolation policy on
    the `code_index` table immediately after it is created.

    Args:
        target:     The SQLAlchemy Table object that was just created.
        connection: The raw DBAPI connection used to issue DDL statements.
        **kwargs:   Additional keyword arguments forwarded by SQLAlchemy
                    (ignored here).
    """
    table_name: str = target.name  # "code_index"

    # Enable RLS — rows are now filtered for all roles including table owner.
    connection.execute(text(f"ALTER TABLE {table_name} ENABLE ROW LEVEL SECURITY;"))
    # FORCE RLS applies the policy even to the table owner, preventing
    # accidental cross-tenant reads from service account queries.
    connection.execute(text(f"ALTER TABLE {table_name} FORCE ROW LEVEL SECURITY;"))

    policy_name = f"rls_{table_name}_tenant_isolation"

    # Drop any pre-existing policy (idempotent: safe to re-run).
    connection.execute(text(f"DROP POLICY IF EXISTS {policy_name} ON {table_name};"))

    # Create the permissive SELECT/INSERT/UPDATE/DELETE policy.
    # The second argument `true` to current_setting() means "missing_ok" —
    # returns '' rather than raising an error if the variable is not set.
    connection.execute(
        text(
            f"""
            CREATE POLICY {policy_name} ON {table_name}
            USING (tenant_id::text = current_setting('app.tenant_id', true));
            """
        )
    )


# Register the listener against the Table object (not the mapper class) so
# it fires during both `create_all` and Alembic's `op.create_table`.
event.listen(
    CodeIndex.__table__,
    "after_create",
    _create_rls_policies,
)
