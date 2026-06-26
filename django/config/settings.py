# CORRECTED: config/settings.py
# Fixed: AUTH_USER_MODEL + Middleware Order + Multi-Tenant Architecture

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR / "apps"))

# Load environment variables
# In Docker, env vars come from docker-compose env_file — .env.local is only for local dev.
# load_dotenv does NOT override already-set env vars, so Docker env takes priority.
load_dotenv(BASE_DIR / ".env.local")

# ============================================================================
# DJANGO CORE SETTINGS
# ============================================================================

SECRET_KEY = os.getenv("SECRET_KEY", "django-insecure-dev-key")
DEBUG = os.getenv("DEBUG", "False") == "True"
ALLOWED_HOSTS = os.getenv("ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")

# ============================================================================
# INSTALLED APPS (Order matters!)
# ============================================================================
NUM_PROXIES = 1
USE_X_FORWARDED_HOST = True

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    #  Third-party apps
    "rest_framework",
    "corsheaders",
    "drf_spectacular",
    "django_celery_beat",
    # Our apps
    "tenants",
    "users",
    "outbox",
    "superadmin",
    "analytics",
    "alerts",
    "playbooks",
    "integrations",
    "channels",
    "websockets",
    "push",
    "billing",
    "collaboration",
]

# ============================================================================
# MIDDLEWARE (CORRECTED ORDER FOR MULTI-TENANCY)
# ============================================================================

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",  # ← ADDED: Serve static files behind Kong
    "django.contrib.sessions.middleware.SessionMiddleware",  # ← FIXED: Position 2
    "corsheaders.middleware.CorsMiddleware",  # ← FIXED: After session
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",  # ← FIXED: After session
    "django.contrib.messages.middleware.MessageMiddleware",  # ← FIXED: After auth
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "core.middleware.RequestIDMiddleware",
    "users.middleware.TenantMiddleware",
    "core.middleware.ExceptionHandlingMiddleware",
]

# ============================================================================
# URL CONFIGURATION
# ============================================================================

ROOT_URLCONF = "config.urls"


# Redis Configuration
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": REDIS_URL,
    }
}

# ============================================================================
# TEMPLATES
# ============================================================================

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

# ============================================================================
# WSGI & ASGI
# ============================================================================

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels_redis.core.RedisChannelLayer",
        "CONFIG": {
            "hosts": [REDIS_URL],
            "capacity": 1500,
            "expiry": 10,
        },
    }
}

# ============================================================================
# DATABASE CONFIGURATION
# ============================================================================

import dj_database_url

DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/neuralops"
)

DATABASE_REPLICA_URL = os.getenv("DATABASE_REPLICA_URL", DATABASE_URL)

DATABASES = {
    "default": dj_database_url.config(
        default=DATABASE_URL,
        conn_max_age=600,
        conn_health_checks=True,
    ),
    "replica": dj_database_url.config(
        default=DATABASE_REPLICA_URL,
        conn_max_age=600,
        conn_health_checks=True,
    ),
}

# During tests, replica mirrors primary
DATABASES["replica"]["TEST"] = {"MIRROR": "default"}


DATABASE_ROUTERS = ["config.db_router.AnalyticsReadRouter"]

# ============================================================================
# CUSTOM AUTH USER MODEL (CRITICAL FOR MULTI-TENANCY)
# ============================================================================

AUTH_USER_MODEL = "users.User"  # ← FIXED: Point to custom multi-tenant User

# ============================================================================
# AUTHENTICATION
# ============================================================================

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {
            "min_length": 8,
        },
    },
]

# ============================================================================
# REST FRAMEWORK CONFIGURATION
# ============================================================================

REST_FRAMEWORK = {
    "EXCEPTION_HANDLER": "core.exception_handler.custom_exception_handler",
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "users.authentication.GatewayAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 20,
    "DEFAULT_THROTTLE_CLASSES": [
        "core.throttling.TenantRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "tenant": "60/minute",
        "billing": "5/hour",
    },
}

# ============================================================================
# SPECTACULAR CONFIGURATION
# ============================================================================

SPECTACULAR_SETTINGS = {
    "TITLE": "NeuralOps API",
    "DESCRIPTION": "API documentation for NeuralOps Backend",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
}

# ============================================================================
# CORS CONFIGURATION
# ============================================================================

