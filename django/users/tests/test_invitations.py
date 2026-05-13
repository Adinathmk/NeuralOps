"""
Tests for engineer invitation flow.
Location: backend/django/users/tests/test_invitations.py
"""
import pytest
from rest_framework import status


@pytest.mark.django_db
class TestInviteEngineerView:
    """Test engineer invitation endpoint."""
    
    def test_invite_engineer_success(self, admin_client, tenant):
        """Test successful engineer invitation."""
        data = {
            'email': 'newengineer@example.com',
            'role': 'engineer',
        }
        
        response = admin_client.post('/api/invitations/send', data, format='json')
        
        assert response.status_code == status.HTTP_201_CREATED
        assert response.data['success'] is True
        assert response.data['data']['email'] == 'newengineer@example.com'
        assert response.data['data']['role'] == 'engineer'
    
    def test_invite_viewer_success(self, admin_client):
        """Test inviting viewer role."""
        data = {
            'email': 'viewer@example.com',
            'role': 'viewer',
        }
        
        response = admin_client.post('/api/invitations/send', data, format='json')
        
        assert response.status_code == status.HTTP_201_CREATED
    
    def test_invite_cannot_be_owner(self, admin_client):
        """Test cannot invite as owner."""
        data = {
            'email': 'newowner@example.com',
            'role': 'owner',
        }
        
        response = admin_client.post('/api/invitations/send', data, format='json')
        
        assert response.status_code == status.HTTP_400_BAD_REQUEST
    
    def test_invite_requires_admin(self, engineer_client):
        """Test only admins can invite."""
        data = {
            'email': 'newengineer@example.com',
            'role': 'engineer',
        }
        
        response = engineer_client.post('/api/invitations/send', data, format='json')
        
        assert response.status_code == status.HTTP_403_FORBIDDEN
    
    def test_invite_duplicate_in_tenant(self, admin_client, owner_user):
        """Test cannot invite existing user in same tenant."""
        data = {
            'email': owner_user.email,
            'role': 'engineer',
        }
        
        response = admin_client.post('/api/invitations/send', data, format='json')
        
        assert response.status_code == status.HTTP_409_CONFLICT
    
    def test_invite_requires_auth(self, api_client):
        """Test invitation requires authentication."""
        data = {
            'email': 'newengineer@example.com',
            'role': 'engineer',
        }
        
        response = api_client.post('/api/invitations/send', data, format='json')
        
        assert response.status_code == status.HTTP_401_UNAUTHORIZED


@pytest.mark.django_db
class TestValidateInvitationView:
    """Test invitation validation endpoint."""
    
    def test_validate_valid_invitation(self, api_client, invitation):
        """Test validating valid invitation."""
        response = api_client.get(f'/api/invitations/validate?token={invitation.token}')
        
        assert response.status_code == status.HTTP_200_OK
        assert response.data['success'] is True
        assert response.data['data']['email'] == invitation.email
    
    def test_validate_invalid_token(self, api_client):
        """Test validation fails with invalid token."""
        response = api_client.get('/api/invitations/validate?token=invalid-token')
        
        assert response.status_code == status.HTTP_404_NOT_FOUND
    
    def test_validate_expired_invitation(self, api_client, expired_invitation):
        """Test validation fails with expired token."""
        response = api_client.get(f'/api/invitations/validate?token={expired_invitation.token}')
        
        assert response.status_code == status.HTTP_403_FORBIDDEN
    
    def test_validate_missing_token(self, api_client):
        """Test validation fails without token."""
        response = api_client.get('/api/invitations/validate')
        
        assert response.status_code == status.HTTP_400_BAD_REQUEST


@pytest.mark.django_db
class TestJoinWithInvitationView:
    """Test engineer joining via invitation."""
    
    def test_join_success(self, api_client, invitation):
        """Test successful join via invitation."""
        data = {
            'invite_token': invitation.token,
            'password': 'NewSecurePass123!',
            'password_confirm': 'NewSecurePass123!',
            'first_name': 'Bob',
            'last_name': 'Engineer',
        }
        
        response = api_client.post('/api/invitations/join', data, format='json')
        
        assert response.status_code == status.HTTP_201_CREATED
        assert response.data['success'] is True
        assert 'access_token' in response.data
        
        # Verify user created in correct tenant
        from django.contrib.auth import get_user_model
        User = get_user_model()
        user = User.objects.get(email=invitation.email)
        assert user.role == invitation.role
        assert user.tenant == invitation.tenant
        
        # Verify invitation marked accepted
        invitation.refresh_from_db()
        assert invitation.status == 'accepted'
        assert invitation.accepted_by == user
    
    def test_join_invalid_token(self, api_client):
        """Test join fails with invalid token."""
        data = {
            'invite_token': 'invalid-token',
            'password': 'NewSecurePass123!',
            'password_confirm': 'NewSecurePass123!',
        }
        
        response = api_client.post('/api/invitations/join', data, format='json')
        
        assert response.status_code == status.HTTP_400_BAD_REQUEST
    
    def test_join_expired_invitation(self, api_client, expired_invitation):
        """Test join fails with expired invitation."""
        data = {
            'invite_token': expired_invitation.token,
            'password': 'NewSecurePass123!',
            'password_confirm': 'NewSecurePass123!',
        }
        
        response = api_client.post('/api/invitations/join', data, format='json')
        
        assert response.status_code == status.HTTP_400_BAD_REQUEST
    
    def test_join_password_mismatch(self, api_client, invitation):
        """Test join fails with mismatched passwords."""
        data = {
            'invite_token': invitation.token,
            'password': 'NewSecurePass123!',
            'password_confirm': 'DifferentPass123!',
        }
        
        response = api_client.post('/api/invitations/join', data, format='json')
        
        assert response.status_code == status.HTTP_400_BAD_REQUEST


@pytest.mark.django_db
class TestListInvitationsView:
    """Test listing invitations."""
    
    def test_list_invitations(self, admin_client, invitation):
        """Test listing pending invitations."""
        response = admin_client.get('/api/invitations/?status=pending')
        
        assert response.status_code == status.HTTP_200_OK
        assert len(response.data['data']) > 0
    
    def test_list_requires_admin(self, engineer_client):
        """Test listing requires admin role."""
        response = engineer_client.get('/api/invitations/?status=pending')
        
        assert response.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.django_db
class TestCancelInvitationView:
    """Test canceling invitations."""
    
    def test_cancel_invitation(self, admin_client, invitation):
        """Test successful invitation cancellation."""
        response = admin_client.post(f'/api/invitations/{invitation.id}/cancel', format='json')
        
        assert response.status_code == status.HTTP_200_OK
        
        # Verify cancelled
        invitation.refresh_from_db()
        assert invitation.status == 'cancelled'
    
    def test_cancel_requires_admin(self, engineer_client, invitation):
        """Test cancellation requires admin."""
        response = engineer_client.post(f'/api/invitations/{invitation.id}/cancel', format='json')
        
        assert response.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.django_db
class TestResendInvitationView:
    """Test resending invitations."""
    
    def test_resend_invitation(self, admin_client, invitation):
        """Test resending invitation."""
        response = admin_client.post(f'/api/invitations/{invitation.id}/resend', format='json')
        
        assert response.status_code == status.HTTP_200_OK
        
        # Verify resend count incremented
        invitation.refresh_from_db()
        assert invitation.email_resent_count > 0