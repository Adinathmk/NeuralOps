"""
fastapi/app/worker/celery_app.py

Celery application instance for NeuralOps FastAPI background workers.

Isolation contract
------------------
Django's Celery workers use Redis database 0 (redis://redis:6379/0).
FastAPI's Celery workers use Redis database 2 (redis://redis:6379/2).

This hard separation ensures that:
  - Queue depth metrics per-service are accurate.
  - A stalled FastAPI worker queue cannot consume Django task messages
    or vice-versa.
  - KEDA ScaledObjects can target the correct Redis key prefix per service.

Task discovery
--------------
Celery auto-discovers tasks from the `app.worker.tasks` package.
Task modules placed under that package are imported automatically at
worker startup — no manual registration is required.

Usage
-----
Start the worker in development (hot-reload via volume mount):

    celery -A app.worker.celery_app worker --loglevel=info

In production the docker-compose `celery-worker-fastapi` service runs
this exact command inside the FastAPI container image.

Architecture reference: NeuralOps Technical Documentation — Section 6
(Infrastructure & Messaging — Celery Task Queues), Section 17
(Code Indexing — Background).
"""

from __future__ import annotations

from celery import Celery

from app.core.config import get_settings

# ── Settings ──────────────────────────────────────────────────────────────────
_settings = get_settings()

# ── Celery application instance ───────────────────────────────────────────────
# The main_module name ("neuralops_tasks") is used internally by Celery as a
# namespace prefix for task names.  It must be unique across all Celery apps
# in the same Redis broker to avoid routing conflicts with Django tasks.
celery_app = Celery("neuralops_tasks")

# ── Broker & result backend ───────────────────────────────────────────────────
# Both point to Redis database 2 — completely isolated from Django (db 0).
celery_app.conf.broker_url = _settings.CELERY_BROKER_URL
celery_app.conf.result_backend = _settings.CELERY_RESULT_BACKEND

# ── Serialisation ─────────────────────────────────────────────────────────────
# JSON is used for all tasks so that payloads are human-readable in Redis and
# interoperable with any future worker language (Go, Node).
celery_app.conf.task_serializer = "json"
celery_app.conf.result_serializer = "json"
celery_app.conf.accept_content = ["json"]

# ── Connection resilience ─────────────────────────────────────────────────────
# Retry connecting to the broker on worker startup instead of raising
# immediately — important in docker-compose where Redis may not be ready
# before the worker container starts.
celery_app.conf.broker_connection_retry_on_startup = True

# ── Time limits ───────────────────────────────────────────────────────────────
# Hard kill at 5 minutes (matches Django worker config from Section 6).
# SoftTimeLimitExceeded is raised at 4 minutes so tasks can clean up.
celery_app.conf.task_time_limit = 300  # seconds — hard kill
celery_app.conf.task_soft_time_limit = 240  # seconds — soft signal

# ── Retry policy defaults ─────────────────────────────────────────────────────
# Individual task classes may override max_retries.  The default backoff
# base of 5 s, doubling on each attempt up to 300 s ceiling, matches the
# architecture specification in Section 6.
celery_app.conf.task_max_retries = 5
celery_app.conf.task_default_retry_delay = 5  # seconds

# ── Dead-letter / poison-message handling ─────────────────────────────────────
# If a worker process is killed mid-task the message is re-queued rather
# than silently acknowledged, preserving at-least-once delivery semantics.
celery_app.conf.task_reject_on_worker_lost = True
celery_app.conf.task_acks_late = True

celery_app.autodiscover_tasks(["app.worker"], force=True)

import app.worker.tasks.embed_playbook

# ── Explicit imports for task registration ────────────────────────────────────
# Since the task module is named index_code.py rather than tasks.py,
# we explicitly import it here to trigger registration with the Celery application.
import app.worker.tasks.index_code
import app.worker.tasks.parse_log
import app.worker.tasks.run_agent
import app.worker.tasks.wipe_data
