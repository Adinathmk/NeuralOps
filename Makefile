# ============================================================
# NeuralOps — Docker Helper Makefile
# ============================================================
# Services & Ownership:
#   django               — Django REST API (DB-1 owner)
#   fastapi              — FastAPI service  (DB-2 owner)
#   django_db            — PostgreSQL DB-1  (Django-owned)
#   fastapi_db           — PostgreSQL DB-2  (FastAPI-owned)
#   redis                — Shared Redis (Celery broker + cache)
#   celery-worker        — Django Celery worker
#   celery-beat          — Django Celery beat scheduler
#   celery-worker-fastapi— FastAPI Celery worker
#   kafka                — Kafka broker
#   debezium             — Debezium CDC connector
#   kong                 — Kong API Gateway
#   minio                — Object storage (S3-compatible)
#   elasticsearch        — Search engine
# ============================================================

# Tell docker compose CLI to always read .env.docker even when called directly
# (i.e. without make). This eliminates "variable is not set" warnings.
export COMPOSE_ENV_FILES := .env.docker

DC = docker compose --env-file .env.docker

.PHONY: help \
	setup build up down restart logs ps test lint format \
	\
	django-shell django-db-shell django-migrate django-makemigrations django-collectstatic \
	django-superuser django-test django-logs django-lint django-format \
	\
	fastapi-shell fastapi-db-shell fastapi-migrate fastapi-makemigrations fastapi-migrate-down \
	fastapi-test fastapi-logs fastapi-lint fastapi-format \
	\
	redis-shell redis-flush \
	\
	worker-restart django-worker-restart fastapi-worker-restart \
	\
	kafka-topics kafka-topic-create kafka-consume kafka-lag \
	\
	kong-validate kong-reload kong-shell \
	debezium-register debezium-status \
	\
	clean clean-all

# ── Help / Menu ──────────────────────────────────────────────
help:
	@echo ""
	@echo "  NeuralOps — Makefile Commands"
	@echo "  ═══════════════════════════════════════════════════════"
	@echo ""
	@echo "  CORE PROJECTS"
	@echo "  ───────────────────────────────────────────────────────"
	@echo "  make setup                  First-time environment copy + build + migrations"
	@echo "  make build                  Rebuild all Docker images (no cache)"
	@echo "  make up                     Start all services in detached mode"
	@echo "  make down                   Stop and remove all containers"
	@echo "  make restart                Restart all containers"
	@echo "  make logs                   Tail logs for all services"
	@echo "  make logs s=django          Tail logs for a specific service container"
	@echo "  make ps                     Show running container status"
	@echo "  make test                   Run BOTH Django and FastAPI test suites"
	@echo "  make lint                   Run static analysis (nox lint) on both services"
	@echo "  make format                 Run auto-formatter (nox format) on Django"
	@echo ""
	@echo "  DJANGO SERVICE (DB-1: django_db)"
	@echo "  ───────────────────────────────────────────────────────"
	@echo "  make django-shell           Open bash inside Django container"
	@echo "  make django-db-shell        Open interactive psql console for DB-1"
	@echo "  make django-migrate         Apply Django database migrations"
	@echo "  make django-makemigrations  Generate new Django database migrations"
	@echo "  make django-collectstatic   Collect static files"
	@echo "  make django-superuser       Create a Django admin superuser"
	@echo "  make django-test            Run Django test suite (pytest)"
	@echo "  make django-logs            Tail logs for Django container"
	@echo "  make django-lint            Lint Django code via nox"
	@echo "  make django-format          Auto-format Django code via nox"
	@echo ""
	@echo "  FASTAPI SERVICE (DB-2: fastapi_db)"
	@echo "  ───────────────────────────────────────────────────────"
	@echo "  make fastapi-shell          Open bash inside FastAPI container"
	@echo "  make fastapi-db-shell       Open interactive psql console for DB-2"
	@echo "  make fastapi-migrate        Apply Alembic migrations (head) to DB-2"
	@echo "  make fastapi-makemigrations m=  Generate Alembic migration revision"
	@echo "  make fastapi-migrate-down   Roll back last Alembic migration on DB-2"
	@echo "  make fastapi-test           Run FastAPI test suite (pytest)"
	@echo "  make fastapi-logs           Tail logs for FastAPI container"
	@echo "  make fastapi-lint           Lint FastAPI code via nox"
	@echo ""
	@echo "  REDIS UTILS"
	@echo "  ───────────────────────────────────────────────────────"
	@echo "  make redis-shell            Open interactive Redis CLI"
	@echo "  make redis-flush            Flush all Redis keys (⚠ clears cache/sessions)"
	@echo ""
	@echo "  CELERY WORKERS"
	@echo "  ───────────────────────────────────────────────────────"
	@echo "  make worker-restart         Restart all Celery workers"
	@echo "  make django-worker-restart  Restart Django Celery worker + beat scheduler"
	@echo "  make fastapi-worker-restart Restart FastAPI Celery worker"
	@echo ""
	@echo "  KAFKA SYSTEMS"
	@echo "  ───────────────────────────────────────────────────────"
	@echo "  make kafka-topics           List all current Kafka topics"
	@echo "  make kafka-topic-create topic=name  Create a new Kafka topic"
	@echo "  make kafka-consume topic=name        Consume messages from the beginning"
	@echo "  make kafka-lag              Show consumer group lag stats"
	@echo ""
	@echo "  KONG API GATEWAY"
	@echo "  ───────────────────────────────────────────────────────"
	@echo "  make kong-validate          Validate declarative kong.yml configuration"
	@echo "  make kong-reload            Hot-reload Kong gateway config"
	@echo "  make kong-shell             Open bash inside Kong container"
	@echo ""
	@echo "  DEBEZIUM CDC CONNECTORS"
	@echo "  ───────────────────────────────────────────────────────"
	@echo "  make debezium-register      Register CDC connectors for DB-1 + DB-2"
	@echo "  make debezium-status        Show detailed connector status"
	@echo ""
	@echo "  CLEANUP TOOLS"
	@echo "  ───────────────────────────────────────────────────────"
	@echo "  make clean                  Remove containers and local images"
	@echo "  make clean-all              Remove containers, local images, AND volumes (⚠ DATA LOSS)"
	@echo ""

