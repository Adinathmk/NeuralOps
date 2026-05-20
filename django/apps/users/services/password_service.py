import logging

from django.conf import settings

from ..authentication import JWTAuthentication
from ..models import User, PasswordReset, UserSession
from ..email import email_service

logger = logging.getLogger(__name__)


class PasswordService:

    @staticmethod
    def forgot_password(email, request):
        """
        Initiates password reset flow:
        - Looks up user by email (silent fail if not found — security)
        - Deletes old reset tokens
        - Creates new PasswordReset token
        - Sends password reset email
        """
        try:
            user = User.objects.get(email=email)

            # Always use backend-configured frontend URL
            frontend_url = settings.FRONTEND_URL

            # Delete old reset tokens
            PasswordReset.objects.filter(user=user).delete()

            # Get client IP
            ip_address = JWTAuthentication._get_client_ip(request)

            # Create new reset token
            reset = PasswordReset.objects.create(
                user=user,
                ip_address=ip_address
            )

            # Send password reset email
            try:
                email_service.send_password_reset_email(
                    user=user,
                    reset_token=reset.token,
                    frontend_url=frontend_url
                )
            except Exception as e:
                logger.error(
                    f"Failed to send password reset email to {email}: {str(e)}"
                )

            logger.info(f"Password reset requested for {email}")

        except User.DoesNotExist:
            # IMPORTANT: Do NOT reveal whether email exists
            logger.warning(
                f"Password reset requested for non-existent email: {email}"
            )

    @staticmethod
    def reset_password(reset_obj, new_password):
        """
        Completes password reset:
        - Updates user's password
        - Marks token as used
        - Deletes all active sessions (forces re-login)
        - Sends password changed notification email
        """
        user = reset_obj.user

        # Update password
        user.set_password(new_password)
        user.save()

        # Mark token as used
        reset_obj.use()

        # Revoke all sessions (force re-login)
        UserSession.objects.filter(user=user).delete()

        # Send notification email
        try:
            email_service.send_password_changed_notification(user)
        except Exception as e:
            logger.warning(f"Failed to send password changed email: {str(e)}")

        logger.info(f"Password reset for user {user.email}")

    @staticmethod
    def change_password(user, current_password, new_password):
        """
        Changes password for an authenticated user:
        - Verifies current password is correct
        - Updates to new password
        - Deletes all active sessions (forces re-login on all devices)
        - Sends password changed notification email
        Returns (success: bool, error_message: str | None)
        """
        # Verify current password
        if not user.check_password(current_password):
            return False, 'Current password is incorrect'

        # Update password
        user.set_password(new_password)
        user.save()

        # Revoke all sessions (force re-login on all devices)
        UserSession.objects.filter(user=user).delete()

        # Send notification email
        try:
            email_service.send_password_changed_notification(user)
        except Exception as e:
            logger.warning(f"Failed to send password changed email: {str(e)}")

        logger.info(f"Password changed for user {user.email}")

        return True, None
