# NEW FILE: users/middleware.py
from django.utils.deprecation import MiddlewareMixin
from django.conf import settings
import jwt
import logging

logger = logging.getLogger(__name__)


class TenantMiddleware(MiddlewareMixin):
    """
    Multi-Tenant Middleware
    
    Extracts tenant_id from JWT token or session user.
    Attaches to request object for use in views.
    """
    
    def process_request(self, request):
        """Attach tenant context to request."""
        
        request.tenant_id = None
        request.user_id = None
        request.is_superadmin = False
        request.tenant = None
        
        # Option 1: JWT Authentication (API requests)
        auth_header = request.META.get('HTTP_AUTHORIZATION', '')
        
        if auth_header.startswith('Bearer '):
            try:
                token = auth_header.split(' ')[1]
                payload = self._verify_jwt_token(token)
                
                request.tenant_id = payload.get('tenant_id')
                request.user_id = payload.get('user_id')
                request.is_superadmin = payload.get('is_superadmin', False)
                
            except Exception as e:
                logger.debug(f"JWT token verification failed: {str(e)}")
        
        # Option 2: Session Authentication (Admin/Web requests)
        elif request.user and request.user.is_authenticated:
            if hasattr(request.user, 'tenant'):
                request.tenant_id = str(request.user.tenant.id)
                request.user_id = str(request.user.id)
                request.tenant = request.user.tenant
        
        return None
    
    @staticmethod
    def _verify_jwt_token(token):
        """Verify JWT token signature."""
        secret = settings.JWT_SECRET_KEY
        algorithm = settings.JWT_ALGORITHM
        
        try:
            payload = jwt.decode(token, secret, algorithms=[algorithm])
            return payload
        except jwt.ExpiredSignatureError:
            raise jwt.ExpiredSignatureError('Token has expired')
        except jwt.InvalidTokenError as e:
            raise jwt.InvalidTokenError(f'Invalid token: {str(e)}')