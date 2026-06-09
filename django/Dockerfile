# ============================================================
# Stage 1: Builder — install Python dependencies
# ============================================================
FROM python:3.12-slim AS builder

WORKDIR /app

# Install system build deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps into a local prefix (not site-packages)
COPY requirements.txt .
RUN pip install --upgrade pip \
    && pip install --prefix=/install --no-cache-dir -r requirements.txt

# ============================================================
# Stage 2: Production — lean final image
# ============================================================
FROM python:3.12-slim AS production

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    DJANGO_SETTINGS_MODULE=config.settings

WORKDIR /app

# Runtime-only system libs:
#   libpq5          → psycopg PostgreSQL client runtime
#   postgresql-client → pg_isready for the healthcheck in entrypoint
#   curl            → Django healthcheck in docker compose
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    postgresql-client \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy project source (venv/sqlite/cache excluded via .dockerignore)
COPY . .

# Copy and set up entrypoint
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Create non-root user for security
RUN addgroup --system django \
    && adduser --system --ingroup django django \
    && mkdir -p /app/staticfiles /app/mediafiles /app/logs \
    && chown -R django:django /app /entrypoint.sh

USER django

EXPOSE 8000

ENTRYPOINT ["/entrypoint.sh"]


# ============================================================
# Stage 3: Celery Worker — same image, different CMD
# ============================================================
FROM production AS worker

# Switch back to root temporarily
USER root

# Override entrypoint for Celery
COPY entrypoint.worker.sh /entrypoint.worker.sh
RUN chmod +x /entrypoint.worker.sh

# Drop privileges again
USER django

ENTRYPOINT ["/entrypoint.worker.sh"]


# ============================================================
# Stage 4: Kafka Consumer — runs Django management command
# ============================================================
FROM production AS kafka-consumer

# Switch back to root temporarily
USER root

# Install consumer-specific entrypoint
COPY entrypoint.kafka_consumer.sh /entrypoint.kafka_consumer.sh
RUN chmod +x /entrypoint.kafka_consumer.sh

# Drop privileges again
USER django

ENTRYPOINT ["/entrypoint.kafka_consumer.sh"]
