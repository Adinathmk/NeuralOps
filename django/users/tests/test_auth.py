"""
Tests for user authentication - register, login, logout, sessions.
Location: backend/django/users/tests/test_auth.py
"""
import pytest
from rest_framework import status
from django.contrib.auth import get_user_model
from users.models import UserSession, EmailVerification

User = get_user_model()


@pytest.mark.django_db
class TestRegisterView:
    """Test owner registration endpoint."""
    
    def test_register_owner_success(self, api_client):
        """Test successful owner registration."""
        data = {
            'email': 'newowner@example.com',
            'password': 'SecurePass123!',
            'password_confirm': 'SecurePass123!',
            'tenant_name': 'New Company Inc',
        }
        
        response = api_client.post('/api/auth/register', data, format='json')
        
        assert response.status_code == status.HTTP_201_CREATED
        assert response.data['success'] is True
        
        # Check user created
        user = User.objects.get(email='newowner@example.com')
        assert user.role == 'owner'
        assert user.email_verified is False
        
        # Check tenant created
        assert user.tenant is not None
        assert user.tenant.name == 'New Company Inc'
        
        # Check email verification token created
        verification = EmailVerification.objects.filter(user=user).exists()
        assert verification is True
    
    def test_register_password_mismatch(self, api_client):
        """Test registration fails with mismatched passwords."""
        data = {
            'email': 'owner@test.com',
            'password': 'SecurePass123!',
            'password_confirm': 'DifferentPass123!',
            'tenant_name': 'Test Co',
        }
        
        response = api_client.post('/api/auth/register', data, format='json')
        
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert 'password' in response.data['errors']
    
    def test_register_weak_password(self, api_client):
        """Test registration fails with weak password."""
        data = {
            'email': 'owner@test.com',
            'password': 'weak',
            'password_confirm': 'weak',
            'tenant_name': 'Test Co',
        }
        
        response = api_client.post('/api/auth/register', data, format='json')
        
        assert response.status_code == status.HTTP_400_BAD_REQUEST
    
    def test_register_duplicate_email(self, api_client, owner_user):
        """Test registration fails with duplicate email."""
        data = {
            'email': owner_user.email,
            'password': 'SecurePass123!',
            'password_confirm': 'SecurePass123!',
            'tenant_name': 'Other Company',
        }
        
        response = api_client.post('/api/auth/register', data, format='json')
        
        assert response.status_code == status.HTTP_400_BAD_REQUEST
    
    def test_register_missing_fields(self, api_client):
        """Test registration fails with missing fields."""
        data = {
            'email': 'owner@test.com',
        }
        
        response = api_client.post('/api/auth/register', data, format='json')
        
        assert response.status_code == status.HTTP_400_BAD_REQUEST


@pytest.mark.django_db
class TestLoginView:
    """Test user login endpoint."""
    
    def test_login_success(self, api_client, owner_user):
        """Test successful login."""
        data = {
            'email': owner_user.email,
            'password': 'TestPass123!',
            'tenant_slug': owner_user.tenant.slug,
        }
        
        response = api_client.post('/api/auth/login', data, format='json')
        
        assert response.status_code == status.HTTP_200_OK
        assert response.data['success'] is True
        assert 'access_token' in response.data
        assert 'refresh_token' in response.data
        assert response.data['data']['email'] == owner_user.email
    
    def test_login_invalid_password(self, api_client, owner_user):
        """Test login fails with wrong password."""
        data = {
            'email': owner_user.email,
            'password': 'WrongPassword123!',
            'tenant_slug': owner_user.tenant.slug,
        }
        
        response = api_client.post('/api/auth/login', data, format='json')
        
        assert response.status_code == status.HTTP_401_UNAUTHORIZED
    
    def test_login_invalid_email(self, api_client, tenant):
        """Test login fails with non-existent email."""
        data = {
            'email': 'nonexistent@example.com',
            'password': 'TestPass123!',
            'tenant_slug': tenant.slug,
        }
        
        response = api_client.post('/api/auth/login', data, format='json')
        
        assert response.status_code == status.HTTP_401_UNAUTHORIZED
    
    def test_login_invalid_tenant(self, api_client, owner_user):
        """Test login fails with invalid tenant."""
        data = {
            'email': owner_user.email,
            'password': 'TestPass123!',
            'tenant_slug': 'invalid-tenant',
        }
        
        response = api_client.post('/api/auth/login', data, format='json')
        
        assert response.status_code == status.HTTP_401_UNAUTHORIZED
    
    def test_login_creates_session(self, api_client, owner_user):
        """Test that login creates a session."""
        data = {
            'email': owner_user.email,
            'password': 'TestPass123!',
            'tenant_slug': owner_user.tenant.slug,
        }
        
        response = api_client.post('/api/auth/login', data, format='json')
        
        assert response.status_code == status.HTTP_200_OK
        
        # Verify session created
        session_exists = UserSession.objects.filter(user=owner_user).exists()
        assert session_exists is True


@pytest.mark.django_db
class TestLogoutView:
    """Test user logout endpoint."""
    
    def test_logout_success(self, owner_client, owner_user):
        """Test successful logout."""
        response = owner_client.post('/api/auth/logout', format='json')
        
        assert response.status_code == status.HTTP_200_OK
        assert response.data['success'] is True
    
    def test_logout_requires_auth(self, api_client):
        """Test logout requires authentication."""
        response = api_client.post('/api/auth/logout', format='json')
        
        assert response.status_code == status.HTTP_401_UNAUTHORIZED


@pytest.mark.django_db
class TestMeView:
    """Test current user endpoint."""
    
    def test_get_current_user(self, owner_client, owner_user):
        """Test getting current user info."""
        response = owner_client.get('/api/auth/me')
        
        assert response.status_code == status.HTTP_200_OK
        assert response.data['data']['email'] == owner_user.email
    
    def test_me_requires_auth(self, api_client):
        """Test me endpoint requires authentication."""
        response = api_client.get('/api/auth/me')
        
        assert response.status_code == status.HTTP_401_UNAUTHORIZED


@pytest.mark.django_db
class TestSessionListView:
    """Test user sessions endpoint."""
    
    def test_list_sessions(self, owner_client, user_session):
        """Test listing user sessions."""
        response = owner_client.get('/api/auth/sessions')
        
        assert response.status_code == status.HTTP_200_OK
        assert len(response.data) > 0
    
    def test_sessions_requires_auth(self, api_client):
        """Test sessions endpoint requires authentication."""
        response = api_client.get('/api/auth/sessions')
        
        assert response.status_code == status.HTTP_401_UNAUTHORIZED


@pytest.mark.django_db
class TestRevokeSessionView:
    """Test session revocation endpoint."""
    
    def test_revoke_session(self, owner_client, user_session):
        """Test revoking a session."""
        response = owner_client.post(
            f'/api/auth/sessions/{user_session.id}/revoke',
            format='json'
        )
        
        assert response.status_code == status.HTTP_200_OK
        
        # Verify session is revoked
        user_session.refresh_from_db()
        assert user_session.is_revoked is True