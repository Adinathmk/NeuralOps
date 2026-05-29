"""
app/models/outbox.py

Transactional Outbox table for DB-2 (FastAPI-owned PostgreSQL).

Architecture rationale
----------------------
FastAPI publishes Kafka events by writing rows to this table *inside the
same SQLAlchemy transaction* as the business-logic write (e.g. creating
an Incident).  Debezium tails the DB-2 PostgreSQL WAL and delivers outbox
rows to Kafka automatically.

This eliminates the "dual-write" problem: if the service crashes after
writing to the database but before a direct Kafka publish, the event is
still delivered because Debezium will pick it up from the WAL on restart.

Topics used by FastAPI outbox events (Phase 1 — config sync scope):
  • incidents.created      (Phase 4+)
  • incidents.analyzed     (Phase 4+)
  • alerts.dispatched      (Phase 5+)

Helper usage
------------
  from app.models.outbox import OutboxEvent, write_outbox

  async with session.begin():
      incident = Incident(...)
      session.add(incident)
      write_outbox(
          session,
          topic="incidents.created",
          key=str(incident.id),
          payload={"incident_id": str(incident.id), ...},
      )
  # Debezium picks up the outbox row and publishes to Kafka.

Architecture reference: NeuralOps Technical Documentation — Section 5
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Dict

from sqlalchemy import Boolean, Column, DateTime, String, Text, event, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Session
from sqlalchemy.sql import func

from app.database.base import Base


class OutboxEvent(Base):
    """
    Transactional outbox row.

    Debezium watches this table via the PostgreSQL WAL and publishes each
    row to the Kafka topic specified in the `topic` column. After delivery,
    Debezium marks the row as published (sets `published = true`).

    Schema mirrors the outbox table in DB-1 exactly so that Debezium
    connector configuration is symmetric between both databases.
    """

    __tablename__ = "outbox"

    event_id: Column = Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        nullable=False,
        comment="Globally unique event identifier. Used for consumer deduplication.",
    )
    topic: Column = Column(
        String(256),
        nullable=False,
        comment="Kafka topic to which Debezium will publish this row.",
    )
    key: Column = Column(
        String(256),
        nullable=False,
        comment=(
            "Kafka message key (e.g. tenant_id or incident_id). "
            "Determines partition assignment for ordered delivery."
        ),
    )
    payload: Column = Column(
        JSONB,
        nullable=False,
        comment="Event payload. Must include event_id, event_type, source_version.",
    )
    created_at: Column = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    published: Column = Column(
        Boolean,
        nullable=False,
        default=False,
        comment=(
            "Set to true by Debezium after the row has been published to Kafka. "
            "Rows with published=false older than 1 hour indicate a CDC lag issue."
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<OutboxEvent event_id={self.event_id} "
            f"topic={self.topic} published={self.published}>"
        )


# ── Convenience helper ────────────────────────────────────────────────────────


def write_outbox(
    session: Session,
    topic: str,
    key: str,
    payload: Dict[str, Any],
) -> OutboxEvent:
    """
    Add an OutboxEvent to the current SQLAlchemy session.

    Must be called inside an active transaction so that the outbox write
    and the triggering business-logic write are committed atomically.

    Args:
        session  — the current async SQLAlchemy session.
        topic    — Kafka topic name (e.g. "incidents.created").
        key      — Kafka message key (e.g. tenant_id or incident_id).
        payload  — event payload dict. Should include:
                     event_id, event_type, version, occurred_at,
                     idempotency_key, source_version, and a payload sub-dict.

    Returns:
        The OutboxEvent instance added to the session (not yet persisted).
    """
    event_row = OutboxEvent(
        event_id=payload.get("event_id", uuid.uuid4()),
        topic=topic,
        key=key,
        payload=payload,
    )
    session.add(event_row)
    return event_row
