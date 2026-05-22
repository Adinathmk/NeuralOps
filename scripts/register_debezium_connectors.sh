#!/usr/bin/env bash
# =============================================================================
# NeuralOps — Debezium CDC Connector Registration
# =============================================================================
# Idempotently registers one Debezium connector per database:
#   - neuralops-django-outbox  → watches DB-1 (django_db)  public.outbox
#   - neuralops-fastapi-outbox → watches DB-2 (fastapi_db) public.outbox
#
# Uses HTTP PUT which is an upsert (create or update), making it safe to
# re-run at any time without duplicate connector errors.
#
# Usage:
#   bash scripts/register_debezium_connectors.sh
#   DEBEZIUM_HOST=debezium bash scripts/register_debezium_connectors.sh
# =============================================================================

set -euo pipefail

# ── Configuration ─────────────────────────────────────────────────────────────
DEBEZIUM_HOST="${DEBEZIUM_HOST:-localhost}"
DEBEZIUM_PORT="${DEBEZIUM_PORT:-8083}"
DEBEZIUM_BASE_URL="http://${DEBEZIUM_HOST}:${DEBEZIUM_PORT}"

# DB-1 credentials (Django)
DJANGO_DB_HOST="${DJANGO_DB_HOST:-django_db}"
DJANGO_DB_PORT="${DJANGO_DB_PORT:-5432}"
DJANGO_DB_NAME="${DJANGO_DB_NAME:-neuralops_db}"
DJANGO_DB_USER="${DJANGO_DB_USER:-neuralops}"
DJANGO_DB_PASSWORD="${DJANGO_DB_PASSWORD:-neuralops_password}"

# DB-2 credentials (FastAPI)
FASTAPI_DB_HOST="${FASTAPI_DB_HOST:-fastapi_db}"
FASTAPI_DB_PORT="${FASTAPI_DB_PORT:-5432}"
FASTAPI_DB_NAME="${FASTAPI_DB_NAME:-neuralops_fastapi_db}"
FASTAPI_DB_USER="${FASTAPI_DB_USER:-neuralops_fastapi}"
FASTAPI_DB_PASSWORD="${FASTAPI_DB_PASSWORD:-fastapi_password}"

# ── Colours ───────────────────────────────────────────────────────────────────
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Colour

# ── Helper functions ──────────────────────────────────────────────────────────
log_info()    { echo -e "${YELLOW}[INFO]${NC}  $*"; }
log_success() { echo -e "${GREEN}[OK]${NC}    $*"; }
log_error()   { echo -e "${RED}[ERROR]${NC} $*" >&2; }

wait_for_debezium() {
  log_info "Waiting for Debezium Connect to be ready at ${DEBEZIUM_BASE_URL} ..."
  local max_attempts=30
  local attempt=0
  until curl -sf "${DEBEZIUM_BASE_URL}/connectors" > /dev/null 2>&1; do
    attempt=$((attempt + 1))
    if [[ $attempt -ge $max_attempts ]]; then
      log_error "Debezium Connect did not become ready after ${max_attempts} attempts. Aborting."
      exit 1
    fi
    echo "  Attempt ${attempt}/${max_attempts} — retrying in 5s ..."
    sleep 5
  done
  log_success "Debezium Connect is ready."
}

register_connector() {
  local connector_name="$1"
  local config_json="$2"

  log_info "Registering connector: ${connector_name}"

  local http_status
  http_status=$(curl -s -o /dev/null -w "%{http_code}" \
    -X PUT \
    -H "Content-Type: application/json" \
    --data "${config_json}" \
    "${DEBEZIUM_BASE_URL}/connectors/${connector_name}/config")

  if [[ "${http_status}" == "200" || "${http_status}" == "201" ]]; then
    log_success "Connector '${connector_name}' registered (HTTP ${http_status})."
  else
    log_error "Failed to register '${connector_name}'. HTTP status: ${http_status}"
    exit 1
  fi
}

