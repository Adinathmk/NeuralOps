import logging
import base64
from io import BytesIO

import qrcode
from django.db import transaction
from django.contrib.auth.hashers import check_password

from ..authentication import JWTAuthentication
from ..models import MFAVerificationToken, TOTPDevice, BackupCode
from ..cache import cache_manager

logger = logging.getLogger(__name__)


class MFAService:

    @staticmethod
    def setup(user):
        """
        Initialises MFA setup for a user:
        - Blocks setup if MFA is already confirmed
        - Deletes any existing unconfirmed device
        - Generates a new TOTP secret
        - Creates an unconfirmed TOTPDevice
        - Generates and returns the QR code as a base64 PNG string
        Returns dict with secret, qr_code, setup_url.
        """
        # Check if MFA already enabled
        existing_device = TOTPDevice.objects.filter(
            user=user,
            is_confirmed=True
        ).exists()

        if existing_device:
            return None, 'already_enabled'

        # Delete existing unconfirmed device
        TOTPDevice.objects.filter(user=user, is_confirmed=False).delete()

        # Generate new secret
        secret = TOTPDevice.generate_secret()

        # Create device
        device = TOTPDevice.objects.create(
            user=user,
            secret_key=secret,
            is_confirmed=False
        )

        # Generate QR setup URL
        qr_url = device.get_qr_code(user.email)

        # Generate QR image
        qr = qrcode.QRCode()
        qr.add_data(qr_url)
        qr.make()

        img = qr.make_image(
            fill_color="black",
            back_color="white"
        )

        img_bytes = BytesIO()
        img.save(img_bytes)

        img_base64 = base64.b64encode(
            img_bytes.getvalue()
        ).decode()

        logger.info(f"MFA setup started for {user.email}")

        return {
            'secret': secret,
            'qr_code': f'data:image/png;base64,{img_base64}',
            'setup_url': qr_url,
            'message': (
                'Scan QR code with Google Authenticator, '
                'Authy, or Microsoft Authenticator'
            )
        }, None

    @staticmethod
    def confirm(user, code):
        """
        Confirms MFA setup by verifying the first TOTP code:
        - Gets the unconfirmed TOTPDevice
        - Verifies the TOTP code
        - Confirms the device
        - Generates 10 backup codes (stored hashed)
        Returns (backup_codes_list, error_code).
        error_code can be: 'setup_required' | 'invalid_code' | None
        """
        try:
            device = TOTPDevice.objects.get(user=user, is_confirmed=False)
        except TOTPDevice.DoesNotExist:
            return None, 'setup_required'

        # Verify code
        if not device.verify_token(code):
            logger.warning(f"Invalid MFA code for {user.email}")
            return None, 'invalid_code'

        try:
            with transaction.atomic():
                # Confirm device
                device.confirm()

                # Delete old backup codes
                BackupCode.objects.filter(user=user).delete()

                # Generate new backup codes
                codes = BackupCode.generate_codes(count=10)
                backup_codes = []

                for code in codes:
                    code_hash = BackupCode.hash_code(code)
                    BackupCode.objects.create(
                        user=user,
                        code_hash=code_hash
                    )
                    backup_codes.append(code)

                logger.info(f"MFA confirmed for {user.email}")
                return backup_codes, None

        except Exception as e:
            logger.error(f"MFA confirmation error: {str(e)}")
            return None, 'mfa_error'

    @staticmethod
    def verify_token(mfa_token, code, request):
        """
        Exchanges a temporary MFA token + TOTP/backup code for full JWT tokens:
        - Validates MFA token exists and is not expired
        - Checks rate limit (max 5 attempts per user)
        - Tries TOTP code first, then backup code
        Returns (result_dict, error_code).
        result_dict contains 'user', 'access_token', 'refresh_token'.
        error_code can be: 'invalid_token' | 'token_expired' | 'rate_limited' | 'invalid_code' | None
        """
        # Verify MFA token exists and is valid
        try:
            mfa_token_obj = MFAVerificationToken.objects.get(token=mfa_token)
        except MFAVerificationToken.DoesNotExist:
            logger.warning(f"Invalid MFA token used")
            return None, 'invalid_token'

        if not mfa_token_obj.is_valid():
            mfa_token_obj.delete()
            return None, 'token_expired'

        user = mfa_token_obj.user

        # Check rate limiting on MFA attempts
        mfa_attempts = cache_manager.get_mfa_attempts(user.email)
        if mfa_attempts >= 5:
            # Lock MFA verification for 15 minutes
            cache_manager.lock_mfa_verification(user.email, 15)
            logger.warning(f"MFA verification rate limited for {user.email}")
            return None, 'rate_limited'

        # Try TOTP code first
        device = TOTPDevice.objects.get(user=user)

        if device.verify_token(code):
            # Valid TOTP code
            cache_manager.reset_mfa_attempts(user.email)
            mfa_token_obj.delete()

            access_token, refresh_token = JWTAuthentication.generate_tokens(user, request)

            logger.info(f"MFA verification success for {user.email}")

            return {
                'user': user,
                'access_token': access_token,
                'refresh_token': refresh_token,
            }, None

        # Try backup code
        backup = BackupCode.objects.filter(user=user, is_used=False).first()

        if backup and check_password(code, backup.code_hash):
            # Valid backup code
            backup.use()
            cache_manager.reset_mfa_attempts(user.email)
            mfa_token_obj.delete()

            access_token, refresh_token = JWTAuthentication.generate_tokens(user, request)

            logger.warning(f"Backup code used by {user.email}")

            return {
                'user': user,
                'access_token': access_token,
                'refresh_token': refresh_token,
                'used_backup_code': True,
            }, None

        # Invalid code
        cache_manager.increment_mfa_attempts(user.email)
        logger.warning(f"Invalid MFA code for {user.email}")
        return None, 'invalid_code'

    @staticmethod
    def disable(user, password, code):
        """
        Disables MFA for a user:
        - Verifies user's password
        - Gets the confirmed TOTPDevice
        - Verifies the TOTP code
        - Deletes the device and all backup codes (within a transaction)
        Returns (success: bool, error_code: str | None).
        error_code can be: 'wrong_password' | 'mfa_not_enabled' | 'code_required' | 'invalid_code' | 'mfa_error' | None
        """
        # Verify password
        if not user.check_password(password):
            logger.warning(
                f"Password verification failed for MFA disable - {user.email}"
            )
            return False, 'wrong_password'

        # Get confirmed device
        try:
            device = TOTPDevice.objects.get(user=user, is_confirmed=True)
        except TOTPDevice.DoesNotExist:
            return False, 'mfa_not_enabled'

        if not code:
            return False, 'code_required'

        # Verify code
        if not device.verify_token(code):
            logger.warning(f"Invalid MFA code during disable - {user.email}")
            return False, 'invalid_code'

        # Disable MFA
        try:
            with transaction.atomic():
                device.delete()
                BackupCode.objects.filter(user=user).delete()

                logger.info(f"MFA disabled for {user.email}")
                return True, None

        except Exception as e:
            logger.error(f"MFA disable error: {str(e)}")
            return False, 'mfa_error'
