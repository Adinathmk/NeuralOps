"""
Global conftest.py - Root level pytest configuration
Place this at: backend/django/conftest.py
"""

import os

import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache
from faker import Faker
from rest_framework.test import APIClient
from tenants.models import Tenant
from users.authentication import JWTAuthentication
from users.models import (
    EmailVerification,
    OAuthAccount,
    PasswordReset,
    UserInvitation,
    UserSession,
)

User = get_user_model()
fake = Faker()

# Configure Django settings before importing models
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

from django.conf import settings

settings.CELERY_TASK_ALWAYS_EAGER = True

# Override cache backend to LocMemCache for tests so throttling and cache calls
# never require a real Redis connection. This keeps tests hermetic — they pass
# whether or not Redis is reachable in the environment.
settings.CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
    }
}


# ============================================================================
# FIXTURES: API Client
# ============================================================================


@pytest.fixture
def api_client():
    """Basic API client for unauthenticated requests."""
    return APIClient()


# ============================================================================
# FIXTURES: Tenants
# ============================================================================


@pytest.fixture
def tenant():
    """Create a test tenant."""
    return Tenant.objects.create(
        name="Test Company", slug="test-company", plan_tier="free", status="active"
    )


@pytest.fixture
def tenant_2():
    """Create a second test tenant."""
    return Tenant.objects.create(
        name="Second Company", slug="second-company", plan_tier="pro", status="active"
    )


# ============================================================================
# FIXTURES: Users
# ============================================================================


@pytest.fixture
def owner_user(tenant):
    """Create an owner user."""
    user = User.objects.create_user(
        email="owner@example.com",
        password="TestPass123!",
        tenant=tenant,
        first_name="Owner",
        last_name="User",
        role="owner",
        email_verified=True,
    )
    return user


@pytest.fixture
def admin_user(tenant):
    """Create an admin user."""
    user = User.objects.create_user(
        email="admin@example.com",
        password="TestPass123!",
        tenant=tenant,
        first_name="Admin",
        last_name="User",
        role="admin",
        email_verified=True,
    )
    return user


@pytest.fixture
def engineer_user(tenant):
    """Create an engineer user."""
    user = User.objects.create_user(
        email="engineer@example.com",
        password="TestPass123!",
        tenant=tenant,
        first_name="Engineer",
        last_name="User",
        role="engineer",
        email_verified=True,
    )
    return user


@pytest.fixture
def viewer_user(tenant):
    """Create a viewer user."""
    user = User.objects.create_user(
        email="viewer@example.com",
        password="TestPass123!",
        tenant=tenant,
        first_name="Viewer",
        last_name="User",
        role="viewer",
        email_verified=True,
    )
    return user


@pytest.fixture
def unverified_user(tenant):
    """Create an unverified user."""
    user = User.objects.create_user(
        email="unverified@example.com",
        password="TestPass123!",
        tenant=tenant,
        email_verified=False,
    )
    return user


# ============================================================================
# FIXTURES: OAuth Accounts
# ============================================================================


@pytest.fixture
def google_oauth_account(owner_user):
    """Create Google OAuth account."""
    return OAuthAccount.objects.create(
        user=owner_user,
        provider="google",
        provider_user_id="google-user-123",
        provider_email=owner_user.email,
        provider_name=owner_user.get_full_name(),
        provider_picture_url="https://example.com/photo.jpg",
    )


@pytest.fixture
def github_oauth_account(engineer_user):
    """Create GitHub OAuth account."""
    return OAuthAccount.objects.create(
        user=engineer_user,
        provider="github",
        provider_user_id="github-user-456",
        provider_email=engineer_user.email,
        provider_name=engineer_user.get_full_name(),
        provider_picture_url="https://github.com/photo.jpg",
    )


# ============================================================================
# FIXTURES: Invitations
# ============================================================================


@pytest.fixture
def invitation(tenant, owner_user):
    """Create a pending invitation."""
    return UserInvitation.objects.create(
        email="invited@example.com",
        tenant=tenant,
        invited_by=owner_user,
        role="engineer",
        status="pending",
    )


@pytest.fixture
def expired_invitation(tenant, owner_user):
    """Create an expired invitation."""
    from datetime import timedelta

    from django.utils import timezone

    inv = UserInvitation.objects.create(
        email="expired@example.com",
        tenant=tenant,
        invited_by=owner_user,
        role="engineer",
        status="pending",
    )
    inv.expires_at = timezone.now() - timedelta(days=1)
    inv.save()
    return inv


# ============================================================================
# FIXTURES: Sessions
# ============================================================================


@pytest.fixture
def user_session(owner_user):
    """Create a user session."""
    from datetime import timedelta

    from django.utils import timezone

    return UserSession.objects.create(
        current_refresh_jti="test-session-id",
        user=owner_user,
        tenant=owner_user.tenant,
        device_name="Test Device",
        ip_address="127.0.0.1",
        user_agent="Test Agent",
        expires_at=timezone.now() + timedelta(hours=24),
    )


# ============================================================================
# FIXTURES: Email Verification
# ============================================================================


@pytest.fixture
def email_verification(unverified_user):
    """Create email verification token."""
    return EmailVerification.objects.create(user=unverified_user, status="pending")


# ============================================================================
# FIXTURES: Password Reset
# ============================================================================


@pytest.fixture
def password_reset(owner_user):
    """Create password reset token."""
    return PasswordReset.objects.create(
        user=owner_user, status="pending", ip_address="127.0.0.1"
    )


# ============================================================================
# FIXTURES: JWT Tokens
# ============================================================================


@pytest.fixture
def access_token(owner_user):
    """Generate valid access token for owner."""
    token, _ = JWTAuthentication.generate_tokens(owner_user)
    return token


@pytest.fixture
def refresh_token(owner_user):
    """Generate valid refresh token for owner."""
    _, token = JWTAuthentication.generate_tokens(owner_user)
    return token


@pytest.fixture
def engineer_access_token(engineer_user):
    """Generate valid access token for engineer."""
    token, _ = JWTAuthentication.generate_tokens(engineer_user)
    return token


@pytest.fixture
def admin_access_token(admin_user):
    """Generate valid access token for admin."""
    token, _ = JWTAuthentication.generate_tokens(admin_user)
    return token


# ============================================================================
# FIXTURES: Authenticated Clients
# ============================================================================


@pytest.fixture
def owner_client(api_client, access_token):
    """API client authenticated as owner."""
    api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token}")
    return api_client


@pytest.fixture
def admin_client(api_client, admin_access_token):
    """API client authenticated as admin."""
    api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {admin_access_token}")
    return api_client


@pytest.fixture
def engineer_client(api_client, engineer_access_token):
    """API client authenticated as engineer."""
    api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {engineer_access_token}")
    return api_client
