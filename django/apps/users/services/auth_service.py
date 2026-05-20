import logging
from django.conf import settings
from django.utils import timezone

from ..authentication import JWTAuthentication
from ..models import (
    User, AuditLog, EmailVerification,
    MFAVerificationToken, TOTPDevice, UserSession
)
from ..email import email_service
from ..cache import cache_manager
from core.responses import APIResponse
from core.exceptions import ValidationException

logger = logging.getLogger(__name__)


class AuthService:

    @staticmethod
    def register(user, frontend_url):
        """
        Post-registration side effects:
        Creates email verification token and sends verification email.
        Called after serializer.save() in RegisterView.
        """
        verification = EmailVerification.objects.create(user=user)

        try:
            email_service.send_verification_email(
                user=user,
                verification_token=verification.token,
                frontend_url=frontend_url
            )
        except Exception as e:
            logger.error(f"Failed to send verification email: {str(e)}")

    @staticmethod
    def login(user, request):
        """
        Handles post-validation login logic.
        - If MFA is enabled: creates MFA token, returns 'requires_mfa' response.
        - If MFA is disabled: generates JWT tokens, creates AuditLog, returns success response.
        """
        try:
            # MFA is enabled — return temporary token
            TOTPDevice.objects.get(user=user, is_confirmed=True)

            mfa_token_obj = MFAVerificationToken.objects.create(user=user)
            logger.info(f"MFA verification required for {user.email}")

            return APIResponse.success(
                message='MFA required. Please verify with authenticator app.',
                mfa_token=mfa_token_obj.token,
                requires_mfa=True
            )

        except TOTPDevice.DoesNotExist:
            # MFA not enabled — return access tokens directly
            access_token, refresh_token = JWTAuthentication.generate_tokens(user, request)

            logger.info(
                f"User {user.email} logged in from "
                f"{JWTAuthentication._get_client_ip(request)}"
            )

            AuditLog.objects.create(
                tenant=user.tenant,
                user=user,
                user_email=user.email,
                action='LOGIN',
                ip_address=JWTAuthentication._get_client_ip(request)
            )

            from ..serializers import UserSerializer
            return APIResponse.success(
                data=UserSerializer(user).data,
                message='Login successful.',
                access_token=access_token,
                refresh_token=refresh_token
            )

    @staticmethod
    def handle_unverified_login(email, frontend_url):
        """
        When login fails because email is unverified:
        Deletes old verification tokens and sends a fresh one.
        """
        try:
            user = User.objects.get(email=email)

            if not user.email_verified:
                EmailVerification.objects.filter(user=user).delete()
                verification = EmailVerification.objects.create(user=user)

                try:
                    email_service.send_verification_email(
                        user=user,
                        verification_token=verification.token,
                        frontend_url=frontend_url
                    )
                except Exception as e:
                    logger.error(f"Failed to resend verification email: {str(e)}")

        except User.DoesNotExist:
            pass

    @staticmethod
    def record_login_failure(email, serializer, request):
        """
        Records a failed login attempt: increments rate limit counter,
        logs warning if multiple failures, creates AuditLog.
        Returns the APIResponse error.
        """
        failed_count = cache_manager.increment_failed_login(email)

        if failed_count >= 3:
            logger.warning(f"Multiple failed login attempts for {email}: {failed_count}")

        first_error = next(iter(serializer.errors.values()))[0]

        AuditLog.objects.create(
            user_email=email,
            action='LOGIN_FAILED',
            success=False,
            description=f"error: {first_error}",
            ip_address=JWTAuthentication._get_client_ip(request)
        )

        return APIResponse.error(
            message=first_error,
            status_code=401,
            code='auth_error',
            errors=serializer.errors
        )

    @staticmethod
    def refresh_token(refresh_token_str):
        """
        Verifies the refresh token, fetches the user,
        and generates a new access/refresh token pair.
        Returns (access_token, refresh_token).
        """
        payload = JWTAuthentication.verify_token(refresh_token_str)
        user = User.objects.get(id=payload['user_id'])
        access_token, refresh_token = JWTAuthentication.generate_tokens(user)
        return access_token, refresh_token

    @staticmethod
    def logout(request):
        """
        Blocklists the JWT's JTI, revokes the session record,
        and creates an AuditLog entry.
        """
        jti = request.auth.get('jti')

        if not jti:
            raise ValidationException('Invalid token format')

        # Add token to revocation blocklist
        exp_time = request.auth.get('exp')
        if exp_time:
            remaining_seconds = int(exp_time - timezone.now().timestamp())
            if remaining_seconds > 0:
                cache_manager.blocklist_token(jti, remaining_seconds)

        # Revoke session record
        try:
            session = UserSession.objects.get(session_id=jti)
            session.revoke()
        except UserSession.DoesNotExist:
            pass

        logger.info(f"User {request.user_email} logged out")

        AuditLog.objects.create(
            tenant_id=request.tenant_id,
            user_id=request.user_id,
            user_email=request.user_email,
            action='LOGOUT',
            ip_address=JWTAuthentication._get_client_ip(request)
        )
