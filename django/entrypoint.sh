#!/bin/sh
set -e

# ============================================================
# NeuralOps Django Entrypoint
# 1. Wait for PostgreSQL (pg_isready)
# 2. Wait for Redis   (Python redis ping)
# 3. Run migrations
# 4. Collect static files
# 5. Start Gunicorn
# ============================================================

# ── 1. Wait for PostgreSQL ───────────────────────────────────
echo "==> [1/6] Waiting for PostgreSQL at ${DB_HOST:-db}:${DB_PORT:-5432}..."
until pg_isready -h "${DB_HOST:-db}" -p "${DB_PORT:-5432}" -U "${DB_USER:-neuralops}" -q; do
  echo "    PostgreSQL not ready yet — retrying in 2s..."
  sleep 2
done
echo "    PostgreSQL is ready!"

# ── 2. Wait for Redis ────────────────────────────────────────
echo "==> [2/6] Waiting for Redis at ${REDIS_HOST:-redis}:${REDIS_PORT:-6379}..."
until python -c "
import redis, sys
try:
    r = redis.Redis(host='${REDIS_HOST:-redis}', port=${REDIS_PORT:-6379}, socket_connect_timeout=2)
    r.ping()
    sys.exit(0)
except Exception:
    sys.exit(1)
" 2>/dev/null; do
  echo "    Redis not ready yet — retrying in 2s..."
  sleep 2
done
echo "    Redis is ready!"


# ── 3. Wait for Kafka ────────────────────────────────────────
echo "==> [3/6] Waiting for Kafka at ${KAFKA_BOOTSTRAP_SERVERS:-kafka:9092}..."
KAFKA_HOST=$(echo "${KAFKA_BOOTSTRAP_SERVERS:-kafka:9092}" | cut -d: -f1)
KAFKA_PORT=$(echo "${KAFKA_BOOTSTRAP_SERVERS:-kafka:9092}" | cut -d: -f2)
until python -c "
import socket, sys
try:
    s = socket.create_connection(('${KAFKA_HOST}', ${KAFKA_PORT}), timeout=2)
    s.close()
    sys.exit(0)
except Exception:
    sys.exit(1)
" 2>/dev/null; do
  echo "    Kafka not ready yet — retrying in 2s..."
  sleep 2
done
echo "    Kafka is ready!"


# ── 4. Run database migrations ───────────────────────────────
echo "==> [4/6] Running database migrations..."
python manage.py migrate --noinput

# ── 5. Collect static files ──────────────────────────────────
echo "==> [5/6] Collecting static files..."
python manage.py collectstatic --noinput --clear

# ── 6. Start Gunicorn ────────────────────────────────────────
echo "==> [6/6] Starting Gunicorn (workers=${GUNICORN_WORKERS:-3})..."
exec gunicorn config.wsgi:application \
    --bind 0.0.0.0:8000 \
    --reload \
    --workers "${GUNICORN_WORKERS:-3}" \
    --threads "${GUNICORN_THREADS:-2}" \
    --timeout "${GUNICORN_TIMEOUT:-120}" \
    --keep-alive 5 \
    --max-requests 1000 \
    --max-requests-jitter 100 \
    --log-level "${GUNICORN_LOG_LEVEL:-info}" \
    --access-logfile - \
    --error-logfile -
