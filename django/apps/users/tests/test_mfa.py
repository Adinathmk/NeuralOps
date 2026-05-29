"""
Tests for MFA (TOTP) functionality.
Location: backend/django/users/tests/test_mfa.py
"""

from unittest.mock import patch

import pyotp
import pytest
from django.contrib.auth import get_user_model
from rest_framework import status

User = get_user_model()


@pytest.mark.django_db
class TestSetupMFAView:
    """Test MFA setup endpoint."""

    def test_setup_mfa_success(self, owner_client, owner_user):
        """Test successful MFA setup."""
        response = owner_client.get("/api/auth/mfa/setup")

        assert response.status_code == status.HTTP_200_OK
        assert response.data["success"] is True
        assert "secret" in response.data["data"]
        assert "qr_code" in response.data["data"]
        assert "setup_url" in response.data["data"]
        assert response.data["data"]["qr_code"].startswith("data:image/png;base64,")

    def test_setup_mfa_creates_device(self, owner_client, owner_user):
        """Test that setup creates TOTP device."""
        from users.models import TOTPDevice

        response = owner_client.get("/api/auth/mfa/setup")

        assert response.status_code == status.HTTP_200_OK

        # Check device created (not confirmed yet)
        device = TOTPDevice.objects.get(user=owner_user)
        assert device.is_confirmed is False

    def test_setup_mfa_deletes_old_unconfirmed(self, owner_client, owner_user):
        """Test that old unconfirmed devices are deleted."""
        from users.models import TOTPDevice

        # Create old unconfirmed device
        old_device = TOTPDevice.objects.create(
            user=owner_user, secret_key=TOTPDevice.generate_secret(), is_confirmed=False
        )

        # Setup MFA again
        response = owner_client.get("/api/auth/mfa/setup")

        assert response.status_code == status.HTTP_200_OK

        # Old device should be deleted
        assert not TOTPDevice.objects.filter(id=old_device.id).exists()

    def test_setup_mfa_requires_auth(self, api_client):
        """Test setup requires authentication."""
        response = api_client.get("/api/auth/mfa/setup")

        assert response.status_code == status.HTTP_401_UNAUTHORIZED


