"""
app/database/base.py

SQLAlchemy declarative base shared by all DB-2 models.
Import Base here and inherit from it in every model module.
"""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Shared declarative base for all FastAPI-service (DB-2) models."""
    pass