CORS_ALLOWED_ORIGINS = os.getenv("CORS_ALLOWED_ORIGINS", "http://localhost:3000").split(
    ","
)

# ============================================================================
# JWT CONFIGURATION
# ============================================================================

# RS256 — Django holds the private key for signing only
JWT_PRIVATE_KEY = os.getenv("JWT_PRIVATE_KEY", "").replace("\\n", "\n")
# Public key is distributed to FastAPI and the gateway
JWT_PUBLIC_KEY = os.getenv("JWT_PUBLIC_KEY", "").replace("\\n", "\n")
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "RS256")
JWT_ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("JWT_ACCESS_TOKEN_EXPIRE_MINUTES", 15))
JWT_REFRESH_TOKEN_EXPIRE_DAYS = int(os.getenv("JWT_REFRESH_TOKEN_EXPIRE_DAYS", 7))

# ============================================================================
# INTERNATIONALIZATION
# ============================================================================

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

# ============================================================================
# STATIC FILES
# ============================================================================

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

# Enable WhiteNoise compression and caching
STATICFILES_STORAGE = "whitenoise.storage.CompressedManifestStaticFilesStorage"

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "mediafiles"

# ============================================================================
# DEFAULT PRIMARY KEY
# ============================================================================

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ============================================================================
# LOGGING CONFIGURATION
# ============================================================================

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "json": {
            "()": "pythonjsonlogger.jsonlogger.JsonFormatter",
            "format": "%(asctime)s %(levelname)s %(name)s %(message)s",
        },
        "verbose": {
            # Fallback for local dev without json logger installed
            "format": "[{levelname}] {asctime} {name} {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "json" if not DEBUG else "verbose",
        },
        "file": {
            "class": "logging.handlers.RotatingFileHandler",
            "filename": BASE_DIR / "logs/django.log",
            "maxBytes": 1024 * 1024 * 5,
            "backupCount": 5,
            "formatter": "json" if not DEBUG else "verbose",
        },
    },
    "root": {
        "handlers": ["console", "file"],
        "level": "INFO",
    },
    "loggers": {
        "django": {
            "handlers": ["console", "file"],
            "level": os.getenv("DJANGO_LOG_LEVEL", "INFO"),
            "propagate": False,
        },
        "django.request": {
            "handlers": ["console", "file"],
            "level": "WARNING",
            "propagate": False,
        },
    },
}


# Email Configuration (AWS SES)
EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
EMAIL_HOST = os.getenv(
    "EMAIL_HOST", "email-smtp.us-east-1.amazonaws.com"
)  # ← Change region if needed
EMAIL_PORT = int(os.getenv("EMAIL_PORT", 587))
EMAIL_USE_TLS = os.getenv("EMAIL_USE_TLS", "True") == "True"
EMAIL_HOST_USER = os.getenv("EMAIL_HOST_USER")  # ← AWS SES SMTP username
EMAIL_HOST_PASSWORD = os.getenv("EMAIL_HOST_PASSWORD")  # ← AWS SES SMTP password
DEFAULT_FROM_EMAIL = os.getenv("DEFAULT_FROM_EMAIL", "noreply@neuralops.com")

# Frontend URL (for email links)
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3000")


# OAuth Configuration
GOOGLE_OAUTH_CLIENT_ID = os.getenv("GOOGLE_OAUTH_CLIENT_ID")
GOOGLE_OAUTH_CLIENT_SECRET = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET")
GOOGLE_OAUTH_REDIRECT_URI = os.getenv(
    "GOOGLE_OAUTH_REDIRECT_URI", "http://localhost:3000/auth/google/callback"
)

GITHUB_OAUTH_CLIENT_ID = os.getenv("GITHUB_OAUTH_CLIENT_ID")
GITHUB_OAUTH_CLIENT_SECRET = os.getenv("GITHUB_OAUTH_CLIENT_SECRET")
GITHUB_OAUTH_REDIRECT_URI = os.getenv(
    "GITHUB_OAUTH_REDIRECT_URI", "http://localhost:3000/auth/github/callback"
)

# Frontend OAuth callback URLs
FRONTEND_OAUTH_SUCCESS_URL = os.getenv(
    "FRONTEND_OAUTH_SUCCESS_URL", "http://localhost:3000/dashboard"
)
FRONTEND_OAUTH_ERROR_URL = os.getenv(
    "FRONTEND_OAUTH_ERROR_URL", "http://localhost:3000/login"
)


