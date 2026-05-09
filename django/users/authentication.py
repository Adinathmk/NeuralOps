import jwt
import os
from datetime import datetime, timedelta
from django.conf import settings
from rest_framework.authentication import TokenAuthentication
from rest_framework.exceptions import AuthenticationFailed


class JWTAuthentication(TokenAuthentication):
    """
    JWT authentication without calling database on every request.
    Validates token signature and extracts claims only.
    """
    
    @staticmethod
    def generate_tokens(user):
        """
        Generate access and refresh tokens for a user.
        Returns: (access_token, refresh_token)
        """
        secret = settings.JWT_SECRET_KEY
        algorithm = settings.JWT_ALGORITHM
        
        # Access token expires in 15 minutes
        access_expire = datetime.utcnow() + timedelta(
            minutes=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES
        )
        
        # Refresh token expires in 7 days
        refresh_expire = datetime.utcnow() + timedelta(
            days=settings.JWT_REFRESH_TOKEN_EXPIRE_DAYS
        )
        
        # Access token payload
        access_payload = {
            'user_id': str(user.id),
            'email': user.email,
            'tenant_id': str(user.tenant.id),
            'tenant_name': user.tenant.name,
            'role': user.role,
            'is_superadmin': user.is_superadmin,
            'exp': access_expire,
            'iat': datetime.utcnow(),
            'type': 'access'
        }
        
        # Refresh token payload (minimal)
        refresh_payload = {
            'user_id': str(user.id),
            'tenant_id': str(user.tenant.id),
            'exp': refresh_expire,
            'iat': datetime.utcnow(),
            'type': 'refresh'
        }
        
        access_token = jwt.encode(access_payload, secret, algorithm=algorithm)
        refresh_token = jwt.encode(refresh_payload, secret, algorithm=algorithm)
        
        return access_token, refresh_token
    
    @staticmethod
    def verify_token(token):
        """
        Verify JWT token without database call.
        Validates signature and returns claims.
        """
        secret = settings.JWT_SECRET_KEY
        algorithm = settings.JWT_ALGORITHM
        
        try:
            payload = jwt.decode(token, secret, algorithms=[algorithm])
            return payload
        except jwt.ExpiredSignatureError:
            raise AuthenticationFailed('Token has expired')
        except jwt.InvalidTokenError as e:
            raise AuthenticationFailed(f'Invalid token: {str(e)}')
    
    def authenticate(self, request):
        """
        Authenticate request using Bearer token.
        Extracts token from Authorization header: "Bearer <token>"
        """
        auth_header = request.META.get('HTTP_AUTHORIZATION', '')
        
        if not auth_header:
            return None
        
        parts = auth_header.split()
        
        if len(parts) != 2 or parts[0].lower() != 'bearer':
            return None
        
        token = parts[1]
        payload = self.verify_token(token)
        
        # Attach claims to request for later use
        request.user_id = payload.get('user_id')
        request.tenant_id = payload.get('tenant_id')
        request.user_email = payload.get('email')
        request.user_role = payload.get('role')
        request.is_superadmin = payload.get('is_superadmin', False)
        
        # Return (user, auth) tuple. For JWT, user is None (stateless)
        return (None, payload)