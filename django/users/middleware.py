from django.utils.deprecation import MiddlewareMixin
from django.conf import settings
import jwt
import logging

logger = logging.getLogger(__name__)


class TenantMiddleware(MiddlewareMixin):
    """
    Extract tenant_id from JWT token and attach to request.
    
    Ensures every request has tenant context for multi-tenant isolation.
    
    Workflow:
    1. Check Authorization header for Bearer token
    2. Verify JWT signature (no DB call)
    3. Extract tenant_id from JWT claims
    4. Attach to request.tenant_id
    
    If token is invalid/missing, request.tenant_id = None (public endpoints)
    """
    
    def process_request(self, request):
        """Attach tenant context to request before view processing."""
        
        # Initialize tenant context (default to None for public endpoints)
        request.tenant_id = None
        request.user_id = None
        request.user_email = None
        request.user_role = None
        request.is_superadmin = False
        
        # Extract Authorization header
        auth_header = request.META.get('HTTP_AUTHORIZATION', '')
        
        if not auth_header.startswith('Bearer '):
            # No token provided - public endpoint (health, register, login)
            return None
        
        try:
            # Extract token
            token = auth_header.split(' ')[1]
            
            # Verify token signature (no database call)
            payload = self._verify_jwt_token(token)
            
            # Attach claims to request
            request.tenant_id = payload.get('tenant_id')
            request.user_id = payload.get('user_id')
            request.user_email = payload.get('email')
            request.user_role = payload.get('role')
            request.is_superadmin = payload.get('is_superadmin', False)
            
            logger.debug(
                f"Tenant context attached: user={request.user_email}, tenant={request.tenant_id}"
            )
            
        except Exception as e:
            # JWT verification failed - will be handled by JWTAuthentication in view
            logger.debug(f"JWT verification failed in middleware: {str(e)}")
            request.tenant_id = None
        
        return None
    
    @staticmethod
    def _verify_jwt_token(token):
        """
        Verify JWT token signature without database call.
        
        Args:
            token: JWT token string
            
        Returns:
            dict: JWT claims/payload
            
        Raises:
            jwt.ExpiredSignatureError: Token has expired
            jwt.InvalidTokenError: Token signature invalid
        """
        secret = settings.JWT_SECRET_KEY
        algorithm = settings.JWT_ALGORITHM
        
        try:
            payload = jwt.decode(token, secret, algorithms=[algorithm])
            return payload
        except jwt.ExpiredSignatureError:
            raise jwt.ExpiredSignatureError('Token has expired')
        except jwt.InvalidTokenError as e:
            raise jwt.InvalidTokenError(f'Invalid token: {str(e)}')