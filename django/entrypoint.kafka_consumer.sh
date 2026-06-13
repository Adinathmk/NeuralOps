#!/bin/sh
set -e

echo "==> Waiting for PostgreSQL..."
until pg_isready -h "${DB_HOST:-django_db}" -p "${DB_PORT:-5432}" -U "${DB_USER:-neuralops}"; do
  echo "PostgreSQL not ready yet, waiting..."
  sleep 2
done
echo "==> PostgreSQL is ready."

# Trap SIGTERM and SIGINT to forward to child processes
cleanup() {
  echo "==> Shutting down Kafka consumers..."
  kill "$PID_INDEXING" "$PID_INCIDENTS" 2>/dev/null || true
  wait "$PID_INDEXING" "$PID_INCIDENTS" 2>/dev/null || true
  echo "==> All consumers stopped."
  exit 0
}
trap cleanup TERM INT

echo "==> Starting Django Kafka consumer: consume_indexing_status"
python manage.py consume_indexing_status &
PID_INDEXING=$!

echo "==> Starting Django Kafka consumer: consume_incidents"
python manage.py consume_incidents &
PID_INCIDENTS=$!

echo "==> Both Kafka consumers running (PIDs: $PID_INDEXING, $PID_INCIDENTS)"

# Wait for background processes
wait "$PID_INDEXING"
wait "$PID_INCIDENTS"

echo "==> A consumer exited. Shutting down remaining..."
cleanup