# Kafka
KAFKA_BOOTSTRAP_SERVERS = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
SCHEMA_REGISTRY_URL = os.getenv("SCHEMA_REGISTRY_URL", "http://localhost:8081")

# Django Kafka consumer — indexing status sync (Section 3: "Kafka consumption")
# Listens to events published by FastAPI's index_code Celery task via Debezium.
KAFKA_INDEXING_STATUS_TOPIC = os.getenv(
    "KAFKA_INDEXING_STATUS_TOPIC", "indexing.status"
)
KAFKA_INDEXING_STATUS_GROUP_ID = os.getenv(
    "KAFKA_INDEXING_STATUS_GROUP_ID", "django-indexing-status-consumer"
)


# Elasticsearch
ELASTICSEARCH_URL = os.getenv("ELASTICSEARCH_URL", "http://localhost:9200")


# ============================================================================
# CELERY CONFIGURATION
# ============================================================================

CELERY_BROKER_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
CELERY_RESULT_BACKEND = os.getenv("REDIS_URL", "redis://localhost:6379/0")
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = "UTC"
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_TIME_LIMIT = 300  # hard kill at 5 min
CELERY_TASK_SOFT_TIME_LIMIT = 240  # raises SoftTimeLimitExceeded at 4 min

# Retry policy (matches doc: base 5s, doubles, ceiling 300s, max 5 retries)
CELERY_TASK_MAX_RETRIES = 5
CELERY_TASK_DEFAULT_RETRY_DELAY = 5

# Dead letter queue — tasks exhausting retries write here
CELERY_TASK_REJECT_ON_WORKER_LOST = True

# Beat schedule (periodic tasks)
from celery.schedules import crontab

CELERY_BEAT_SCHEDULE = {
    "cleanup-expired-invitations-hourly": {
        "task": "users.tasks.cleanup_expired_invitations_task",
        "schedule": crontab(minute=0, hour="*"),  # hourly
        "kwargs": {"is_superadmin": True},
    },
    "cleanup-expired-tokens-hourly": {
        "task": "users.tasks.cleanup_expired_tokens_task",
        "schedule": crontab(minute=30, hour="*"),  # hourly at minute 30
        "kwargs": {"is_superadmin": True},
    },
    "sync-razorpay-subscription-status-daily": {
        "task": "billing.tasks.sync_subscription_statuses",
        "schedule": crontab(minute=0, hour=2),
    },
}


# ============================================================================
# AWS S3 / OBJECT STORAGE
# ============================================================================

AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY")
AWS_REGION_NAME = os.getenv("AWS_REGION_NAME", "us-east-1")
AWS_S3_BUCKET_NAME = os.getenv("AWS_S3_BUCKET_NAME", "neuralops-artifacts")

# Pre-signed URL expiry (15 minutes — matches doc)
AWS_S3_SIGNED_URL_EXPIRY = int(os.getenv("AWS_S3_SIGNED_URL_EXPIRY", 900))


# ============================================================================
# ELASTICSEARCH (read-only for Django analytics)
# ============================================================================

ELASTICSEARCH_URL = os.getenv("ELASTICSEARCH_URL", "http://localhost:9200")
ELASTICSEARCH_INDEX_LOGS = os.getenv("ELASTICSEARCH_INDEX_LOGS", "logs")

FERNET_ENCRYPTION_KEY = os.getenv("FERNET_ENCRYPTION_KEY")

# NOTE: Required for the integrations app.
# Generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# Add the key to .env.docker as: FERNET_ENCRYPTION_KEY=<generated-key>

# ============================================================================
# AWS DYNAMODB
# ============================================================================
DYNAMODB_REGION = os.getenv("DYNAMODB_REGION", "ap-south-1")
DYNAMODB_ACCESS_KEY_ID = os.getenv("DYNAMODB_ACCESS_KEY_ID")
DYNAMODB_SECRET_ACCESS_KEY = os.getenv("DYNAMODB_SECRET_ACCESS_KEY")

# ============================================================================
# AWS SQS — Push Notification Pipeline
# ============================================================================
SQS_REGION = os.getenv("SQS_REGION", "ap-south-1")
SQS_PUSH_INCIDENTS_QUEUE_URL = os.getenv("SQS_PUSH_INCIDENTS_QUEUE_URL", "")
SQS_PUSH_DISPATCH_QUEUE_URL = os.getenv("SQS_PUSH_DISPATCH_QUEUE_URL", "")

