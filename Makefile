# ============================================================
# NeuralOps — Docker Helper Makefile
# ============================================================
# Prerequisites: Docker Desktop with Compose v2
#
# Quick start:
#   make setup   ← first-time setup (copy env, build, start)
#   make up      ← start all services
#   make down    ← stop all services
#   make logs    ← tail all logs
# ============================================================

.PHONY: help setup build up down restart logs shell migrate collectstatic \
        clean clean-volumes ps db-shell redis-cli superuser test \
        fastapi-shell fastapi-migrate fastapi-migrate-gen fastapi-migrate-down fastapi-test \
        fastapi-db-shell

# ── Default target ───────────────────────────────────────────
help:
	@echo ""
	@echo "  NeuralOps Docker Commands"
	@echo "  ─────────────────────────────────────────────────"
	@echo "  make setup          First-time: copy env + build + start"
	@echo "  make build          Rebuild Docker images"
	@echo "  make up             Start all services (detached)"
	@echo "  make down           Stop and remove containers"
	@echo "  make restart        Restart all services"
	@echo "  make logs           Tail logs (all services)"
	@echo "  make logs s=django   Tail logs for a specific service"	
	@echo "  make shell          Open Django container bash shell"
	@echo "  make migrate        Run Django DB migrations"
	@echo "  make collectstatic  Collect static files"
	@echo "  make superuser      Create Django superuser"
	@echo "  make test           Run Django test suite"
	@echo ""
	@echo "  FastAPI"
	@echo "  ─────────────────────────────────────────────────"
	@echo "  make fastapi-shell              Open FastAPI container bash shell"
	@echo "  make fastapi-migrate            Run Alembic migrations (upgrade head)"
	@echo "  make fastapi-migrate-gen m=msg  Generate new Alembic migration"
	@echo "  make fastapi-migrate-down       Roll back last Alembic migration"
	@echo "  make fastapi-test               Run FastAPI test suite"
	@echo "  make fastapi-db-shell           Open FastAPI PostgreSQL shell"
	@echo ""
	@echo "  Infrastructure"
	@echo "  ─────────────────────────────────────────────────"
	@echo "  make db-shell       Open Django PostgreSQL interactive shell"
	@echo "  make redis-cli      Open Redis CLI"
	@echo "  make ps             Show container status"
	@echo "  make clean          Remove containers + images"
	@echo "  make clean-volumes  Remove containers + images + volumes (DATA LOSS!)"
	@echo ""

# ── First-time setup ─────────────────────────────────────────
setup:
	@if [ ! -f .env.docker ]; then \
		cp .env.example .env.docker; \
		echo "  ✔ Created .env.docker from .env.example"; \
		echo "  ⚠  Edit .env.docker and set your real SECRET_KEY, DB_PASSWORD, etc."; \
	else \
		echo "  ✔ .env.docker already exists — skipping copy"; \
	fi
	$(MAKE) build
	$(MAKE) up

# ── Build ────────────────────────────────────────────────────
build:
	docker compose --env-file .env.docker build --no-cache

# ── Start ────────────────────────────────────────────────────
up:
	docker compose --env-file .env.docker up -d
	@echo ""
	@echo "  ✔ NeuralOps is running!"
	@echo "  API:     http://localhost/api/"
	@echo "  Swagger: http://localhost/api/schema/swagger-ui/"
	@echo "  Admin:   http://localhost/admin/"
	@echo ""

# ── Stop ─────────────────────────────────────────────────────
down:
	docker compose --env-file .env.docker down

restart:
	docker compose --env-file .env.docker restart

# ── Logs ─────────────────────────────────────────────────────
logs:
	docker compose --env-file .env.docker logs -f $(s)

# ── Shell access ─────────────────────────────────────────────
shell:
	docker compose --env-file .env.docker exec django bash

# ── Django management ────────────────────────────────────────
migrate:
	docker compose --env-file .env.docker exec django python manage.py migrate

collectstatic:
	docker compose --env-file .env.docker exec django python manage.py collectstatic --noinput

superuser:
	docker compose --env-file .env.docker exec django python manage.py createsuperuser

test:
	docker compose --env-file .env.docker exec django python manage.py test

# ── FastAPI management ───────────────────────────────────────
fastapi-shell:
	docker compose --env-file .env.docker exec fastapi bash

fastapi-migrate:
	docker compose --env-file .env.docker exec fastapi alembic upgrade head

fastapi-migrate-gen:
	@[ "$(m)" ] || ( echo "  ⚠  Usage: make fastapi-migrate-gen m=your_message" ; exit 1 )
	docker compose --env-file .env.docker exec fastapi alembic revision --autogenerate -m "$(m)"

fastapi-migrate-down:
	docker compose --env-file .env.docker exec fastapi alembic downgrade -1

fastapi-test:
	docker compose --env-file .env.docker exec fastapi python -m pytest

# ── Database ─────────────────────────────────────────────────
db-shell:
	docker compose --env-file .env.docker exec django_db psql -U ${DJANGO_DB_USER:-neuralops} -d ${DJANGO_DB_NAME:-neuralops_db}

fastapi-db-shell:
	docker compose --env-file .env.docker exec fastapi_db psql -U ${FASTAPI_DB_USER:-neuralops_fastapi} -d ${FASTAPI_DB_NAME:-neuralops_fastapi_db}


# ── Redis ─────────────────────────────────────────────────────
redis-cli:
	docker compose --env-file .env.docker exec redis redis-cli

# ── Status ───────────────────────────────────────────────────
ps:
	docker compose --env-file .env.docker ps

# ── Cleanup ──────────────────────────────────────────────────
clean:
	docker compose --env-file .env.docker down --rmi local

clean-volumes:
	@echo "  ⚠  WARNING: This will delete ALL data including the database!"
	@read -p "  Are you sure? [y/N] " confirm && [ "$$confirm" = "y" ]
	docker compose --env-file .env.docker down -v --rmi local


kafka-topics:
	docker compose --env-file .env.docker exec kafka kafka-topics \
		--bootstrap-server localhost:9092 --list

kafka-topic-create:
	docker compose --env-file .env.docker exec kafka kafka-topics \
		--bootstrap-server localhost:9092 \
		--create --if-not-exists \
		--topic $(topic) --partitions 1 --replication-factor 1

kafka-consume:
	docker compose --env-file .env.docker exec kafka kafka-console-consumer \
		--bootstrap-server localhost:9092 \
		--topic $(topic) --from-beginning

kafka-lag:
	docker compose --env-file .env.docker exec kafka kafka-consumer-groups \
		--bootstrap-server localhost:9092 --describe --all-groups

