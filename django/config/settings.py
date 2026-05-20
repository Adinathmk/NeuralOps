# CORRECTED: config/settings.py
# Fixed: AUTH_USER_MODEL + Middleware Order + Multi-Tenant Architecture

import os
from pathlib import Path
from dotenv import load_dotenv
import sys


BASE_DIR = Path(__file__).resolve().parent.parent

# Load environment variables
# In Docker, env vars come from docker-compose env_file — .env.local is only for local dev.
# load_dotenv does NOT override already-set env vars, so Docker env takes priority.
load_dotenv(BASE_DIR / '.env.local')

# ============================================================================
# DJANGO CORE SETTINGS
# ============================================================================

SECRET_KEY = os.getenv('SECRET_KEY', 'django-insecure-dev-key')
DEBUG = os.getenv('DEBUG', 'False') == 'True'
ALLOWED_HOSTS = os.getenv('ALLOWED_HOSTS', 'localhost,127.0.0.1').split(',')

# ============================================================================
# INSTALLED APPS (Order matters!)
# ============================================================================

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'rest_framework',
    'corsheaders',
    'drf_spectacular',
    # Our apps
    'tenants',
    'users',
]

# ============================================================================
# MIDDLEWARE (CORRECTED ORDER FOR MULTI-TENANCY)
# ============================================================================

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',           # ← FIXED: Position 2
    'corsheaders.middleware.CorsMiddleware',                          # ← FIXED: After session
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',        # ← FIXED: After session
    'django.contrib.messages.middleware.MessageMiddleware',           # ← FIXED: After auth
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'users.middleware.TenantMiddleware',                              # ← NEW: Multi-tenant
    'core.middleware.ExceptionHandlingMiddleware',
]

# ============================================================================
# URL CONFIGURATION
# ============================================================================

ROOT_URLCONF = 'config.urls'



# Redis Configuration
REDIS_URL = os.getenv('REDIS_URL', 'redis://localhost:6379/0')

# ============================================================================
# TEMPLATES
# ============================================================================

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

# ============================================================================
# WSGI
# ============================================================================

WSGI_APPLICATION = 'config.wsgi.application'

# ============================================================================
# DATABASE CONFIGURATION
# ============================================================================

DATABASE_URL = os.getenv('DATABASE_URL', 'sqlite:///db.sqlite3')

if DATABASE_URL.startswith('sqlite'):
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': BASE_DIR / 'db.sqlite3',
        }
    }
else:
    import dj_database_url
    DATABASES = {
        'default': dj_database_url.config(
            default=DATABASE_URL,
            conn_max_age=600,
            conn_health_checks=True
        )
    }

# ============================================================================
# CUSTOM AUTH USER MODEL (CRITICAL FOR MULTI-TENANCY)
# ============================================================================

AUTH_USER_MODEL = 'users.User'  # ← FIXED: Point to custom multi-tenant User

# ============================================================================
# AUTHENTICATION
# ============================================================================

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
        'OPTIONS': {
            'min_length': 8,
        }
    },
]

# ============================================================================
# REST FRAMEWORK CONFIGURATION
# ============================================================================

REST_FRAMEWORK = {
    'EXCEPTION_HANDLER': 'core.exception_handler.custom_exception_handler',
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'users.authentication.JWTAuthentication',
    ],
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticated',
    ],
    'DEFAULT_SCHEMA_CLASS': 'drf_spectacular.openapi.AutoSchema',
    'DEFAULT_PAGINATION_CLASS': 'rest_framework.pagination.PageNumberPagination',
    'PAGE_SIZE': 20,
    'DEFAULT_THROTTLE_CLASSES': [
        'core.throttling.TenantRateThrottle',
    ],
    'DEFAULT_THROTTLE_RATES': {
        'tenant': '60/minute',
    },
}

# ============================================================================
# SPECTACULAR CONFIGURATION
# ============================================================================

SPECTACULAR_SETTINGS = {
    'TITLE': 'NeuralOps API',
    'DESCRIPTION': 'API documentation for NeuralOps Backend',
    'VERSION': '1.0.0',
    'SERVE_INCLUDE_SCHEMA': False,
}

# ============================================================================
# CORS CONFIGURATION
# ============================================================================

