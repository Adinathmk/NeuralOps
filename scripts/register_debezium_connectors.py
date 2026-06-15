import os
import json
import time
import urllib.request
import urllib.error

# ── Configuration ─────────────────────────────────────────────────────────────
DEBEZIUM_HOST = os.environ.get("DEBEZIUM_HOST", "localhost")
DEBEZIUM_PORT = os.environ.get("DEBEZIUM_PORT", "8083")
DEBEZIUM_BASE_URL = f"http://{DEBEZIUM_HOST}:{DEBEZIUM_PORT}"

def get_env_from_docker_file(key, default):
    """
    Read explicitly from .env.docker to prevent host shell variables 
    (e.g., DJANGO_DB_HOST=127.0.0.1) from leaking into the container configuration.
    Debezium runs inside Docker and MUST use the internal container names.
    """
    try:
        with open(".env.docker", "r") as f:
            for line in f:
                line = line.strip()
                if line.startswith(f"{key}="):
                    return line.split("=", 1)[1].strip("'\"")
    except Exception:
        pass
    return os.environ.get(key, default)

DJANGO_DB_HOST = get_env_from_docker_file("DJANGO_DB_HOST", "django_db")
DJANGO_DB_PORT = get_env_from_docker_file("DJANGO_DB_PORT", "5432")
DJANGO_DB_NAME = get_env_from_docker_file("DJANGO_DB_NAME", "neuralops_db")
DJANGO_DB_USER = get_env_from_docker_file("DJANGO_DB_USER", "neuralops")
DJANGO_DB_PASSWORD = get_env_from_docker_file("DJANGO_DB_PASSWORD", "neuralops_password")

FASTAPI_DB_HOST = get_env_from_docker_file("FASTAPI_DB_HOST", "fastapi_db")
FASTAPI_DB_PORT = get_env_from_docker_file("FASTAPI_DB_PORT", "5432")
FASTAPI_DB_NAME = get_env_from_docker_file("FASTAPI_DB_NAME", "neuralops_fastapi_db")
FASTAPI_DB_USER = get_env_from_docker_file("FASTAPI_DB_USER", "neuralops_fastapi")
FASTAPI_DB_PASSWORD = get_env_from_docker_file("FASTAPI_DB_PASSWORD", "fastapi_password")

def log_info(msg):
    print(f"[INFO]  {msg}")

def log_success(msg):
    print(f"[OK]    {msg}")

def log_error(msg):
    print(f"[ERROR] {msg}")

def wait_for_debezium():
    log_info(f"Waiting for Debezium Connect to be ready at {DEBEZIUM_BASE_URL} ...")
    max_attempts = 60
    for attempt in range(1, max_attempts + 1):
        try:
            urllib.request.urlopen(f"{DEBEZIUM_BASE_URL}/connectors", timeout=5)
            log_success("Debezium Connect is ready.")
            return
        except urllib.error.URLError:
            print(f"  Attempt {attempt}/{max_attempts} — retrying in 5s ...")
            time.sleep(5)
    log_error(f"Debezium Connect did not become ready after {max_attempts} attempts. Aborting.")
    exit(1)

def register_connector(connector_name, config_dict):
    log_info(f"Registering connector: {connector_name}")
    url = f"{DEBEZIUM_BASE_URL}/connectors/{connector_name}/config"
    data = json.dumps(config_dict).encode('utf-8')
    req = urllib.request.Request(url, data=data, method='PUT')
    req.add_header('Content-Type', 'application/json')
    try:
        with urllib.request.urlopen(req) as response:
            if response.status in [200, 201]:
                log_success(f"Connector '{connector_name}' registered (HTTP {response.status}).")
            else:
                log_error(f"Failed to register '{connector_name}'. HTTP status: {response.status}")
                exit(1)
    except urllib.error.HTTPError as e:
        log_error(f"HTTP Error {e.code} for '{connector_name}': {e.read().decode('utf-8')}")
        exit(1)
    except urllib.error.URLError as e:
        log_error(f"URL Error for '{connector_name}': {e.reason}")
        exit(1)

def check_connector_status(connector_name):
    log_info(f"Checking status of connector: {connector_name}")
    url = f"{DEBEZIUM_BASE_URL}/connectors/{connector_name}/status"
    try:
        with urllib.request.urlopen(url) as response:
            data = json.loads(response.read().decode('utf-8'))
            state = data.get('connector', {}).get('state', 'UNKNOWN')
            if state == 'RUNNING':
                log_success(f"Connector '{connector_name}' is RUNNING.")
            else:
                log_error(f"Connector '{connector_name}' state: {state} (expected RUNNING)")
    except Exception as e:
        log_error(f"Failed to check status for '{connector_name}': {e}")

if __name__ == "__main__":
    wait_for_debezium()

    django_config = {
        "connector.class": "io.debezium.connector.postgresql.PostgresConnector",
        "database.hostname": DJANGO_DB_HOST,
        "database.port": DJANGO_DB_PORT,
        "database.user": DJANGO_DB_USER,
        "database.password": DJANGO_DB_PASSWORD,
        "database.dbname": DJANGO_DB_NAME,
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
    register_connector("neuralops-django-outbox", django_config)

    fastapi_config = {
        "connector.class": "io.debezium.connector.postgresql.PostgresConnector",
        "database.hostname": FASTAPI_DB_HOST,
        "database.port": FASTAPI_DB_PORT,
        "database.user": FASTAPI_DB_USER,
        "database.password": FASTAPI_DB_PASSWORD,
        "database.dbname": FASTAPI_DB_NAME,
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
    register_connector("neuralops-fastapi-outbox", fastapi_config)

    print("\n[INFO] Waiting 10s for connectors to initialise ...")
    time.sleep(10)

    check_connector_status("neuralops-django-outbox")
    check_connector_status("neuralops-fastapi-outbox")

    print("\n[OK] Debezium CDC setup complete!")
