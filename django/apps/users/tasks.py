import logging  

from celery import shared_task
from core.celery_base import TenantAwareTask
from users.email import email_service
from users.models import EmailVerification, User, UserInvitation

logger = logging.getLogger(__name__)


@shared_task(bind=True, base=TenantAwareTask)
def send_verification_email_task(
    self, tenant_id, user_id, verification_id, frontend_url
):
    logger.info(f"Starting send_verification_email_task for user_id={user_id}")
    user = User.objects.get(id=user_id)
    verification = EmailVerification.objects.get(id=verification_id)

    email_service.send_verification_email(
        user=user,
        verification_token=verification.token,
        frontend_url=frontend_url,
    )


@shared_task(bind=True, base=TenantAwareTask)
def send_welcome_email_task(self, tenant_id, user_id):
    logger.info(f"Starting send_welcome_email_task for user_id={user_id}")
    user = User.objects.get(id=user_id)
    email_service.send_welcome_email(user)


@shared_task(bind=True, base=TenantAwareTask)
def send_invitation_email_task(self, tenant_id, invitation_id, frontend_url):
    logger.info(
        f"Starting send_invitation_email_task for invitation_id={invitation_id}"
    )
    invitation = UserInvitation.objects.get(id=invitation_id)
    email_service.send_invitation_email(invitation, frontend_url)


@shared_task(bind=True, base=TenantAwareTask)
def send_invitation_reminder_email_task(self, tenant_id, invitation_id, frontend_url):
    logger.info(
        f"Starting send_invitation_reminder_email_task for invitation_id={invitation_id}"
    )
    invitation = UserInvitation.objects.get(id=invitation_id)
    email_service.send_invitation_reminder_email(invitation, frontend_url)


@shared_task(bind=True, base=TenantAwareTask)
def cleanup_expired_invitations_task(self, *args, **kwargs):
    from django.utils import timezone
    from users.models import UserInvitation

    now = timezone.now()
    count = UserInvitation.objects.filter(status="pending", expires_at__lt=now).update(
        status="expired"
    )
    logger.info(f"System-wide cleanup: Marked {count} pending invitations as expired.")
    return count


@shared_task(bind=True, base=TenantAwareTask)
def cleanup_expired_tokens_task(self, *args, **kwargs):
    from django.utils import timezone
    from users.models import MFAVerificationToken, PasswordReset

    now = timezone.now()
    mfa_count = MFAVerificationToken.objects.filter(expires_at__lt=now).delete()[0]
    pwd_count = PasswordReset.objects.filter(expires_at__lt=now).delete()[0]
    logger.info(
        f"System-wide cleanup: Deleted {mfa_count} expired MFA tokens and {pwd_count} expired password resets."
    )
    return {"mfa_deleted": mfa_count, "password_resets_deleted": pwd_count}
