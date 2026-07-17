#!/bin/sh
set -e

echo "Waiting for database to be ready..."
RETRIES=30
until python -c "
import asyncio, asyncpg, os, sys
async def check():
    try:
        conn = await asyncpg.connect(os.environ['DATABASE_URL'].replace('postgresql+asyncpg://', 'postgresql://'))
        await conn.close()
    except Exception as e:
        sys.exit(1)
asyncio.run(check())
" 2>/dev/null; do
  RETRIES=$((RETRIES - 1))
  if [ "$RETRIES" -le 0 ]; then
    echo "Database did not become ready in time. Exiting."
    exit 1
  fi
  echo "Database not ready yet, retrying in 2s... ($RETRIES attempts left)"
  sleep 2
done
echo "Database is ready."

echo "Running database migrations..."
alembic upgrade head

echo "Entrypoint complete, handing off to CMD..."
exec "$@"