# ── Core Operations ──────────────────────────────────────────
setup:
	@if [ ! -f .env.docker ]; then \
		cp .env.example .env.docker; \
		echo "  ✔ Created .env.docker file from .env.example"; \
	fi
	$(MAKE) build
	$(MAKE) up
	@echo "Waiting for databases to be ready..."
	@sleep 5
	$(MAKE) django-migrate
	$(MAKE) fastapi-migrate
	@echo "  ✔ Setup complete! Services are fully provisioned and migrated."

build:
	$(DC) build --no-cache

up:
	$(DC) up -d --remove-orphans
	@echo "  Verifying Debezium CDC connectors..."
	@$(MAKE) debezium-register
	@echo ""
	@echo "  ✔ NeuralOps is running!"
	@echo "  Admin:      http://localhost:8000/admin/"
	@echo "  FastAPI Swagger:    http://127.0.0.1:8001/docs#/"
	@echo "  Django Swagger:    http://localhost:8000/api/v1/schema/swagger-ui/"
	@echo "  Webhook:    Visit http://localhost:4040 to see your Ngrok URL"
	@echo ""

down:
	$(DC) down

restart:
	$(DC) restart

logs:
	$(DC) logs -f $(s)

ps:
	$(DC) ps

test:
	@echo "==> Running Django test suite..."
	$(DC) run --rm -e SKIP_KAFKA_WAIT=true django pytest -v
	@echo "==> Running FastAPI test suite..."
	$(DC) run --rm fastapi pytest tests/ -v --tb=short

lint:
	@echo "==> Linting Django codebase..."
	$(DC) exec django nox -s lint
	@echo "==> Linting FastAPI codebase..."
	$(DC) exec fastapi nox -s lint

format:
	@echo "==> Formatting Django codebase..."
	$(DC) exec django nox -s format
	@echo "==> Formatting FastAPI codebase..."
	$(DC) exec fastapi nox -s format

# ── Django Service (DB-1: django_db) ──────────────────────────
django-shell:
	$(DC) exec django bash

django-db-shell:
	$(DC) exec django_db psql -U $${DJANGO_DB_USER:-neuralops} -d $${DJANGO_DB_NAME:-neuralops_db}

django-migrate:
	$(DC) exec django python manage.py migrate

django-makemigrations:
	$(DC) exec django python manage.py makemigrations

django-collectstatic:
	$(DC) exec django python manage.py collectstatic --noinput

