"""
OAuth onboarding handlers for owner signup and engineer invitation flows.
"""
import logging
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from .models import User, OAuthAccount, UserInvitation
from tenants.models import Tenant
from .slug_generator import generate_unique_slug

logger = logging.getLogger(__name__)


class OwnerOAuthHandler:
    """
    Handle OAuth flow for tenant owners.
    
    Creates new tenant + owner account or signs in existing owner.
    """
    
    @staticmethod
    @transaction.atomic
    def process_oauth_signup(user_info):
        """
        Handle owner OAuth signup/signin.
        
        Args:
            user_info: Dict {
                provider_user_id,
                email,
                name,
                picture,
                provider  # 'google' or 'github'
            }
            
        Returns:
            tuple: (user, created_user, created_oauth)
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
            
            # Update last used
            oauth_account.last_used_at = timezone.now()
            oauth_account.save()
            
            logger.info(f"OAuth signin for existing user {email} via {provider}")
            return user, False, False
        
        except OAuthAccount.DoesNotExist:
            pass
        
        # 2. Check if user exists with same email
        try:
            user = User.objects.get(email=email)
            
            # Link OAuth account to existing user
            oauth_account = OAuthAccount.objects.create(
                user=user,
                provider=provider,
                provider_user_id=provider_user_id,
                provider_email=email,
                provider_name=user_info['name'],
                provider_picture_url=user_info['picture']
            )
            
            logger.info(f"Linked {provider} OAuth to existing user {email}")
            return user, False, True
        
        except User.DoesNotExist:
            pass
        
        # 3. Create new owner and tenant
        logger.info(f"Creating new owner and tenant for {email} via {provider}")
        
        # Create tenant with unique slug
        tenant_name = user_info['name'] + "'s Organization"
        slug = generate_unique_slug(tenant_name)
        
        tenant = Tenant.objects.create(
            name=tenant_name,
            slug=slug,
            plan_tier='free',
            status='active'
        )
        
        # Extract first and last name
        name_parts = user_info['name'].split()
        first_name = name_parts[0] if name_parts else ''
        last_name = ' '.join(name_parts[1:]) if len(name_parts) > 1 else ''
        
        # Create owner user
        user = User.objects.create_user(
            email=email,
            password=None,  # No password for OAuth users
            tenant=tenant,
            first_name=first_name,
            last_name=last_name,
            role='owner',
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
        
        logger.info(
            f"Created new owner {email} with tenant '{tenant_name}' "
            f"(slug: {slug}) via {provider} OAuth"
        )
        
        return user, True, True


class EngineerOAuthHandler:
    """
    Handle OAuth flow for engineer invitations.
    
    Adds engineer to invited tenant with specified role.
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
            tuple: (user, created_user, created_oauth)
        """
        email = user_info['email']
        provider = user_info['provider']
        provider_user_id = user_info['provider_user_id']
        tenant = invitation.tenant

        if email.lower() != invitation.email.lower():
            raise ValidationError(
                "This invitation was sent to a different email address."
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
                    'This OAuth account is linked to a different organization.'
                )
            
            # Update last used
            oauth_account.last_used_at = timezone.now()
            oauth_account.save()
            
            logger.info(f"OAuth signin for existing engineer {email} in {tenant.name}")
            return user, False, False
        
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
            return user, False, True
        
        except User.DoesNotExist:
            pass
        
        # 3. Create engineer account in invited tenant
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
        
        return user, True, True