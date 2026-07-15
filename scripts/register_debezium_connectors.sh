#!/usr/bin/env bash
# =============================================================================
# NeuralOps — Debezium CDC Connector Registration
# =============================================================================

set -euo pipefail

# ── Configuration ─────────────────────────────────────────────────────────────
DEBEZIUM_HOST="${DEBEZIUM_HOST:-localhost}"
DEBEZIUM_PORT="${DEBEZIUM_PORT:-8083}"
DEBEZIUM_BASE_URL="http://${DEBEZIUM_HOST}:${DEBEZIUM_PORT}"

# DB-1 (Django)
DJANGO_DB_HOST="${DJANGO_DB_HOST:-django_db}"
DJANGO_DB_PORT="${DJANGO_DB_PORT:-5432}"
DJANGO_DB_NAME="${DJANGO_DB_NAME:-neuralops_db}"
DJANGO_DB_USER="${DJANGO_DB_USER:-neuralops}"
DJANGO_DB_PASSWORD="${DJANGO_DB_PASSWORD:-neuralops_password}"

# DB-2 (FastAPI)
FASTAPI_DB_HOST="${FASTAPI_DB_HOST:-fastapi_db}"
FASTAPI_DB_PORT="${FASTAPI_DB_PORT:-5432}"
FASTAPI_DB_NAME="${FASTAPI_DB_NAME:-neuralops_fastapi_db}"
FASTAPI_DB_USER="${FASTAPI_DB_USER:-neuralops_fastapi}"
FASTAPI_DB_PASSWORD="${FASTAPI_DB_PASSWORD:-fastapi_password}"

# ── Colours ───────────────────────────────────────────────────────────────────
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_info()    { echo -e "${YELLOW}[INFO]${NC}  $*"; }
log_success() { echo -e "${GREEN}[OK]${NC}    $*"; }
log_error()   { echo -e "${RED}[ERROR]${NC} $*" >&2; }

wait_for_debezium() {
    log_info "Waiting for Debezium Connect at ${DEBEZIUM_BASE_URL}..."

    local attempts=0
    until curl -sf "${DEBEZIUM_BASE_URL}/connectors" >/dev/null 2>&1; do
        attempts=$((attempts+1))

        if [[ $attempts -ge 30 ]]; then
            log_error "Debezium did not become ready."
            exit 1
        fi

        sleep 5
    done

    log_success "Debezium Connect is ready."
}

register_connector() {

    local connector_name="$1"
    local config_json="$2"

    log_info "Registering ${connector_name}"

    local response
    local http_status
    local body

    response=$(
        curl -s \
            -w "\nHTTP_STATUS:%{http_code}" \
            -X PUT \
            -H "Content-Type: application/json" \
            --data "${config_json}" \
            "${DEBEZIUM_BASE_URL}/connectors/${connector_name}/config"
    )

    body=$(echo "$response" | sed '$d')
    http_status=$(echo "$response" | sed -n 's/HTTP_STATUS://p')

    if [[ "$http_status" == "200" || "$http_status" == "201" ]]; then
        log_success "${connector_name} registered."
    else
        log_error "Registration failed."
        echo
        echo "HTTP Status: ${http_status}"
        echo
        echo "Response:"
        echo "${body}"
        exit 1
    fi
}

check_connector_status() {

    local connector_name="$1"

    log_info "Checking ${connector_name}"

    local state

    state=$(
        curl -sf \
            "${DEBEZIUM_BASE_URL}/connectors/${connector_name}/status" |
            python3 -c '
import json,sys
print(json.load(sys.stdin)["connector"]["state"])
' 2>/dev/null || echo UNKNOWN
    )

    if [[ "$state" == "RUNNING" ]]; then
        log_success "${connector_name} is RUNNING."
    else
        log_error "${connector_name} state = ${state}"
    fi
}

wait_for_debezium

###############################################################################
# Django connector
###############################################################################

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
  "transforms.outbox.route.topic.replacement": "\${routedByValue}",
  "key.converter": "org.apache.kafka.connect.storage.StringConverter",
  "value.converter": "org.apache.kafka.connect.storage.StringConverter"
}
EOF
)

register_connector "neuralops-django-outbox" "${DJANGO_CONNECTOR_CONFIG}"

###############################################################################
# FastAPI connector
###############################################################################

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
  "transforms.outbox.route.topic.replacement": "\${routedByValue}",
  "key.converter": "org.apache.kafka.connect.storage.StringConverter",
  "value.converter": "org.apache.kafka.connect.storage.StringConverter"
}
EOF
)

register_connector "neuralops-fastapi-outbox" "${FASTAPI_CONNECTOR_CONFIG}"

echo
log_info "Waiting 10 seconds..."
sleep 10

check_connector_status "neuralops-django-outbox"
check_connector_status "neuralops-fastapi-outbox"

echo
log_info "Registered connectors:"
curl -sf "${DEBEZIUM_BASE_URL}/connectors" | python3 -m json.tool

echo
log_success "Debezium CDC setup complete."