CORS_ALLOWED_ORIGINS = os.getenv('CORS_ALLOWED_ORIGINS', 'http://localhost:3000').split(',')

# ============================================================================
# JWT CONFIGURATION
# ============================================================================

JWT_SECRET_KEY = os.getenv('JWT_SECRET_KEY', 'jwt-secret-dev-key')
JWT_ALGORITHM = os.getenv('JWT_ALGORITHM', 'HS256')
JWT_ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv('JWT_ACCESS_TOKEN_EXPIRE_MINUTES', 15))
JWT_REFRESH_TOKEN_EXPIRE_DAYS = int(os.getenv('JWT_REFRESH_TOKEN_EXPIRE_DAYS', 7))

# ============================================================================
# INTERNATIONALIZATION
# ============================================================================

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True

# ============================================================================
# STATIC FILES
# ============================================================================

STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'

# ============================================================================
# DEFAULT PRIMARY KEY
# ============================================================================

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# ============================================================================
# LOGGING CONFIGURATION
# ============================================================================

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {
            'format': '[{levelname}] {asctime} {name} {message}',
            'style': '{',
        },
        'simple': {
            'format': '[{levelname}] {name} {message}',
            'style': '{',
        },
    },
    'handlers': {
        # stdout handler — used by Docker (docker compose logs)
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'verbose',
        },
        # Rotating file handler — used for local dev
        'file': {
            'class': 'logging.handlers.RotatingFileHandler',
            'filename': BASE_DIR / 'logs/django.log',
            'maxBytes': 1024 * 1024 * 5,  # 5 MB
            'backupCount': 5,
            'formatter': 'verbose',
        },
    },
    'root': {
        # Both handlers active: Docker captures stdout, local dev uses file
        'handlers': ['console', 'file'],
        'level': 'INFO',
    },
    'loggers': {
        'django': {
            'handlers': ['console', 'file'],
            'level': os.getenv('DJANGO_LOG_LEVEL', 'INFO'),
            'propagate': False,
        },
        'django.request': {
            'handlers': ['console', 'file'],
            'level': 'WARNING',
            'propagate': False,
        },
    },
}   




# Email Configuration (AWS SES)
EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
EMAIL_HOST = os.getenv('EMAIL_HOST', 'email-smtp.us-east-1.amazonaws.com')  # ← Change region if needed
EMAIL_PORT = int(os.getenv('EMAIL_PORT', 587))
EMAIL_USE_TLS = os.getenv( 'EMAIL_USE_TLS', 'True' ) == 'True'
EMAIL_HOST_USER = os.getenv('EMAIL_HOST_USER')  # ← AWS SES SMTP username
EMAIL_HOST_PASSWORD = os.getenv('EMAIL_HOST_PASSWORD')  # ← AWS SES SMTP password
DEFAULT_FROM_EMAIL = os.getenv('DEFAULT_FROM_EMAIL', 'noreply@neuralops.com')

# Frontend URL (for email links)
FRONTEND_URL = os.getenv('FRONTEND_URL', 'http://localhost:3000')



# OAuth Configuration
GOOGLE_OAUTH_CLIENT_ID = os.getenv('GOOGLE_OAUTH_CLIENT_ID')
GOOGLE_OAUTH_CLIENT_SECRET = os.getenv('GOOGLE_OAUTH_CLIENT_SECRET')
GOOGLE_OAUTH_REDIRECT_URI = os.getenv('GOOGLE_OAUTH_REDIRECT_URI', 'http://localhost:3000/auth/google/callback')

GITHUB_OAUTH_CLIENT_ID = os.getenv('GITHUB_OAUTH_CLIENT_ID')
GITHUB_OAUTH_CLIENT_SECRET = os.getenv('GITHUB_OAUTH_CLIENT_SECRET')
GITHUB_OAUTH_REDIRECT_URI = os.getenv('GITHUB_OAUTH_REDIRECT_URI', 'http://localhost:3000/auth/github/callback')

# Frontend OAuth callback URLs
FRONTEND_OAUTH_SUCCESS_URL = os.getenv('FRONTEND_OAUTH_SUCCESS_URL', 'http://localhost:3000/dashboard')
FRONTEND_OAUTH_ERROR_URL = os.getenv('FRONTEND_OAUTH_ERROR_URL', 'http://localhost:3000/login')