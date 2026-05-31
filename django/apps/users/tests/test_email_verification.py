"""
Tests for email verification flow.
Location: backend/django/users/tests/test_email_verification.py
"""

import pytest
from rest_framework import status
from users.models import EmailVerification


@pytest.mark.django_db
class TestVerifyEmailView:
    """Test email verification endpoint."""

    def test_verify_email_success(self, api_client, email_verification):
        """Test successful email verification."""
        data = {
            "token": email_verification.token,
        }

        response = api_client.post("/api/v1/auth/verify-email", data, format="json")

        assert response.status_code == status.HTTP_200_OK
        assert response.data["success"] is True

        # Verify email marked as verified
        email_verification.refresh_from_db()
        assert email_verification.status == "verified"

        # Verify user email_verified is True
        email_verification.user.refresh_from_db()
        assert email_verification.user.email_verified is True

    def test_verify_email_invalid_token(self, api_client):
        """Test email verification fails with invalid token."""
        data = {
            "token": "invalid-token-xyz",
        }

        response = api_client.post("/api/v1/auth/verify-email", data, format="json")

        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_verify_email_already_verified(self, api_client, email_verification):
        """Test verification fails if already verified."""
        # Mark as verified first
        email_verification.status = "verified"
        email_verification.save()
        email_verification.user.email_verified = True
        email_verification.user.save()

        data = {
            "token": email_verification.token,
        }

        response = api_client.post("/api/v1/auth/verify-email", data, format="json")

        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_verify_email_expired_token(self, api_client, email_verification):
        """Test verification fails with expired token."""
        from datetime import timedelta

        from django.utils import timezone

        # Make token expired
        email_verification.expires_at = timezone.now() - timedelta(days=1)
        email_verification.save()

        data = {
            "token": email_verification.token,
        }

        response = api_client.post("/api/v1/auth/verify-email", data, format="json")

        assert response.status_code == status.HTTP_400_BAD_REQUEST


@pytest.mark.django_db
class TestResendVerificationEmailView:
    """Test resend verification email endpoint."""

    def test_resend_verification_success(self, api_client, unverified_user):
        """Test successful resend of verification email."""
        data = {
            "email": unverified_user.email,
        }

        response = api_client.post(
            "/api/v1/auth/resend-verification", data, format="json"
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.data["success"] is True

    def test_resend_for_nonexistent_email(self, api_client):
        """Test resend fails for non-existent email."""
        data = {
            "email": "nonexistent@example.com",
        }

        response = api_client.post(
            "/api/v1/auth/resend-verification", data, format="json"
        )

        # Should not reveal if email exists
        assert response.status_code == status.HTTP_200_OK

    def test_resend_for_verified_user(self, api_client, owner_user):
        """Test resend fails for already verified user."""
        data = {
            "email": owner_user.email,
        }

        response = api_client.post(
            "/api/v1/auth/resend-verification", data, format="json"
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
