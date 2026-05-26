"""
app/core/config.py

Strongly-typed application configuration backed by pydantic-settings.
All values are read from environment variables (or a .env file).
No secret is hard-coded here.
"""

from __future__ import annotations

from functools import lru_cache
from typing import List, Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings resolved from environment / .env file."""

    model_config = SettingsConfigDict(
        env_file=".env.local",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Application ───────────────────────────────────────────────────────────
    APP_ENV: str = Field(default="development", description="Runtime environment")
    APP_NAME: str = Field(default="neuralops-fastapi")
    APP_VERSION: str = Field(default="1.0.0")
    DEBUG: bool = Field(default=False)

    # ── Database (DB-2 — FastAPI-owned PostgreSQL) ────────────────────────────
    DATABASE_URL: str = Field(
        ...,
        description=(
            "Async SQLAlchemy DSN. "
            "Must use the asyncpg driver, e.g. "
            "postgresql+asyncpg://user:pass@host:port/db"
        ),
    )

    # ── Redis ─────────────────────────────────────────────────────────────────
    REDIS_URL: str = Field(
        default="redis://localhost:6379/1",
        description="Redis connection URL used for caching and suspension flags.",
    )

    # ── JWT (RS256 — PUBLIC KEY ONLY) ─────────────────────────────────────────
    JWT_PUBLIC_KEY: str = Field(
        ...,
        description="PEM-encoded RSA public key for RS256 JWT verification.",
    )
    JWT_ALGORITHM: str = Field(default="RS256")

    # ── Tenant suspension Redis key pattern ───────────────────────────────────
    TENANT_SUSPENSION_REDIS_PREFIX: str = Field(default="tenant")
    TENANT_SUSPENSION_REDIS_SUFFIX: str = Field(default="suspended")

    # ── CORS ──────────────────────────────────────────────────────────────────
    CORS_ALLOWED_ORIGINS: List[str] = Field(
        default=["http://localhost:3000", "http://localhost:5173"],
    )

    # ── Kafka ─────────────────────────────────────────────────────────────────
    KAFKA_BOOTSTRAP_SERVERS: str = Field(
        default="kafka:9092",
        description="Comma-separated Kafka broker addresses.",
    )
    KAFKA_CONFIG_GROUP_ID: str = Field(
        default="fastapi-config-sync-group",
        description="Kafka consumer group ID for the config-sync consumer.",
    )

    # ── AWS S3 / Object Storage ───────────────────────────────────────────────
    AWS_ACCESS_KEY_ID: Optional[str] = Field(
        default=None,
        description="AWS access key ID for S3 operations.",
    )
    AWS_SECRET_ACCESS_KEY: Optional[str] = Field(
        default=None,
        description="AWS secret access key for S3 operations.",
    )
    AWS_REGION_NAME: str = Field(
        default="us-east-1",
        description="AWS region for S3 operations.",
    )
    AWS_S3_BUCKET_NAME: str = Field(
        default="neuralops-artifacts",
        description="S3 bucket where log context buffers and artifacts are stored.",
    )
    AWS_S3_SIGNED_URL_EXPIRY: int = Field(
        default=900,
        description="Pre-signed URL expiry in seconds (default: 15 minutes).",
    )
    AWS_S3_ENDPOINT_URL: Optional[str] = Field(
        default=None,
        description="Optional custom endpoint URL for S3 compatible APIs (e.g. MinIO).",
    )

    # ── Derived helpers ───────────────────────────────────────────────────────

    @field_validator("JWT_PUBLIC_KEY", mode="before")
    @classmethod
    def normalise_public_key(cls, v: str) -> str:
        """Replace literal \\n with real newlines so PEM blocks work correctly."""
        return v.replace("\\n", "\n")

    def tenant_suspension_key(self, tenant_id: str) -> str:
        """Return the Redis key for a tenant's suspension flag."""
        return (
            f"{self.TENANT_SUSPENSION_REDIS_PREFIX}"
            f":{tenant_id}"
            f":{self.TENANT_SUSPENSION_REDIS_SUFFIX}"
        )

    def tenant_config_cache_key(self, tenant_id: str) -> str:
        """
        Return the Redis key for a tenant's aggregated config cache.
        Pattern: tenant:{tenant_id}:config  (TTL: 1 hour)
        Invalidated by ConfigSyncConsumer after every snapshot upsert.
        """
        return f"tenant:{tenant_id}:config"

    @property
    def is_production(self) -> bool:
        return self.APP_ENV == "production"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached singleton Settings instance."""
    return Settings()