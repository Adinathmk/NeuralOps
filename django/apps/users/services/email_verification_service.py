import logging

from django.conf import settings

from ..authentication import JWTAuthentication
from ..email import email_service
from ..models import AuditLog, EmailVerification, User

logger = logging.getLogger(__name__)


class EmailVerificationService:

    @staticmethod
    def verify_email(verification_obj, request):
        """
        Completes email verification:
        - Marks the token as verified
        - Sends welcome email
        - Generates JWT tokens for auto-login
        - Writes EMAIL_VERIFIED audit log entry
        Returns (user, access_token, refresh_token).
        """
        # Mark as verified
        verification_obj.verify()
        user = verification_obj.user

        # Send welcome email
        try:
            email_service.send_welcome_email(user)
        except Exception as e:
            logger.warning(f"Failed to send welcome email: {str(e)}")

        # Generate tokens for auto-login
        access_token, refresh_token = JWTAuthentication.generate_tokens(user, request)

        AuditLog.log(
            action="EMAIL_VERIFIED",
            user=user,
            resource_type="EmailVerification",
            resource_id=str(verification_obj.id),
            ip_address=JWTAuthentication._get_client_ip(request),
        )

        logger.info(f"Email verified for user {user.email}")

        return user, access_token, refresh_token

    @staticmethod
    def resend_verification(email):
        """
        Resends the verification email:
        - Looks up user by email (silent fail if not found — security)
        - Silently fails if email is already verified
        - Deletes old tokens, creates a new one, sends the email
        Returns (already_verified: bool)
        """
        try:
            user = User.objects.get(email=email)

            # User exists but already verified
            if user.email_verified:
                return True

            # Always use backend-configured frontend URL
            frontend_url = settings.FRONTEND_URL

            # Delete old tokens
            EmailVerification.objects.filter(user=user).delete()

            # Create new token
            verification = EmailVerification.objects.create(user=user)

            # Send verification email
            try:
                email_service.send_verification_email(
                    user=user,
                    verification_token=verification.token,
                    frontend_url=frontend_url,
                )
            except Exception as e:
                logger.error(f"Failed to send verification email to {email}: {str(e)}")

            logger.info(f"Verification email resent to {email}")

        except User.DoesNotExist:
            # IMPORTANT: Do NOT reveal whether user exists
            logger.warning(
                f"Verification resend requested for non-existent email: {email}"
            )

        return False
