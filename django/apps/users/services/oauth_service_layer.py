import logging

from rest_framework.exceptions import ValidationError

from ..authentication import JWTAuthentication
from ..models import User, MFAVerificationToken, TOTPDevice
from .oauth_providers import GoogleOAuthService, GitHubOAuthService
from .oauth_handlers import EngineerOAuthHandler, OwnerOAuthHandler
from core.utils.errors import extract_error_message

logger = logging.getLogger(__name__)


class OAuthServiceLayer:
    """
    Routes OAuth provider callbacks (Google / GitHub) to the correct
    handler (OwnerOAuthHandler / EngineerOAuthHandler), checks if MFA is
    required, and generates JWT tokens.
    """

    @staticmethod
    def _finalise_oauth_login(user, request):
        """
        Shared post-authentication step for both providers:
        - If MFA is enabled: create MFA token, return mfa payload dict.
        - If MFA disabled: generate JWT tokens, return token payload dict.
        """
        try:
            TOTPDevice.objects.get(user=user, is_confirmed=True)

            # MFA is enabled — return temporary MFA token
            mfa_token_obj = MFAVerificationToken.objects.create(user=user)
            logger.info(f"MFA verification required for {user.email}")

            return {
                'requires_mfa': True,
                'mfa_token': mfa_token_obj.token,
            }

        except TOTPDevice.DoesNotExist:
            # MFA not enabled — return access tokens directly
            access_token, refresh_token = JWTAuthentication.generate_tokens(user, request)
            return {
                'requires_mfa': False,
                'user': user,
                'access_token': access_token,
                'refresh_token': refresh_token,
            }

    @staticmethod
    def handle_google_oauth(access_token, invitation, request):
        """
        Processes a Google OAuth callback:
        - Gets user info from Google
        - Routes to EngineerOAuthHandler (with invite) or OwnerOAuthHandler (without)
        - For owner-less accounts raises ValidationError (no sign-up without email)
        - Returns result dict from _finalise_oauth_login plus log_message
        Raises ValidationError or generic Exception on failure.
        """
        # Get user info from Google
        user_info = GoogleOAuthService.get_user_info(access_token)
        user_info['provider'] = 'google'
        email = user_info['email']

        # Route to appropriate handler
        if invitation:
            # Engineer joining via invitation
            logger.info(f"Engineer OAuth signup via invitation - {user_info['email']}")
            user = EngineerOAuthHandler.process_oauth_invitation(user_info, invitation)
            log_message = (
                f"Engineer {user.email} joined {invitation.tenant.name} "
                f"via Google OAuth"
            )

        else:
            try:
                existing_user = User.objects.get(email=email)
            except User.DoesNotExist:
                raise ValidationError(
                    "No account found with this email. "
                    "Please contact your administrator."
                )

            if existing_user.role == 'owner':
                logger.info(f"Owner OAuth login attempt - {email}")
                user = OwnerOAuthHandler.process_oauth_login(user_info)
                log_message = f"Owner {user.email} signed in via Google OAuth"
            else:
                logger.info(f"Engineer OAuth login attempt - {email}")
                user = EngineerOAuthHandler.process_oauth_login(user_info)
                log_message = (
                    f"Engineer {user.email} signed in "
                    f"via Google OAuth"
                )

        result = OAuthServiceLayer._finalise_oauth_login(user, request)
        result['log_message'] = log_message
        return result

    @staticmethod
    def handle_github_oauth(access_token, invitation, request):
        """
        Processes a GitHub OAuth callback:
        - Gets user info from GitHub
        - Routes to EngineerOAuthHandler (with invite) or OwnerOAuthHandler (without)
        - Returns result dict from _finalise_oauth_login plus log_message
        Raises ValidationError or generic Exception on failure.
        """
        # Get user info from GitHub
        user_info = GitHubOAuthService.get_user_info(access_token)
        user_info['provider'] = 'github'

        # Route to appropriate handler
        if invitation:
            # Engineer joining via invitation
            logger.info(f"Engineer OAuth signup via invitation - {user_info['email']}")
            user = EngineerOAuthHandler.process_oauth_invitation(user_info, invitation)
            log_message = (
                f"Engineer {user.email} joined {invitation.tenant.name} "
                f"via GitHub OAuth"
            )
        else:
            # No invitation → Try owner login (signup is BLOCKED)
            logger.info(f"Owner OAuth login attempt - {user_info['email']}")
            user = OwnerOAuthHandler.process_oauth_login(user_info)
            log_message = f"Owner {user.email} signed in via GitHub OAuth"

        result = OAuthServiceLayer._finalise_oauth_login(user, request)
        result['log_message'] = log_message
        return result
