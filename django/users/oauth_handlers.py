"""
FIXED OAuth onboarding handlers:
- Owner OAuth signup: BLOCKED (no tenant creation)
- Owner OAuth login: ALLOWED (existing account only)
- Owner OAuth linking: ALLOWED
- Engineer OAuth signup: ALLOWED (via invitation only)
- Engineer OAuth login: ALLOWED
- Engineer OAuth linking: ALLOWED
"""
import logging
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from .models import User, OAuthAccount, UserInvitation
from tenants.models import Tenant

logger = logging.getLogger(__name__)


class OwnerOAuthHandler:
    """
    Handle OAuth flow for tenant owners.
    
    IMPORTANT: Owners CANNOT sign up via OAuth.
    - New owner signup: BLOCKED → return error
    - Existing owner login: ALLOWED
    - Existing owner OAuth linking: ALLOWED
    """
    
    @staticmethod
    @transaction.atomic
    def process_oauth_login(user_info):
        """
        Handle owner OAuth login/linking.
        
        Does NOT create new tenants or users.
        Only allows:
        1. Signing in with existing OAuth account
        2. Linking OAuth to existing email account
        
        Args:
            user_info: Dict {
                provider_user_id,
                email,
                name,
                picture,
                provider  # 'google' or 'github'
            }
            
        Returns:
            user: User instance (or raises ValidationError if no account)
            
        Raises:
            ValidationError: If no account exists with this email
        """
        email = user_info['email']
        provider = user_info['provider']
        provider_user_id = user_info['provider_user_id']
        
        # 1. Check if OAuth account already exists
        try:
            oauth_account = OAuthAccount.objects.get(
                provider=provider,
                provider_user_id=provider_user_id
            )
            user = oauth_account.user
            
            # Verify this is a tenant owner
            if not user.is_tenant_owner():
                raise ValidationError(
                    'This OAuth account is not linked to a tenant owner. '
                    'Please use email/password or invitation link to sign in.'
                )
            
            # Update last used
            oauth_account.last_used_at = timezone.now()
            oauth_account.save()
            
            logger.info(f"OAuth signin for existing owner {email} via {provider}")
            return user
        
        except OAuthAccount.DoesNotExist:
            pass
        
        # 2. Check if user exists with same email
        try:
            user = User.objects.get(email=email)
            
            # Verify this is a tenant owner
            if not user.is_tenant_owner():
                raise ValidationError(
                    'No owner account exists with this email. '
                    'Please sign up with email/password or use your invitation link.'
                )
            
            # Link OAuth account to existing owner
            oauth_account = OAuthAccount.objects.create(
                user=user,
                provider=provider,
                provider_user_id=provider_user_id,
                provider_email=email,
                provider_name=user_info['name'],
                provider_picture_url=user_info['picture']
            )
            
            logger.info(f"Linked {provider} OAuth to existing owner {email}")
            return user
        
        except User.DoesNotExist:
            pass
        
        # 3. No account exists → BLOCK signup
        logger.warning(
            f"OAuth signup attempt for new owner {email} via {provider} - BLOCKED"
        )
        
        raise ValidationError(
            f'No account found for {email}. '
            f'Please sign up with email and password to create your account and organization.'
        )


class EngineerOAuthHandler:
    """
    Handle OAuth flow for engineer invitations.
    
    Engineers CAN sign up via OAuth (through invitation).
    - New engineer signup: ALLOWED (with valid invitation)
    - Existing engineer login: ALLOWED
    - Existing engineer OAuth linking: ALLOWED
    """
    
    @staticmethod
    @transaction.atomic
    def process_oauth_invitation(user_info, invitation):
        """
        Handle engineer OAuth join via invitation.
        
        Args:
            user_info: Dict {
                provider_user_id,
                email,
                name,
                picture,
                provider  # 'google' or 'github'
            }
            invitation: UserInvitation instance
            
        Returns:
            user: User instance
            
        Raises:
            ValidationError: If email doesn't match or other issues
        """
        email = user_info['email']
        provider = user_info['provider']
        provider_user_id = user_info['provider_user_id']
        tenant = invitation.tenant
        
        # CRITICAL: Email must match invitation
        if email.lower() != invitation.email.lower():
            raise ValidationError(
                f'This invitation was sent to {invitation.email}, '
                f'but you signed in with {email}. '
                f'Please use the email this invitation was sent to.'
            )
        
        # 1. Check if OAuth account already exists
        try:
            oauth_account = OAuthAccount.objects.get(
                provider=provider,
                provider_user_id=provider_user_id
            )
            user = oauth_account.user
            
            # Verify user is in correct tenant
            if user.tenant_id != tenant.id:
                raise ValidationError(
                    'This OAuth account is linked to a different organization. '
                    'Please contact support.'
                )
            
            # Update last used
            oauth_account.last_used_at = timezone.now()
            oauth_account.save()
            
            logger.info(f"OAuth signin for existing engineer {email} in {tenant.name}")
            return user
        
        except OAuthAccount.DoesNotExist:
            pass
        
        # 2. Check if user already exists in this tenant
        try:
            user = User.objects.get(email=email, tenant=tenant)
            
            # Link OAuth account
            oauth_account = OAuthAccount.objects.create(
                user=user,
                provider=provider,
                provider_user_id=provider_user_id,
                provider_email=email,
                provider_name=user_info['name'],
                provider_picture_url=user_info['picture']
            )
            
            logger.info(f"Linked {provider} OAuth to existing engineer {email}")
            return user
        
        except User.DoesNotExist:
            pass
        
        # 3. Create NEW engineer account in invited tenant
        logger.info(
            f"Creating new engineer {email} in tenant '{tenant.name}' via {provider}"
        )
        
        # Extract name
        name_parts = user_info['name'].split()
        first_name = name_parts[0] if name_parts else ''
        last_name = ' '.join(name_parts[1:]) if len(name_parts) > 1 else ''
        
        # Create user with invited tenant and role
        user = User.objects.create_user(
            email=email,
            password=None,  # No password for OAuth users
            tenant=tenant,
            first_name=first_name,
            last_name=last_name,
            role=invitation.role,
            is_staff=False,
            email_verified=True  # OAuth emails are verified
        )
        
        # Create OAuth account
        oauth_account = OAuthAccount.objects.create(
            user=user,
            provider=provider,
            provider_user_id=provider_user_id,
            provider_email=email,
            provider_name=user_info['name'],
            provider_picture_url=user_info['picture']
        )
        
        # Mark invitation as accepted
        invitation.accepted_by = user
        invitation.status = 'accepted'
        invitation.save()
        
        logger.info(
            f"Engineer {email} joined {tenant.name} "
            f"as {invitation.role} via {provider} OAuth"
        )
        
        return user