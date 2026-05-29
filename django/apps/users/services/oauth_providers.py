import logging

import requests
from django.conf import settings
from rest_framework.exceptions import AuthenticationFailed

logger = logging.getLogger(__name__)


class GoogleOAuthService:
    """Handle Google OAuth authentication."""

    GOOGLE_OAUTH_URL = "https://oauth2.googleapis.com/token"
    GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"

    @staticmethod
    def get_user_info(access_token):
        """
        Get user info from Google using access token.

        Args:
            access_token: Google OAuth access token

        Returns:
            dict: User info {id, email, name, picture}
        """
        try:
            response = requests.get(
                GoogleOAuthService.GOOGLE_USERINFO_URL,
                params={"access_token": access_token},
                timeout=10,
            )
            response.raise_for_status()
            data = response.json()

            return {
                "provider_user_id": data.get("id"),
                "email": data.get("email"),
                "name": data.get("name"),
                "picture": data.get("picture"),
            }
        except Exception as e:
            logger.error(f"Failed to get Google user info: {str(e)}")
            raise AuthenticationFailed("Failed to authenticate with Google")

    @staticmethod
    def exchange_code_for_token(code):
        """
        Exchange authorization code for access token.

        Args:
            code: Google authorization code from frontend

        Returns:
            str: Access token
        """
        try:
            response = requests.post(
                GoogleOAuthService.GOOGLE_OAUTH_URL,
                data={
                    "client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
                    "client_secret": settings.GOOGLE_OAUTH_CLIENT_SECRET,
                    "code": code,
                    "grant_type": "authorization_code",
                    "redirect_uri": settings.GOOGLE_OAUTH_REDIRECT_URI,
                },
                timeout=10,
            )
            response.raise_for_status()
            data = response.json()

            return data.get("access_token")
        except Exception as e:
            logger.error(f"Failed to exchange Google code for token: {str(e)}")
            raise AuthenticationFailed("Failed to authenticate with Google")


class GitHubOAuthService:
    """Handle GitHub OAuth authentication."""

    GITHUB_OAUTH_URL = "https://github.com/login/oauth/access_token"
    GITHUB_USERINFO_URL = "https://api.github.com/user"

    @staticmethod
    def get_user_info(access_token):
        """
        Get user info from GitHub using access token.

        Args:
            access_token: GitHub OAuth access token

        Returns:
            dict: User info {id, email, name, avatar_url}
        """
        try:
            response = requests.get(
                GitHubOAuthService.GITHUB_USERINFO_URL,
                headers={
                    "Authorization": f"token {access_token}",
                    "Accept": "application/vnd.github.v3+json",
                },
                timeout=10,
            )
            response.raise_for_status()
            data = response.json()

            # GitHub doesn't always return email, fetch separately if needed
            email = data.get("email")
            if not email:
                email = GitHubOAuthService._get_primary_email(access_token)

            return {
                "provider_user_id": str(data.get("id")),
                "email": email or data.get("login") + "@github.com",
                "name": data.get("name") or data.get("login"),
                "picture": data.get("avatar_url"),
            }
        except Exception as e:
            logger.error(f"Failed to get GitHub user info: {str(e)}")
            raise AuthenticationFailed("Failed to authenticate with GitHub")

    @staticmethod
    def _get_primary_email(access_token):
        """Get primary email from GitHub if not in user info."""
        try:
            response = requests.get(
                "https://api.github.com/user/emails",
                headers={
                    "Authorization": f"token {access_token}",
                    "Accept": "application/vnd.github.v3+json",
                },
                timeout=10,
            )
            response.raise_for_status()
            emails = response.json()

            # Get primary email
            for email in emails:
                if email.get("primary"):
                    return email.get("email")

            # Fallback to first verified email
            for email in emails:
                if email.get("verified"):
                    return email.get("email")
        except Exception as e:
            logger.warning(f"Failed to get GitHub primary email: {str(e)}")

        return None

    @staticmethod
    def exchange_code_for_token(code):
        """
        Exchange authorization code for access token.

        Args:
            code: GitHub authorization code from frontend

        Returns:
            str: Access token
        """
        try:
            response = requests.post(
                GitHubOAuthService.GITHUB_OAUTH_URL,
                data={
                    "client_id": settings.GITHUB_OAUTH_CLIENT_ID,
                    "client_secret": settings.GITHUB_OAUTH_CLIENT_SECRET,
                    "code": code,
                },
                headers={"Accept": "application/json"},
                timeout=10,
            )
            response.raise_for_status()
            data = response.json()

            if "error" in data:
                raise Exception(data.get("error_description", "OAuth error"))

            return data.get("access_token")
        except Exception as e:
            logger.error(f"Failed to exchange GitHub code for token: {str(e)}")
            raise AuthenticationFailed("Failed to authenticate with GitHub")
