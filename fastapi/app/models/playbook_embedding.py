import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, ForeignKey, BigInteger, DateTime
from sqlalchemy.dialects.postgresql import UUID
from pgvector.sqlalchemy import Vector

from app.database.base import Base


class PlaybookEmbedding(Base):
    """
    SQLAlchemy model for playbook_embeddings.
    Owned by FastAPI / DB-2. Managed by Alembic.

    playbook_id has a UNIQUE constraint and an ON DELETE CASCADE FK to
    playbook_snapshots.playbook_id. Deleting a snapshot row automatically
    removes its embedding — no orphaned vectors are possible.

    The embedding column uses pgvector's Vector(1536) type, which maps to
    PostgreSQL's vector(1536) and supports the <=> cosine distance operator.
    """
    __tablename__ = "playbook_embeddings"

    id             = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    playbook_id    = Column(
                        UUID(as_uuid=True),
                        ForeignKey("playbook_snapshots.playbook_id", ondelete="CASCADE"),
                        nullable=False,
                        unique=True,
                     )
    tenant_id      = Column(UUID(as_uuid=True), nullable=False)
    embedding      = Column(Vector(768), nullable=False)
    source_version = Column(BigInteger, nullable=False)
    embedded_at    = Column(
                        DateTime(timezone=True),
                        nullable=False,
                        default=lambda: datetime.now(timezone.utc),
                     )
