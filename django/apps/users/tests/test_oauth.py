"""
Tests for OAuth authentication (Google and GitHub).
Location: backend/django/users/tests/test_oauth.py
"""
import pytest
from rest_framework import status
from unittest.mock import patch, MagicMock
from django.contrib.auth import get_user_model

User = get_user_model()


@pytest.mark.django_db
class TestGoogleOAuthCallback:
    """Test Google OAuth callback."""
    
    @patch('users.oauth_service.GoogleOAuthService.exchange_code_for_token')
    @patch('users.oauth_service.GoogleOAuthService.get_user_info')
    def test_owner_signup_new_google_account(self, mock_get_info, mock_exchange, api_client):
        """Test owner signup with new Google account."""
        # Mock OAuth service responses
        mock_exchange.return_value = 'google-access-token-123'
        mock_get_info.return_value = {
            'provider_user_id': 'google-user-123',
            'email': 'owner@gmail.com',
            'name': 'John Owner',
            'picture': 'https://example.com/photo.jpg',
        }
        
        data = {
            'code': 'google-auth-code-xyz',
        }
        
        response = api_client.post('/api/auth/google/callback', data, format='json')
        
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.data['code'] == 'auth_error'
        assert "No account found" in response.data["message"]
    
    @patch('users.oauth_service.GoogleOAuthService.exchange_code_for_token')
    @patch('users.oauth_service.GoogleOAuthService.get_user_info')
    def test_owner_signin_existing_oauth(self, mock_get_info, mock_exchange, api_client, google_oauth_account):
        """Test owner signin with existing OAuth account."""
        user = google_oauth_account.user
        
        mock_exchange.return_value = 'google-access-token-123'
        mock_get_info.return_value = {
            'provider_user_id': google_oauth_account.provider_user_id,
            'email': user.email,
            'name': user.get_full_name(),
            'picture': 'https://example.com/photo.jpg',
        }
        
        data = {
            'code': 'google-auth-code-xyz',
        }
        
        response = api_client.post('/api/auth/google/callback', data, format='json')
        
        assert response.status_code == status.HTTP_200_OK
        assert response.data['data']['email'] == user.email
    
    @patch('users.oauth_service.GoogleOAuthService.exchange_code_for_token')
    @patch('users.oauth_service.GoogleOAuthService.get_user_info')
    def test_engineer_join_via_google_invitation(self, mock_get_info, mock_exchange, api_client, invitation):
        """Test engineer joining via Google OAuth with invitation."""
        mock_exchange.return_value = 'google-access-token-123'
        mock_get_info.return_value = {
            'provider_user_id': 'google-engineer-456',
            'email': invitation.email,
            'name': 'Bob Engineer',
            'picture': 'https://example.com/photo.jpg',
        }
        
        data = {
            'code': 'google-auth-code-xyz',
            'invite_token': invitation.token,
        }
        
        response = api_client.post('/api/auth/google/callback', data, format='json')
        
        assert response.status_code == status.HTTP_200_OK
        
        # Verify user created in invited tenant
        user = User.objects.get(email=invitation.email)
        assert user.tenant == invitation.tenant
        assert user.role == invitation.role
        
        # Verify invitation accepted
        invitation.refresh_from_db()
        assert invitation.status == 'accepted'
    
    @patch('users.oauth_service.GoogleOAuthService.exchange_code_for_token')
    def test_google_oauth_invalid_code(self, mock_exchange, api_client):
        """Test Google OAuth fails with invalid code."""
        mock_exchange.side_effect = Exception('Invalid code')
        
        data = {
            'code': 'invalid-code',
        }
        
        response = api_client.post('/api/auth/google/callback', data, format='json')
        
        assert response.status_code == status.HTTP_400_BAD_REQUEST
    
    def test_google_oauth_invalid_invite_token(self, api_client):
        """Test Google OAuth fails with invalid invite token."""
        data = {
            'code': 'google-code',
            'invite_token': 'invalid-invite-token',
        }
        
        response = api_client.post('/api/auth/google/callback', data, format='json')
        
        assert response.status_code == status.HTTP_400_BAD_REQUEST


@pytest.mark.django_db
class TestGitHubOAuthCallback:
    """Test GitHub OAuth callback."""
    
    @patch('users.oauth_service.GitHubOAuthService.exchange_code_for_token')
    @patch('users.oauth_service.GitHubOAuthService.get_user_info')
    def test_owner_signup_new_github_account(self, mock_get_info, mock_exchange, api_client):
        """Test owner signup with new GitHub account."""
        mock_exchange.return_value = 'github-access-token-123'
        mock_get_info.return_value = {
            'provider_user_id': 'github-user-789',
            'email': 'owner@github.com',
            'name': 'Jane Owner',
            'picture': 'https://github.com/photo.jpg',
        }
        
        data = {
            'code': 'github-auth-code-xyz',
        }
        
        response = api_client.post('/api/auth/github/callback', data, format='json')
        
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.data['code'] == 'auth_error'
        assert "No account found" in response.data["message"]
    
    @patch('users.oauth_service.GitHubOAuthService.exchange_code_for_token')
    @patch('users.oauth_service.GitHubOAuthService.get_user_info')
    def test_owner_signin_existing_oauth(self, mock_get_info, mock_exchange, api_client, owner_user):
        """Test owner signin with existing OAuth account."""
        from users.models import OAuthAccount
        github_account = OAuthAccount.objects.create(
            user=owner_user,
            provider='github',
            provider_user_id='github-user-456',
            provider_email=owner_user.email,
            provider_name=owner_user.get_full_name(),
            provider_picture_url='https://github.com/photo.jpg'
        )
        user = github_account.user
        
        mock_exchange.return_value = 'github-access-token-123'
        mock_get_info.return_value = {
            'provider_user_id': github_account.provider_user_id,
            'email': user.email,
            'name': user.get_full_name(),
            'picture': 'https://github.com/photo.jpg',
        }
        
        data = {
            'code': 'github-auth-code-xyz',
        }
        
        response = api_client.post('/api/auth/github/callback', data, format='json')
        
        assert response.status_code == status.HTTP_200_OK
        assert response.data['data']['email'] == user.email
    
    @patch('users.oauth_service.GitHubOAuthService.exchange_code_for_token')
    @patch('users.oauth_service.GitHubOAuthService.get_user_info')
    def test_engineer_join_via_github_invitation(self, mock_get_info, mock_exchange, api_client, invitation):
        """Test engineer joining via GitHub OAuth with invitation."""
        mock_exchange.return_value = 'github-access-token-123'
        mock_get_info.return_value = {
            'provider_user_id': 'github-engineer-999',
            'email': invitation.email,
            'name': 'Alice Engineer',
            'picture': 'https://github.com/photo.jpg',
        }
        
        data = {
            'code': 'github-auth-code-xyz',
            'invite_token': invitation.token,
        }
        
        response = api_client.post('/api/auth/github/callback', data, format='json')
        
        assert response.status_code == status.HTTP_200_OK
        
        # Verify user created in invited tenant
        user = User.objects.get(email=invitation.email)
        assert user.tenant == invitation.tenant
        assert user.role == invitation.role
    
    @patch('users.oauth_service.GitHubOAuthService.exchange_code_for_token')
    def test_github_oauth_invalid_code(self, mock_exchange, api_client):
        """Test GitHub OAuth fails with invalid code."""
        mock_exchange.side_effect = Exception('Invalid code')
        
        data = {
            'code': 'invalid-code',
        }
        
        response = api_client.post('/api/auth/github/callback', data, format='json')
        
        assert response.status_code == status.HTTP_400_BAD_REQUEST