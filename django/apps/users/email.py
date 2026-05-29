import logging

from django.conf import settings
from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.utils.html import strip_tags

logger = logging.getLogger(__name__)


class EmailService:
    """Send emails via AWS SES."""

    @staticmethod
    def send_verification_email(user, verification_token, frontend_url):
        """
        Send email verification link.

        Args:
            user: User instance
            verification_token: Token from EmailVerification model
            frontend_url: Base URL of frontend (e.g., https://app.neuralops.com)
        """
        verification_link = f"{frontend_url}/verify-email?token={verification_token}"

        context = {
            "user_name": user.get_full_name(),
            "verification_link": verification_link,
            "expiry_hours": 24,
        }

        # Load HTML template
        html_message = render_to_string("emails/verify_email.html", context)
        plain_message = strip_tags(html_message)

        try:
            send_mail(
                subject="Verify your email - NeuralOps",
                message=plain_message,
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[user.email],
                html_message=html_message,
                fail_silently=False,
            )
            logger.info(f"Verification email sent to {user.email}")
        except Exception as e:
            logger.error(f"Failed to send verification email to {user.email}: {str(e)}")
            raise

    @staticmethod
    def send_welcome_email(user):
        """Send welcome email after email verification."""
        context = {
            "user_name": user.get_full_name(),
            "tenant_name": user.tenant.name if user.tenant else "NeuralOps",
        }

        html_message = render_to_string("emails/welcome.html", context)
        plain_message = strip_tags(html_message)

        try:
            send_mail(
                subject="Welcome to NeuralOps!",
                message=plain_message,
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[user.email],
                html_message=html_message,
                fail_silently=False,
            )
            logger.info(f"Welcome email sent to {user.email}")
        except Exception as e:
            logger.error(f"Failed to send welcome email to {user.email}: {str(e)}")
            raise

    @staticmethod
    def send_password_reset_email(user, reset_token, frontend_url):
        """
        Send password reset link.

        Args:
            user: User instance
            reset_token: Token from PasswordReset model
            frontend_url: Base URL of frontend
        """
        reset_link = f"{frontend_url}/reset-password?token={reset_token}"

        context = {
            "user_name": user.get_full_name(),
            "reset_link": reset_link,
            "expiry_hours": 24,
        }

        html_message = render_to_string("emails/reset_password.html", context)
        plain_message = strip_tags(html_message)

        try:
            send_mail(
                subject="Reset your password - NeuralOps",
                message=plain_message,
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[user.email],
                html_message=html_message,
                fail_silently=False,
            )
            logger.info(f"Password reset email sent to {user.email}")
        except Exception as e:
            logger.error(
                f"Failed to send password reset email to {user.email}: {str(e)}"
            )
            raise

    @staticmethod
    def send_password_changed_notification(user):
        """Send notification email after password change."""
        context = {
            "user_name": user.get_full_name(),
        }

        html_message = render_to_string("emails/password_changed.html", context)
        plain_message = strip_tags(html_message)

        try:
            send_mail(
                subject="Your password has been changed - NeuralOps",
                message=plain_message,
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[user.email],
                html_message=html_message,
                fail_silently=False,
            )
            logger.info(f"Password changed notification sent to {user.email}")
        except Exception as e:
            logger.error(
                f"Failed to send password changed email to {user.email}: {str(e)}"
            )

    @staticmethod
    def send_invitation_email(invitation, frontend_url):
        """
        Send invitation email to engineer.

        Args:
            invitation: UserInvitation instance
            frontend_url: Base URL of frontend
        """
        join_link = f"{frontend_url}/join?token={invitation.token}"

        context = {
            "engineer_email": invitation.email,
            "tenant_name": invitation.tenant.name,
            "invited_by_name": (
                invitation.invited_by.get_full_name()
                if invitation.invited_by
                else "Admin"
            ),
            "join_link": join_link,
            "role": invitation.role,
            "expiry_days": 7,
        }

        html_message = render_to_string("emails/invitation.html", context)
        plain_message = strip_tags(html_message)

        try:
            send_mail(
                subject=f"Join {invitation.tenant.name} on NeuralOps",
                message=plain_message,
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[invitation.email],
                html_message=html_message,
                fail_silently=False,
            )

            # Track email sent
            invitation.mark_email_sent()

            logger.info(f"Invitation email sent to {invitation.email}")
        except Exception as e:
            logger.error(f"Failed to send invitation email: {str(e)}")
            raise

    @staticmethod
    def send_invitation_reminder_email(invitation, frontend_url):
        """Send reminder email for pending invitation."""
        join_link = f"{frontend_url}/join?token={invitation.token}"

        context = {
            "engineer_email": invitation.email,
            "tenant_name": invitation.tenant.name,
            "join_link": join_link,
            "expiry_days": 7,
        }

        html_message = render_to_string("emails/invitation_reminder.html", context)
        plain_message = strip_tags(html_message)

        try:
            send_mail(
                subject=f"Reminder: Join {invitation.tenant.name} on NeuralOps",
                message=plain_message,
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[invitation.email],
                html_message=html_message,
                fail_silently=False,
            )

            invitation.increment_resend_count()
            logger.info(f"Invitation reminder sent to {invitation.email}")
        except Exception as e:
            logger.error(f"Failed to send invitation reminder: {str(e)}")
            raise

    @staticmethod
    def send_team_member_joined_notification(invitation):
        """Notify admin when engineer joins."""
        context = {
            "admin_name": (
                invitation.invited_by.get_full_name()
                if invitation.invited_by
                else "Admin"
            ),
            "engineer_email": invitation.email,
            "engineer_name": invitation.accepted_by.get_full_name(),
            "role": invitation.accepted_by.role,
            "tenant_name": invitation.tenant.name,
        }

        html_message = render_to_string("emails/team_member_joined.html", context)
        plain_message = strip_tags(html_message)

        try:
            send_mail(
                subject=f"{invitation.accepted_by.get_full_name()} joined {invitation.tenant.name}",
                message=plain_message,
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=(
                    [invitation.invited_by.email] if invitation.invited_by else []
                ),
                html_message=html_message,
                fail_silently=False,
            )
            logger.info(f"Team member joined notification sent to admin")
        except Exception as e:
            logger.warning(f"Failed to send team member joined notification: {str(e)}")


email_service = EmailService()
