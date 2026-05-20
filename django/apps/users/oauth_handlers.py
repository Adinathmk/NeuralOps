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

            if not user.email_verified:
                user.email_verified = True
                user.save(update_fields=['email_verified'])


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
    Handle OAuth flow for engineers.

    Engineers CAN:
    - Sign up via invitation
    - Login without invitation
    - Link OAuth to existing account
    """

    # =========================================================
    # ENGINEER LOGIN (NO INVITATION)
    # =========================================================
    @staticmethod
    @transaction.atomic
    def process_oauth_login(user_info):
        

        email = user_info['email']
        provider = user_info['provider']
        provider_user_id = user_info['provider_user_id']

        # -----------------------------------------------------
        # 1. Existing OAuth account
        # -----------------------------------------------------
        try:
            oauth_account = OAuthAccount.objects.get(
                provider=provider,
                provider_user_id=provider_user_id
            )

            user = oauth_account.user

            # Prevent owner login here
            if user.role == 'owner':
                raise ValidationError(
                    'This account belongs to an owner account.'
                )

            oauth_account.last_used_at = timezone.now()
            oauth_account.save()

            logger.info(
                f"OAuth signin for existing engineer "
                f"{email} via {provider}"
            )

            return user

        except OAuthAccount.DoesNotExist:
            pass

        # -----------------------------------------------------
        # 2. Existing engineer account by email
        # -----------------------------------------------------
        try:
            user = User.objects.get(email=email)

            # Prevent owner login here
            if user.role == 'owner':
                raise ValidationError(
                    'This account belongs to an owner account.'
                )

            # Check if provider already linked
            existing_link = OAuthAccount.objects.filter(
                user=user,
                provider=provider
            ).first()

            if not existing_link:

                OAuthAccount.objects.create(
                    user=user,
                    provider=provider,
                    provider_user_id=provider_user_id,
                    provider_email=email,
                    provider_name=user_info['name'],
                    provider_picture_url=user_info['picture']
                )

                logger.info(
                    f"Linked {provider} OAuth "
                    f"to existing engineer {email}"
                )
            
            if not user.email_verified:
                user.email_verified = True
                user.save(update_fields=['email_verified'])

            return user

        except User.DoesNotExist:
            raise ValidationError(
                'No engineer account found. '
                'Please accept your invitation first.'
            )

    # =========================================================
    # ENGINEER SIGNUP VIA INVITATION
    # =========================================================
    @staticmethod
    @transaction.atomic
    def process_oauth_invitation(user_info, invitation):
        print('1')
        """
        Handle engineer OAuth join via invitation.

        Args:
            user_info: Dict {
                provider_user_id,
                email,
                name,
                picture,
                provider
            }

            invitation: UserInvitation instance

        Returns:
            user: User instance
        """

        email = user_info['email']
        provider = user_info['provider']
        provider_user_id = user_info['provider_user_id']
        tenant = invitation.tenant

        # -----------------------------------------------------
        # Email MUST match invitation
        # -----------------------------------------------------
        if email.lower() != invitation.email.lower():

            raise ValidationError(
                f'This invitation was sent to '
                f'{invitation.email}, but you signed in '
                f'with {email}.'
            )
        print('2')

        # # -----------------------------------------------------
        # # 1. Existing OAuth account
        # # -----------------------------------------------------
        # try:

        #     oauth_account = OAuthAccount.objects.get(
        #         provider=provider,
        #         provider_user_id=provider_user_id
        #     )

        #     user = oauth_account.user

        #     # Verify tenant
        #     if user.tenant_id != tenant.id:

        #         raise ValidationError(
        #             'This OAuth account is linked '
        #             'to a different organization.'
        #         )

        #     oauth_account.last_used_at = timezone.now()
        #     oauth_account.save()

        #     logger.info(
        #         f"OAuth signin for existing engineer "
        #         f"{email} in {tenant.name}"
        #     )

        #     return user

        # except OAuthAccount.DoesNotExist:
        #     pass

        # # -----------------------------------------------------
        # # 2. Existing user in tenant
        # # -----------------------------------------------------
        # try:

        #     user = User.objects.get(
        #         email=email,
        #         tenant=tenant
        #     )

        #     # Check if provider already linked
        #     existing_link = OAuthAccount.objects.filter(
        #         user=user,
        #         provider=provider
        #     ).first()

        #     if not existing_link:

        #         OAuthAccount.objects.create(
        #             user=user,
        #             provider=provider,
        #             provider_user_id=provider_user_id,
        #             provider_email=email,
        #             provider_name=user_info['name'],
        #             provider_picture_url=user_info['picture']
        #         )

        #     logger.info(
        #         f"Linked {provider} OAuth "
        #         f"to existing engineer {email}"
        #     )

        #     return user

        # except User.DoesNotExist:
        #     pass

        # -----------------------------------------------------
        # 3. Create NEW engineer account
        # -----------------------------------------------------
        logger.info(
            f"Creating new engineer {email} "
            f"in tenant '{tenant.name}' via {provider}"
        )

        # Extract name
        name_parts = user_info['name'].split()

        first_name = (
            name_parts[0]
            if name_parts else ''
        )
        print('3')
        last_name = (
            ' '.join(name_parts[1:])
            if len(name_parts) > 1 else ''
        )

        print('3.5')
        # Create user
        user = User.objects.create_user(
            email=email,
            password=None,
            tenant=tenant,
            first_name=first_name,
            last_name=last_name,
            role=invitation.role,
            is_staff=False,
            email_verified=True
        )
        print('4')
        # Create OAuth account
        OAuthAccount.objects.create(
            user=user,
            provider=provider,
            provider_user_id=provider_user_id,
            provider_email=email,
            provider_name=user_info['name'],
            provider_picture_url=user_info['picture']
        )
        print('4.5')
        # Mark invitation accepted
        invitation.accepted_by = user

        print('4.6')
        invitation.status = 'accepted'
        print('4.6')
        invitation.save()
        print('5')

        logger.info(
            f"Engineer {email} joined "
            f"{tenant.name} as {invitation.role} "
            f"via {provider} OAuth"
        )
        print('6')
       

        return user