# ============================================================================
# ============================================================================
# DEFAULT PRIMARY KEY
# ============================================================================

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ============================================================================
# LOGGING CONFIGURATION
# ============================================================================

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "json": {
            "()": "pythonjsonlogger.jsonlogger.JsonFormatter",
            "format": "%(asctime)s %(levelname)s %(name)s %(message)s",
        },
        "verbose": {
            # Fallback for local dev without json logger installed
            "format": "[{levelname}] {asctime} {name} {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "json" if not DEBUG else "verbose",
        },
        "file": {
            "class": "logging.handlers.RotatingFileHandler",
            "filename": BASE_DIR / "logs/django.log",
            "maxBytes": 1024 * 1024 * 5,
            "backupCount": 5,
            "formatter": "json" if not DEBUG else "verbose",
        },
    },
    "root": {
        "handlers": ["console", "file"],
        "level": "INFO",
    },
    "loggers": {
        "django": {
            "handlers": ["console", "file"],
            "level": os.getenv("DJANGO_LOG_LEVEL", "INFO"),
            "propagate": False,
        },
        "django.request": {
            "handlers": ["console", "file"],
            "level": "WARNING",
            "propagate": False,
        },
    },
}


# Email Configuration (AWS SES)
EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
EMAIL_HOST = os.getenv(
    "EMAIL_HOST", "email-smtp.us-east-1.amazonaws.com"
)  # ← Change region if needed
EMAIL_PORT = int(os.getenv("EMAIL_PORT", 587))
EMAIL_USE_TLS = os.getenv("EMAIL_USE_TLS", "True") == "True"
EMAIL_HOST_USER = os.getenv("EMAIL_HOST_USER")  # ← AWS SES SMTP username
EMAIL_HOST_PASSWORD = os.getenv("EMAIL_HOST_PASSWORD")  # ← AWS SES SMTP password
DEFAULT_FROM_EMAIL = os.getenv("DEFAULT_FROM_EMAIL", "noreply@neuralops.com")

# Frontend URL (for email links)
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3000")


# OAuth Configuration
GOOGLE_OAUTH_CLIENT_ID = os.getenv("GOOGLE_OAUTH_CLIENT_ID")
GOOGLE_OAUTH_CLIENT_SECRET = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET")
GOOGLE_OAUTH_REDIRECT_URI = os.getenv(
    "GOOGLE_OAUTH_REDIRECT_URI", "http://localhost:3000/auth/google/callback"
)

GITHUB_OAUTH_CLIENT_ID = os.getenv("GITHUB_OAUTH_CLIENT_ID")
GITHUB_OAUTH_CLIENT_SECRET = os.getenv("GITHUB_OAUTH_CLIENT_SECRET")
GITHUB_OAUTH_REDIRECT_URI = os.getenv(
    "GITHUB_OAUTH_REDIRECT_URI", "http://localhost:3000/auth/github/callback"
)

# Frontend OAuth callback URLs
FRONTEND_OAUTH_SUCCESS_URL = os.getenv(
    "FRONTEND_OAUTH_SUCCESS_URL", "http://localhost:3000/dashboard"
)
FRONTEND_OAUTH_ERROR_URL = os.getenv(
    "FRONTEND_OAUTH_ERROR_URL", "http://localhost:3000/login"
)


# Kafka
KAFKA_BOOTSTRAP_SERVERS = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
SCHEMA_REGISTRY_URL = os.getenv("SCHEMA_REGISTRY_URL", "http://localhost:8081")

# Django Kafka consumer — indexing status sync (Section 3: "Kafka consumption")
# Listens to events published by FastAPI's index_code Celery task via Debezium.
KAFKA_INDEXING_STATUS_TOPIC = os.getenv(
    "KAFKA_INDEXING_STATUS_TOPIC", "indexing.status"
)
KAFKA_INDEXING_STATUS_GROUP_ID = os.getenv(
    "KAFKA_INDEXING_STATUS_GROUP_ID", "django-indexing-status-consumer"
)


# Elasticsearch
ELASTICSEARCH_URL = os.getenv("ELASTICSEARCH_URL", "http://localhost:9200")


# ============================================================================
# CELERY CONFIGURATION
# ============================================================================