@pytest.mark.django_db
class TestConfirmMFAView:
    """Test MFA confirmation endpoint."""

    def test_confirm_mfa_success(self, owner_client, owner_user):
        """Test successful MFA confirmation."""
        from users.models import BackupCode, TOTPDevice

        # Setup MFA first
        owner_client.get("/api/auth/mfa/setup")

        # Get device and generate valid code
        device = TOTPDevice.objects.get(user=owner_user)
        totp = pyotp.TOTP(device.secret_key)
        code = totp.now()

        # Confirm MFA
        response = owner_client.post(
            "/api/auth/mfa/confirm", {"code": code}, format="json"
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.data["success"] is True
        assert "backup_codes" in response.data["data"]
        assert len(response.data["data"]["backup_codes"]) == 10

        # Device should be confirmed
        device.refresh_from_db()
        assert device.is_confirmed is True

        # Backup codes should be created
        assert BackupCode.objects.filter(user=owner_user).count() == 10

    def test_confirm_mfa_invalid_code(self, owner_client, owner_user):
        from users.models import TOTPDevice

        """Test confirmation fails with invalid code."""
        # Setup MFA first
        owner_client.get("/api/auth/mfa/setup")

        # Try invalid code
        response = owner_client.post(
            "/api/auth/mfa/confirm", {"code": "000000"}, format="json"
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.data["success"] is False

        # Device should still not be confirmed
        device = TOTPDevice.objects.get(user=owner_user)
        assert device.is_confirmed is False

    def test_confirm_mfa_invalid_format(self, owner_client):
        """Test confirmation fails with invalid code format."""
        # Setup MFA first
        owner_client.get("/api/auth/mfa/setup")

        # Try non-numeric code
        response = owner_client.post(
            "/api/auth/mfa/confirm", {"code": "abcdef"}, format="json"
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_confirm_mfa_setup_not_started(self, owner_client):
        """Test confirmation fails if setup not started."""
        # Try to confirm without setup
        response = owner_client.post(
            "/api/auth/mfa/confirm", {"code": "123456"}, format="json"
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "setup_required" in response.data["code"]

    def test_confirm_mfa_requires_auth(self, api_client):
        """Test confirmation requires authentication."""
        response = api_client.post(
            "/api/auth/mfa/confirm", {"code": "123456"}, format="json"
        )

        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_confirm_mfa_generates_backup_codes(self, owner_client, owner_user):
        """Test backup codes are properly generated."""
        from users.models import BackupCode, TOTPDevice

        # Setup and confirm
        owner_client.get("/api/auth/mfa/setup")
        device = TOTPDevice.objects.get(user=owner_user)
        totp = pyotp.TOTP(device.secret_key)

        response = owner_client.post(
            "/api/auth/mfa/confirm", {"code": totp.now()}, format="json"
        )

        # Check backup codes
        backup_codes = BackupCode.objects.filter(user=owner_user)
        assert backup_codes.count() == 10

        # All should be unused
        assert all(not code.is_used for code in backup_codes)


@pytest.mark.django_db
class TestVerifyMFATokenView:
    """Test MFA verification (login) endpoint."""

    @pytest.fixture(autouse=True)
    def reset_mfa_cache(self, owner_user):
        from users.cache import cache_manager

        cache_manager.reset_mfa_attempts(owner_user.email)
        cache_manager.redis_client.delete(f"mfa_locked:{owner_user.email}")
        yield

    def test_verify_mfa_token_success(self, api_client, owner_user):
        """Test successful MFA token verification."""
        from users.models import MFAVerificationToken, TOTPDevice

        # Setup and confirm MFA
        device = TOTPDevice.objects.create(
            user=owner_user,
            secret_key=TOTPDevice.generate_secret(),
            is_confirmed=True,
            confirmed_at="2025-05-15T10:00:00Z",
        )

        # Create MFA verification token
        mfa_token = MFAVerificationToken.objects.create(user=owner_user)

        # Get valid code
        totp = pyotp.TOTP(device.secret_key)
        code = totp.now()

        # Verify token
        response = api_client.post(
            "/api/auth/mfa/verify",
            {"mfa_token": mfa_token.token, "code": code},
            format="json",
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.data["success"] is True
        assert "access_token" in response.data
        assert "refresh_token" in response.data

        # Token should be deleted
        assert not MFAVerificationToken.objects.filter(id=mfa_token.id).exists()

    def test_verify_mfa_token_invalid_code(self, api_client, owner_user):
        """Test verification fails with invalid code."""
        from users.models import MFAVerificationToken, TOTPDevice

        # Setup MFA
        TOTPDevice.objects.create(
            user=owner_user, secret_key=TOTPDevice.generate_secret(), is_confirmed=True
        )

        mfa_token = MFAVerificationToken.objects.create(user=owner_user)

        # Try invalid code
        response = api_client.post(
            "/api/auth/mfa/verify",
            {"mfa_token": mfa_token.token, "code": "000000"},
            format="json",
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.data["success"] is False

    def test_verify_mfa_token_invalid_token(self, api_client):
        """Test verification fails with invalid token."""
        response = api_client.post(
            "/api/auth/mfa/verify",
            {"mfa_token": "invalid-token", "code": "123456"},
            format="json",
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "Invalid MFA token" in response.data["message"]

    def test_verify_mfa_token_expired(self, api_client, owner_user):
        """Test verification fails with expired token."""
        from datetime import timedelta

        from django.utils import timezone
        from users.models import MFAVerificationToken

        # Create expired token
        mfa_token = MFAVerificationToken.objects.create(
            user=owner_user, expires_at=timezone.now() - timedelta(minutes=10)
        )

        response = api_client.post(
            "/api/auth/mfa/verify",
            {"mfa_token": mfa_token.token, "code": "123456"},
            format="json",
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "expired" in response.data["message"]

    def test_verify_mfa_token_backup_code(self, api_client, owner_user):
        """Test verification with backup code."""
        from django.contrib.auth.hashers import make_password
        from users.models import BackupCode, MFAVerificationToken, TOTPDevice

        # Setup MFA Device
        TOTPDevice.objects.create(
            user=owner_user, secret_key=TOTPDevice.generate_secret(), is_confirmed=True
        )

        # Isolate database state by cleaning up pre-existing backup codes
        BackupCode.objects.filter(user=owner_user).delete()

        # Create clear-text backup code and store its database reference
        backup_code = "BACKUP123456"
        test_backup = BackupCode.objects.create(
            user=owner_user, code_hash=make_password(backup_code)
        )

        # Create temporary MFA verification token
        mfa_token = MFAVerificationToken.objects.create(user=owner_user)

        # Verify using the plain-text backup code string
        response = api_client.post(
            "/api/auth/mfa/verify",
            {"mfa_token": mfa_token.token, "code": backup_code},
            format="json",
        )

        # Verify API response expectations
        assert response.status_code == status.HTTP_200_OK
        assert "access_token" in response.data

        # Pull fresh row attributes down from the database using our stored instance ID
        test_backup.refresh_from_db()

        # Verify that the backup code was marked as used by the view lifecycle
        assert test_backup.is_used is True

    def test_verify_mfa_rate_limiting(self, api_client, owner_user):
        """Test rate limiting on failed MFA attempts."""
        from users.models import MFAVerificationToken, TOTPDevice

        # Setup MFA
        TOTPDevice.objects.create(
            user=owner_user, secret_key=TOTPDevice.generate_secret(), is_confirmed=True
        )

        # Make 5 failed attempts
        for i in range(5):
            mfa_token = MFAVerificationToken.objects.create(user=owner_user)
            api_client.post(
                "/api/auth/mfa/verify",
                {"mfa_token": mfa_token.token, "code": "000000"},
                format="json",
            )

        # 6th attempt should be rate limited
        mfa_token = MFAVerificationToken.objects.create(user=owner_user)
        response = api_client.post(
            "/api/auth/mfa/verify",
            {"mfa_token": mfa_token.token, "code": "000000"},
            format="json",
        )

        assert response.status_code == status.HTTP_429_TOO_MANY_REQUESTS


@pytest.mark.django_db
class TestDisableMFAView:
    """Test MFA disable endpoint."""

    def test_disable_mfa_success(self, owner_client, owner_user):
        """Test successful MFA disable."""
        from users.models import BackupCode, TOTPDevice

        # Setup and confirm MFA
        device = TOTPDevice.objects.create(
            user=owner_user, secret_key=TOTPDevice.generate_secret(), is_confirmed=True
        )

        # Create backup code
        BackupCode.objects.create(user=owner_user, code_hash="test")

        # Get TOTP code
        totp = pyotp.TOTP(device.secret_key)
        code = totp.now()

        # Disable MFA
        response = owner_client.post(
            "/api/auth/mfa/disable",
            {"password": "TestPass123!", "code": code},
            format="json",
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.data["success"] is True

        # Device should be deleted
        assert not TOTPDevice.objects.filter(user=owner_user).exists()

        # Backup codes should be deleted
        assert BackupCode.objects.filter(user=owner_user).count() == 0

    def test_disable_mfa_wrong_password(self, owner_client, owner_user):
        """Test disable fails with wrong password."""
        from users.models import TOTPDevice

        # Setup MFA
        device = TOTPDevice.objects.create(
            user=owner_user, secret_key=TOTPDevice.generate_secret(), is_confirmed=True
        )

        totp = pyotp.TOTP(device.secret_key)

        # Try wrong password
        response = owner_client.post(
            "/api/auth/mfa/disable",
            {"password": "WrongPassword123!", "code": totp.now()},
            format="json",
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "Incorrect password" in response.data["message"]

    def test_disable_mfa_wrong_code(self, owner_client, owner_user):
        """Test disable fails with wrong TOTP code."""
        from users.models import TOTPDevice

        # Setup MFA
        TOTPDevice.objects.create(
            user=owner_user, secret_key=TOTPDevice.generate_secret(), is_confirmed=True
        )

        # Try wrong code
        response = owner_client.post(
            "/api/auth/mfa/disable",
            {"password": "TestPass123!", "code": "000000"},
            format="json",
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "Invalid MFA code" in response.data["message"]

    def test_disable_mfa_not_enabled(self, owner_client):
        """Test disable fails if MFA not enabled."""
        response = owner_client.post(
            "/api/auth/mfa/disable",
            {"password": "TestPass123!", "code": "123456"},
            format="json",
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "MFA not enabled" in response.data["message"]

    def test_disable_mfa_code_required(self, owner_client, owner_user):
        """Test disable requires TOTP code."""
        from users.models import TOTPDevice

        # Setup MFA
        TOTPDevice.objects.create(
            user=owner_user, secret_key=TOTPDevice.generate_secret(), is_confirmed=True
        )

        # Try without code
        response = owner_client.post(
            "/api/auth/mfa/disable",
            {
                "password": "TestPass123!",
            },
            format="json",
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "MFA code required" in response.data["message"]

    def test_disable_mfa_requires_auth(self, api_client):
        """Test disable requires authentication."""
        response = api_client.post(
            "/api/auth/mfa/disable",
            {"password": "TestPass123!", "code": "123456"},
            format="json",
        )

        assert response.status_code == status.HTTP_401_UNAUTHORIZED


@pytest.mark.django_db
class TestLoginWithMFA:
    """Test login flow with MFA enabled."""

    def test_login_with_mfa_enabled(self, api_client, owner_user):
        """Test login returns MFA token when MFA enabled."""
        from users.models import TOTPDevice

        # Setup MFA
        TOTPDevice.objects.create(
            user=owner_user, secret_key=TOTPDevice.generate_secret(), is_confirmed=True
        )

        # Login
        response = api_client.post(
            "/api/auth/login",
            {
                "email": owner_user.email,
                "password": "TestPass123!",
                "tenant_slug": owner_user.tenant.slug,
            },
            format="json",
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.data["success"] is True
        assert response.data.get("requires_mfa") is True
        assert "mfa_token" in response.data
        assert "access_token" not in response.data  # Not issued yet

    def test_login_without_mfa_enabled(self, api_client, owner_user):
        """Test login returns tokens when MFA not enabled."""
        # Login without MFA setup
        response = api_client.post(
            "/api/auth/login",
            {
                "email": owner_user.email,
                "password": "TestPass123!",
                "tenant_slug": owner_user.tenant.slug,
            },
            format="json",
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.data["success"] is True
        assert "access_token" in response.data
        assert "refresh_token" in response.data
        assert not response.data.get("requires_mfa", False)


@pytest.mark.django_db
class TestTOTPDeviceModel:
    """Test TOTP device model methods."""

    def test_generate_secret(self):
        """Test secret generation."""
        from users.models import TOTPDevice

        secret = TOTPDevice.generate_secret()
        assert len(secret) == 32
        assert secret.isalnum()

    def test_verify_token(self, owner_user):
        """Test TOTP token verification."""
        from users.models import TOTPDevice

        device = TOTPDevice.objects.create(
            user=owner_user, secret_key=TOTPDevice.generate_secret(), is_confirmed=False
        )

        totp = pyotp.TOTP(device.secret_key)
        code = totp.now()

        # Verify current code
        assert device.verify_token(code) is True

        # Verify wrong code
        assert device.verify_token("000000") is False

    def test_get_qr_code(self, owner_user):
        """Test QR code URL generation."""
        from users.models import TOTPDevice

        device = TOTPDevice.objects.create(
            user=owner_user, secret_key=TOTPDevice.generate_secret(), is_confirmed=False
        )

        qr_url = device.get_qr_code(owner_user.email)

        assert "otpauth://totp/" in qr_url
        assert "owner%40example.com" in qr_url
        assert "NeuralOps" in qr_url

    def test_confirm_device(self, owner_user):
        """Test device confirmation."""
        from users.models import TOTPDevice

        device = TOTPDevice.objects.create(
            user=owner_user, secret_key=TOTPDevice.generate_secret(), is_confirmed=False
        )

        assert device.confirmed_at is None

        device.confirm()

        assert device.is_confirmed is True
        assert device.confirmed_at is not None


@pytest.mark.django_db
class TestBackupCodeModel:
    """Test backup code model methods."""

    def test_generate_codes(self):
        """Test backup code generation."""
        from users.models import BackupCode

        codes = BackupCode.generate_codes(count=10)

        assert len(codes) == 10
        assert all(len(code) > 0 for code in codes)
        assert len(set(codes)) == 10  # All unique

    def test_hash_code(self):
        """Test backup code hashing."""
        from users.models import BackupCode

        code = "BACKUP123456"
        hashed = BackupCode.hash_code(code)

        # Hash should be different from original
        assert hashed != code
        assert len(hashed) > 0

    def test_use_backup_code(self, owner_user):
        """Test marking backup code as used."""
        from users.models import BackupCode

        backup = BackupCode.objects.create(user=owner_user, code_hash="test_hash")

        assert backup.is_used is False
        assert backup.used_at is None

        backup.use()

        assert backup.is_used is True
        assert backup.used_at is not None
