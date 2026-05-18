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
        clean clean-volumes ps db-shell redis-cli superuser test

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
	@echo "  make logs s=web     Tail logs for a specific service"	
	@echo "  make shell          Open Django container bash shell"
	@echo "  make migrate        Run Django migrations"
	@echo "  make collectstatic  Collect static files"
	@echo "  make superuser      Create Django superuser"
	@echo "  make test           Run Django test suite"
	@echo "  make db-shell       Open PostgreSQL interactive shell"
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
	docker compose build --no-cache

# ── Start ────────────────────────────────────────────────────
up:
	docker compose up -d
	@echo ""
	@echo "  ✔ NeuralOps is running!"
	@echo "  API:     http://localhost/api/"
	@echo "  Swagger: http://localhost/api/schema/swagger-ui/"
	@echo "  Admin:   http://localhost/admin/"
	@echo ""

# ── Stop ─────────────────────────────────────────────────────
down:
	docker compose down

restart:
	docker compose restart

# ── Logs ─────────────────────────────────────────────────────
logs:
	docker compose logs -f $(s)

# ── Shell access ─────────────────────────────────────────────
shell:
	docker compose exec web bash

# ── Django management ────────────────────────────────────────
migrate:
	docker compose exec web python manage.py migrate

collectstatic:
	docker compose exec web python manage.py collectstatic --noinput

superuser:
	docker compose exec web python manage.py createsuperuser

test:
	docker compose exec web python manage.py test

# ── Database ─────────────────────────────────────────────────
db-shell:
	docker compose exec db psql -U neuralops -d neuralops_db

# ── Redis ─────────────────────────────────────────────────────
redis-cli:
	docker compose exec redis redis-cli

# ── Status ───────────────────────────────────────────────────
ps:
	docker compose ps

# ── Cleanup ──────────────────────────────────────────────────
clean:
	docker compose down --rmi local

clean-volumes:
	@echo "  ⚠  WARNING: This will delete ALL data including the database!"
	@read -p "  Are you sure? [y/N] " confirm && [ "$$confirm" = "y" ]
	docker compose down -v --rmi local