CELERY_BROKER_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
CELERY_RESULT_BACKEND = os.getenv("REDIS_URL", "redis://localhost:6379/0")
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = "UTC"
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_TIME_LIMIT = 300  # hard kill at 5 min
CELERY_TASK_SOFT_TIME_LIMIT = 240  # raises SoftTimeLimitExceeded at 4 min

# Retry policy (matches doc: base 5s, doubles, ceiling 300s, max 5 retries)
CELERY_TASK_MAX_RETRIES = 5
CELERY_TASK_DEFAULT_RETRY_DELAY = 5

# Dead letter queue — tasks exhausting retries write here
CELERY_TASK_REJECT_ON_WORKER_LOST = True

# Beat schedule (periodic tasks)
from celery.schedules import crontab

CELERY_BEAT_SCHEDULE = {
    "cleanup-expired-invitations-hourly": {
        "task": "users.tasks.cleanup_expired_invitations_task",
        "schedule": crontab(minute=0, hour="*"),  # hourly
        "kwargs": {"is_superadmin": True},
    },
    "cleanup-expired-tokens-hourly": {
        "task": "users.tasks.cleanup_expired_tokens_task",
        "schedule": crontab(minute=30, hour="*"),  # hourly at minute 30
        "kwargs": {"is_superadmin": True},
    },
    "sync-razorpay-subscription-status-daily": {
        "task": "billing.tasks.sync_subscription_statuses",
        "schedule": crontab(minute=0, hour=2),
    },
}


# ============================================================================
# AWS S3 / OBJECT STORAGE
# ============================================================================

AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY")
AWS_REGION_NAME = os.getenv("AWS_REGION_NAME", "us-east-1")
AWS_S3_BUCKET_NAME = os.getenv("AWS_S3_BUCKET_NAME", "neuralops-artifacts")

# Pre-signed URL expiry (15 minutes — matches doc)
AWS_S3_SIGNED_URL_EXPIRY = int(os.getenv("AWS_S3_SIGNED_URL_EXPIRY", 900))


# ============================================================================
# ELASTICSEARCH (read-only for Django analytics)
# ============================================================================

ELASTICSEARCH_URL = os.getenv("ELASTICSEARCH_URL", "http://localhost:9200")
ELASTICSEARCH_INDEX_LOGS = os.getenv("ELASTICSEARCH_INDEX_LOGS", "logs")

FERNET_ENCRYPTION_KEY = os.getenv("FERNET_ENCRYPTION_KEY")

# NOTE: Required for the integrations app.
# Generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# Add the key to .env.docker as: FERNET_ENCRYPTION_KEY=<generated-key>

# ============================================================================
# AWS DYNAMODB
# ============================================================================
DYNAMODB_REGION = os.getenv("DYNAMODB_REGION", "ap-south-1")
DYNAMODB_ACCESS_KEY_ID = os.getenv("DYNAMODB_ACCESS_KEY_ID")
DYNAMODB_SECRET_ACCESS_KEY = os.getenv("DYNAMODB_SECRET_ACCESS_KEY")

# ============================================================================
# AWS SQS — Push Notification Pipeline
# ============================================================================
SQS_REGION = os.getenv("SQS_REGION", "ap-south-1")
SQS_PUSH_INCIDENTS_QUEUE_URL = os.getenv("SQS_PUSH_INCIDENTS_QUEUE_URL", "")
SQS_PUSH_DISPATCH_QUEUE_URL = os.getenv("SQS_PUSH_DISPATCH_QUEUE_URL", "")

# ============================================================================
# WEB PUSH NOTIFICATIONS
# ============================================================================
VAPID_PRIVATE_KEY = os.getenv("VAPID_PRIVATE_KEY")
VAPID_SUBJECT = os.getenv("VAPID_SUBJECT", "mailto:admin@neuralops.com")

RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID", "rzp_test_123")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET", "secret")
RAZORPAY_WEBHOOK_SECRET = os.getenv("RAZORPAY_WEBHOOK_SECRET", "secret")

# ============================================================================
# GITHUB APP CONFIGURATION
# ============================================================================
GITHUB_APP_ID = os.getenv("GITHUB_APP_ID")
GITHUB_APP_PRIVATE_KEY = os.getenv("GITHUB_APP_PRIVATE_KEY", "").replace("\\n", "\n")
