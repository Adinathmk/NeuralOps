#!/bin/sh
set -e

# Wait for PostgreSQL, Redis, Kafka (same as main entrypoint but skip migrations/static)
# ... same wait blocks as entrypoint.sh steps 1-3 ...

echo "==> Starting Celery worker..."
exec celery -A config worker \
    --loglevel="${CELERY_LOG_LEVEL:-info}" \
    --concurrency="${CELERY_CONCURRENCY:-4}" \
    --queues="${CELERY_QUEUES:-celery}" \
    --max-tasks-per-child=100