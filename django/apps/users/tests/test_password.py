"""
Tests for password reset and change password.
Location: backend/django/users/tests/test_password.py
"""

import pytest
from rest_framework import status
from users.models import PasswordReset


@pytest.mark.django_db
class TestForgotPasswordView:
    """Test forgot password endpoint."""

    def test_forgot_password_success(self, api_client, owner_user):
        """Test successful forgot password request."""
        data = {
            "email": owner_user.email,
        }

        response = api_client.post("/api/v1/auth/forgot-password", data, format="json")

        assert response.status_code == status.HTTP_200_OK
        assert response.data["success"] is True

        # Verify reset token created
        reset_exists = PasswordReset.objects.filter(user=owner_user).exists()
        assert reset_exists is True

    def test_forgot_password_nonexistent_email(self, api_client):
        """Test forgot password doesn't reveal if email exists."""
        data = {
            "email": "nonexistent@example.com",
        }

        response = api_client.post("/api/v1/auth/forgot-password", data, format="json")

        # Should return success to prevent email enumeration
        assert response.status_code == status.HTTP_200_OK


@pytest.mark.django_db
class TestResetPasswordView:
    """Test password reset with token."""

    def test_reset_password_success(self, api_client, password_reset):
        """Test successful password reset."""
        data = {
            "token": password_reset.token,
            "new_password": "NewSecurePass123!",
            "new_password_confirm": "NewSecurePass123!",
        }

        response = api_client.post("/api/v1/auth/reset-password", data, format="json")

        assert response.status_code == status.HTTP_200_OK
        assert response.data["success"] is True

        # Verify password reset token marked as used
        password_reset.refresh_from_db()
        assert password_reset.status == "used"

    def test_reset_password_invalid_token(self, api_client):
        """Test reset password fails with invalid token."""
        data = {
            "token": "invalid-token-xyz",
            "new_password": "NewSecurePass123!",
            "new_password_confirm": "NewSecurePass123!",
        }

        response = api_client.post("/api/v1/auth/reset-password", data, format="json")

        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_reset_password_mismatch(self, api_client, password_reset):
        """Test reset password fails with mismatched passwords."""
        data = {
            "token": password_reset.token,
            "new_password": "NewSecurePass123!",
            "new_password_confirm": "DifferentPass123!",
        }

        response = api_client.post("/api/v1/auth/reset-password", data, format="json")

        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_reset_password_weak(self, api_client, password_reset):
        """Test reset password fails with weak password."""
        data = {
            "token": password_reset.token,
            "new_password": "weak",
            "new_password_confirm": "weak",
        }

        response = api_client.post("/api/v1/auth/reset-password", data, format="json")

        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_reset_password_already_used(self, api_client, password_reset):
        """Test reset password fails if token already used."""
        # Mark as used
        password_reset.status = "used"
        password_reset.save()

        data = {
            "token": password_reset.token,
            "new_password": "NewSecurePass123!",
            "new_password_confirm": "NewSecurePass123!",
        }

        response = api_client.post("/api/v1/auth/reset-password", data, format="json")

        assert response.status_code == status.HTTP_400_BAD_REQUEST


@pytest.mark.django_db
class TestChangePasswordView:
    """Test change password endpoint."""

    def test_change_password_success(self, owner_client, owner_user):
        """Test successful password change."""
        data = {
            "current_password": "TestPass123!",
            "new_password": "NewSecurePass123!",
            "new_password_confirm": "NewSecurePass123!",
        }

        response = owner_client.post("/api/v1/auth/change-password", data, format="json")

        assert response.status_code == status.HTTP_200_OK
        assert response.data["success"] is True

        # Verify password changed
        owner_user.refresh_from_db()
        assert owner_user.check_password("NewSecurePass123!")

    def test_change_password_wrong_current(self, owner_client):
        """Test change password fails with wrong current password."""
        data = {
            "current_password": "WrongPassword123!",
            "new_password": "NewSecurePass123!",
            "new_password_confirm": "NewSecurePass123!",
        }

        response = owner_client.post("/api/v1/auth/change-password", data, format="json")

        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_change_password_mismatch(self, owner_client):
        """Test change password fails with mismatched new passwords."""
        data = {
            "current_password": "TestPass123!",
            "new_password": "NewSecurePass123!",
            "new_password_confirm": "DifferentPass123!",
        }

        response = owner_client.post("/api/v1/auth/change-password", data, format="json")

        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_change_password_requires_auth(self, api_client):
        """Test change password requires authentication."""
        data = {
            "current_password": "TestPass123!",
            "new_password": "NewSecurePass123!",
            "new_password_confirm": "NewSecurePass123!",
        }

        response = api_client.post("/api/v1/auth/change-password", data, format="json")

        assert response.status_code == status.HTTP_401_UNAUTHORIZED