django-superuser:
	$(DC) exec django python manage.py createsuperuser

django-test:
	$(DC) run --rm -e SKIP_KAFKA_WAIT=true django pytest -v

django-logs:
	$(DC) logs -f django

django-lint:
	$(DC) exec django nox -s lint

django-format:
	$(DC) exec django nox -s format

# ── FastAPI Service (DB-2: fastapi_db) ────────────────────────
fastapi-shell:
	$(DC) exec fastapi bash

fastapi-db-shell:
	$(DC) exec fastapi_db psql -U $${FASTAPI_DB_USER:-neuralops_fastapi} -d $${FASTAPI_DB_NAME:-neuralops_fastapi_db}

fastapi-migrate:
	$(DC) exec fastapi alembic upgrade head

fastapi-makemigrations:
	@[ "$(m)" ] || ( echo "  ⚠  Usage: make fastapi-makemigrations m=your_message" ; exit 1 )
	$(DC) exec fastapi alembic revision --autogenerate -m "$(m)"

fastapi-migrate-down:
	$(DC) exec fastapi alembic downgrade -1

fastapi-test:
	$(DC) run --rm fastapi pytest tests/ -v --tb=short

fastapi-logs:
	$(DC) logs -f fastapi

fastapi-lint:
	$(DC) exec fastapi nox -s lint

fastapi-format:
	$(DC) exec fastapi nox -s format

# ── Redis Utils ───────────────────────────────────────────────
redis-shell:
	$(DC) exec redis redis-cli

redis-flush:
	@echo "  ⚠  Flushing ALL Redis keys ..."
	$(DC) exec redis redis-cli FLUSHALL
	@echo "  ✔ Redis flushed."

# ── Celery Workers ────────────────────────────────────────────
worker-restart:
	$(DC) restart celery-worker celery-beat celery-worker-fastapi

django-worker-restart:
	$(DC) restart celery-worker celery-beat

fastapi-worker-restart:
	$(DC) restart celery-worker-fastapi

# ── Kafka Systems ─────────────────────────────────────────────
kafka-topics:
	$(DC) exec kafka kafka-topics --bootstrap-server localhost:9092 --list

kafka-topic-create:
	@[ "$(topic)" ] || ( echo "  ⚠  Usage: make kafka-topic-create topic=name" ; exit 1 )
	$(DC) exec kafka kafka-topics \
		--bootstrap-server localhost:9092 \
		--create --if-not-exists \
		--topic $(topic) --partitions 1 --replication-factor 1

kafka-consume:
	@[ "$(topic)" ] || ( echo "  ⚠  Usage: make kafka-consume topic=name" ; exit 1 )
	$(DC) exec kafka kafka-console-consumer \
		--bootstrap-server localhost:9092 \
		--topic $(topic) --from-beginning

kafka-lag:
	$(DC) exec kafka kafka-consumer-groups --bootstrap-server localhost:9092 --describe --all-groups

# ── Kong API Gateway ──────────────────────────────────────────
kong-validate:
	@echo "  Validating kong/kong.yml ..."
	$(DC) exec kong kong config parse /usr/local/kong/declarative/kong.yml

kong-reload:
	@echo "  Hot-reloading Kong config ..."
	$(DC) exec kong kong reload

kong-shell:
	$(DC) exec kong bash

# ── Debezium CDC Connectors ───────────────────────────────────
debezium-register:
	@echo "  Registering Debezium connectors for DB-1 and DB-2 ..."
	python scripts/register_debezium_connectors.py

debezium-status:
	@echo "  All connectors:"
	@curl -sf http://localhost:8083/connectors | python3 -m json.tool
	@echo ""
	@echo "  Django (DB-1 outbox) status:"
	@curl -sf http://localhost:8083/connectors/neuralops-django-outbox/status | python3 -m json.tool
	@echo ""
	@echo "  FastAPI (DB-2 outbox) status:"
	@curl -sf http://localhost:8083/connectors/neuralops-fastapi-outbox/status | python3 -m json.tool

# ── Cleanup Tools ─────────────────────────────────────────────
clean:
	$(DC) down --rmi local

clean-all:
	@echo "  ⚠  WARNING: This will permanently delete ALL data (DB-1, DB-2, Redis, Kafka)!"
	@read -p "  Are you sure? [y/N] " confirm && [ "$$confirm" = "y" ]
	$(DC) down -v --rmi local
