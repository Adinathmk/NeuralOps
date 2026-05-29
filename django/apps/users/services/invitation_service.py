import logging
from datetime import timedelta

from core.exceptions import NotFoundException
from core.quotas import QuotaService
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from tenants.models import Tenant

from ..authentication import JWTAuthentication
from ..email import email_service
from ..models import AuditLog, User, UserInvitation
from ..serializers import UserSerializer

logger = logging.getLogger(__name__)


class InvitationService:

    @staticmethod
    def send_invitation(tenant_id, inviter, email, role, frontend_url):
        """
        Sends an invitation to a new engineer:
        - Checks user quota for the tenant
        - Checks if user already exists in the tenant
        - get_or_creates the UserInvitation record (resets if expired)
        - Sends the invitation email
        Returns the invitation object.
        Raises NotFoundException, QuotaExceeded, or email-related errors.
        """
        try:
            tenant = Tenant.objects.get(id=tenant_id)
        except Tenant.DoesNotExist:
            raise NotFoundException("Tenant not found")

        QuotaService.check_user_limit(tenant)

        try:
            # Check if user already exists in tenant
            User.objects.get(email=email, tenant=tenant)
            return None, f"User {email} already exists in {tenant.name}"
        except User.DoesNotExist:
            pass

        # Create or update invitation
        invitation, created = UserInvitation.objects.get_or_create(
            tenant=tenant,
            email=email,
            status="pending",
            defaults={
                "invited_by": inviter,
                "role": role,
            },
        )

        # If invitation already existed but was expired, reset it
        if not created and invitation.status == "expired":
            invitation.status = "pending"
            invitation.expires_at = timezone.now() + timedelta(days=7)
            invitation.invited_by = inviter
            invitation.role = role
            invitation.save()

        # Send invitation email
        try:
            email_service.send_invitation_email(invitation, frontend_url)
        except Exception as e:
            logger.error(f"Failed to send invitation email: {str(e)}")
            return None, "email_failed"

        logger.info(f"Admin {inviter.email} invited {email} to {tenant.name} as {role}")

        AuditLog.log(
            action="USER_INVITED",
            user=inviter,
            resource_type="UserInvitation",
            resource_id=str(invitation.id),
            description=f"Invited {email} as {role} to {tenant.name}",
        )

        return invitation, None

    @staticmethod
    def validate_invitation(token):
        """
        Validates an invitation token.
        Returns (invitation, error_code) where error_code is None on success.
        """
        if not token:
            return None, "missing_token"

        try:
            invitation = UserInvitation.objects.get(token=token)
        except UserInvitation.DoesNotExist:
            return None, "not_found"

        if not invitation.is_valid():
            return None, "expired"

        return invitation, None

    @staticmethod
    def join_with_password(invitation, password, first_name, last_name, request):
        """
        Creates a new engineer account from an invitation:
        - Creates the user inside a transaction
        - Marks the invitation as accepted
        - Sends admin notification
        - Generates JWT tokens
        Returns (user, access_token, refresh_token).
        """
        with transaction.atomic():
            # Create user in invited tenant
            user = User.objects.create_user(
                email=invitation.email,
                password=password,
                tenant=invitation.tenant,
                first_name=first_name,
                last_name=last_name,
                role=invitation.role,
                is_staff=False,
                email_verified=True,  # Email is verified via invitation
            )

            # Mark invitation as accepted
            invitation.accept(user)

            # Send notification to admin
            try:
                email_service.send_team_member_joined_notification(invitation)
            except Exception as e:
                logger.warning(f"Failed to send joined notification: {str(e)}")

            # Generate tokens
            access_token, refresh_token = JWTAuthentication.generate_tokens(
                user, request
            )

            logger.info(
                f"Engineer {user.email} joined {invitation.tenant.name} "
                f"as {user.role} via email/password"
            )

            AuditLog.log(
                action="USER_CREATED",
                user=user,
                description=(
                    f"Engineer joined tenant '{invitation.tenant.name}' "
                    f"as {user.role} via invitation"
                ),
                ip_address=JWTAuthentication._get_client_ip(request),
            )

            return user, access_token, refresh_token

    @staticmethod
    def list_invitations(tenant_id, status):
        """
        Returns a serialized list of invitations for the tenant
        filtered by status, ordered by most recent first.
        """
        invitations = UserInvitation.objects.filter(
            tenant_id=tenant_id, status=status
        ).order_by("-created_at")

        return [
            {
                "id": str(inv.id),
                "email": inv.email,
                "role": inv.role,
                "status": inv.status,
                "invited_by": inv.invited_by.email if inv.invited_by else None,
                "created_at": inv.created_at.isoformat(),
                "expires_at": inv.expires_at.isoformat(),
                "accepted_at": inv.accepted_at.isoformat() if inv.accepted_at else None,
            }
            for inv in invitations
        ]

    @staticmethod
    def cancel_invitation(invitation_id, tenant_id):
        """
        Cancels a pending invitation (scoped to the requesting tenant).
        Returns (invitation, error_message) where error_message is None on success.
        Raises NotFoundException if invitation does not exist.
        """
        try:
            invitation = UserInvitation.objects.get(
                id=invitation_id, tenant_id=tenant_id
            )
        except UserInvitation.DoesNotExist:
            raise NotFoundException("Invitation not found")

        if invitation.status != "pending":
            return None, f"Cannot cancel invitation with status: {invitation.status}"

        invitation.cancel()
        logger.info(f"Invitation {invitation_id} cancelled")

        AuditLog.log(
            action="USER_INVITE_CANCELLED",
            tenant=invitation.tenant,
            user_email=invitation.email,
            resource_type="UserInvitation",
            resource_id=str(invitation.id),
            description=f"Invitation to {invitation.email} cancelled",
        )

        return invitation, None

    @staticmethod
    def resend_invitation(invitation_id, tenant_id, frontend_url):
        """
        Resends an invitation email:
        - Refreshes expiry if invitation has expired
        - Sends the reminder email
        Returns (success: bool, error_message: str | None).
        Raises NotFoundException if invitation does not exist.
        """
        try:
            invitation = UserInvitation.objects.get(
                id=invitation_id, tenant_id=tenant_id
            )
        except UserInvitation.DoesNotExist:
            raise NotFoundException("Invitation not found")

        if invitation.status != "pending":
            return False, f"Cannot resend invitation with status: {invitation.status}"

        # Check if invitation expired — reset expiration
        if not invitation.is_valid():
            invitation.expires_at = timezone.now() + timedelta(days=7)
            invitation.save()

        try:
            email_service.send_invitation_reminder_email(invitation, frontend_url)
        except Exception as e:
            logger.error(f"Failed to resend invitation: {str(e)}")
            return False, "Failed to resend invitation"

        logger.info(f"Invitation {invitation_id} resent to {invitation.email}")

        AuditLog.log(
            action="USER_INVITE_RESENT",
            tenant=invitation.tenant,
            user_email=invitation.email,
            resource_type="UserInvitation",
            resource_id=str(invitation.id),
            description=f"Invitation resent to {invitation.email}",
        )

        return True, None
