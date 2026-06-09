#!/bin/sh
set -e

echo "==> Waiting for PostgreSQL..."
until pg_isready -h "${DB_HOST:-django_db}" -p "${DB_PORT:-5432}" -U "${DB_USER:-neuralops}"; do
  echo "PostgreSQL not ready yet, waiting..."
  sleep 2
done
echo "==> PostgreSQL is ready."

echo "==> Starting Django Kafka consumer: consume_indexing_status"
exec python manage.py consume_indexing_status