check_connector_status() {
  local connector_name="$1"

  log_info "Checking status of connector: ${connector_name}"
  local state
  state=$(curl -sf "${DEBEZIUM_BASE_URL}/connectors/${connector_name}/status" \
    | python3 -c "import sys, json; d=json.load(sys.stdin); print(d['connector']['state'])" 2>/dev/null || echo "UNKNOWN")

  if [[ "${state}" == "RUNNING" ]]; then
    log_success "Connector '${connector_name}' is RUNNING."
  else
    log_error "Connector '${connector_name}' state: ${state} (expected RUNNING)"
  fi
}

# ── Wait for Debezium ─────────────────────────────────────────────────────────
wait_for_debezium

# ── Register DB-1 Connector (Django → public.outbox) ─────────────────────────
DJANGO_CONNECTOR_CONFIG=$(cat <<EOF
{
  "connector.class": "io.debezium.connector.postgresql.PostgresConnector",
  "database.hostname": "${DJANGO_DB_HOST}",
  "database.port": "${DJANGO_DB_PORT}",
  "database.user": "${DJANGO_DB_USER}",
  "database.password": "${DJANGO_DB_PASSWORD}",
  "database.dbname": "${DJANGO_DB_NAME}",
  "database.server.name": "neuralops_django",
  "topic.prefix": "neuralops_django",
  "plugin.name": "pgoutput",
  "publication.name": "debezium_django_publication",
  "slot.name": "debezium_django_outbox",
  "table.include.list": "public.outbox",
  "heartbeat.interval.ms": "5000",
  "transforms": "outbox",
  "transforms.outbox.type": "io.debezium.transforms.outbox.EventRouter",
  "transforms.outbox.table.field.event.id": "event_id",
  "transforms.outbox.table.field.event.key": "key",
  "transforms.outbox.table.field.event.type": "topic",
  "transforms.outbox.table.field.event.payload": "payload",
  "transforms.outbox.route.by.field": "topic",
  "transforms.outbox.route.topic.replacement": "${routedByValue}",
  "key.converter": "org.apache.kafka.connect.storage.StringConverter",
  "value.converter": "org.apache.kafka.connect.storage.StringConverter"
}
EOF
)

register_connector "neuralops-django-outbox" "${DJANGO_CONNECTOR_CONFIG}"

# ── Register DB-2 Connector (FastAPI → public.outbox) ────────────────────────
FASTAPI_CONNECTOR_CONFIG=$(cat <<EOF
{
  "connector.class": "io.debezium.connector.postgresql.PostgresConnector",
  "database.hostname": "${FASTAPI_DB_HOST}",
  "database.port": "${FASTAPI_DB_PORT}",
  "database.user": "${FASTAPI_DB_USER}",
  "database.password": "${FASTAPI_DB_PASSWORD}",
  "database.dbname": "${FASTAPI_DB_NAME}",
  "database.server.name": "neuralops_fastapi",
  "topic.prefix": "neuralops_fastapi",
  "plugin.name": "pgoutput",
  "publication.name": "debezium_fastapi_publication",
  "slot.name": "debezium_fastapi_outbox",
  "table.include.list": "public.outbox",
  "heartbeat.interval.ms": "5000",
  "transforms": "outbox",
  "transforms.outbox.type": "io.debezium.transforms.outbox.EventRouter",
  "transforms.outbox.table.field.event.id": "event_id",
  "transforms.outbox.table.field.event.key": "key",
  "transforms.outbox.table.field.event.type": "topic",
  "transforms.outbox.table.field.event.payload": "payload",
  "transforms.outbox.route.by.field": "topic",
  "transforms.outbox.route.topic.replacement": "${routedByValue}",
  "key.converter": "org.apache.kafka.connect.storage.StringConverter",
  "value.converter": "org.apache.kafka.connect.storage.StringConverter"
}
EOF
)

register_connector "neuralops-fastapi-outbox" "${FASTAPI_CONNECTOR_CONFIG}"

# ── Final Status Check ────────────────────────────────────────────────────────
echo ""
log_info "Waiting 10s for connectors to initialise ..."
sleep 10

check_connector_status "neuralops-django-outbox"
check_connector_status "neuralops-fastapi-outbox"

echo ""
log_info "All registered connectors:"
curl -sf "${DEBEZIUM_BASE_URL}/connectors" | python3 -m json.tool
echo ""
log_success "Debezium CDC setup complete!"
