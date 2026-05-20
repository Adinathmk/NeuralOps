from django.utils.deprecation import MiddlewareMixin
from django.conf import settings
from django.db import connection, transaction
import jwt
import logging

logger = logging.getLogger(__name__)

class TenantMiddleware:
    """
    Extract tenant_id from JWT token and attach to request.
    Manages PostgreSQL Row-Level Security (RLS) context via set_config
    inside a transaction-scoped block for strict isolation.
    """
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Initialize context
        request.tenant_id = None
        request.user_id = None
        request.user_email = None
        request.user_role = None
        request.is_superadmin = False
        
        auth_header = request.META.get('HTTP_AUTHORIZATION', '')
        
        if auth_header.startswith('Bearer '):
            try:
                token = auth_header.split(' ')[1]
                payload = self._verify_jwt_token(token)
                
                request.tenant_id = payload.get('tenant_id')
                request.user_id = payload.get('user_id')
                request.user_email = payload.get('email')
                request.user_role = payload.get('role')
                request.is_superadmin = payload.get('is_superadmin', False)
                
                # Trust the JWT payload for superadmin status rather than inferring from roles
                # request.is_superadmin is already set from payload.get('is_superadmin', False)
                
                logger.debug(
                    f"Tenant context: user={request.user_email}, tenant={request.tenant_id}, superadmin={request.is_superadmin}"
                )
            except Exception as e:
                logger.debug(f"JWT verification failed in middleware: {str(e)}")
        
        # --- Connection-Level RLS Context ---
        # We avoid transaction.atomic() to prevent long-running transactions from locking Postgres.
        # Instead, we set variables at the connection level (is_local=false) and MUST clear them in finally.
        
        # RLS Chicken-and-Egg Fix: Auth endpoints need to look up users across all tenants to verify passwords
        # Since these views are strictly controlled and don't expose tenant data, we bypass RLS for them.
        UNRESTRICTED_PATHS = [
            '/api/auth/login',
            '/api/auth/register',
            '/api/auth/forgot-password',
            '/api/auth/reset-password',
            '/api/auth/verify-email',
            '/api/auth/resend-verification',
            '/api/auth/refresh-token',
            '/api/auth/google/callback',
            '/api/auth/github/callback',
            '/api/auth/mfa/',
            '/api/invitations/'
        ]
        is_unrestricted = any(request.path.startswith(p) for p in UNRESTRICTED_PATHS)
        
        try:
            with connection.cursor() as cursor:
                if is_unrestricted or request.is_superadmin:
                    # Auth endpoint or Platform admin: explicit RLS bypass
                    cursor.execute("SELECT set_config('app.bypass_rls', 'on', false)")
                    cursor.execute("SELECT set_config('app.current_tenant', '', false)")
                elif request.tenant_id:
                    # Normal tenant: strict isolation
                    cursor.execute("SELECT set_config('app.bypass_rls', 'off', false)")
                    cursor.execute("SELECT set_config('app.current_tenant', %s, false)", [str(request.tenant_id)])
                else:
                    # Unauthenticated/Invalid: Fail closed
                    cursor.execute("SELECT set_config('app.bypass_rls', 'off', false)")
                    cursor.execute("SELECT set_config('app.current_tenant', '', false)")
            
            # Process the view
            response = self.get_response(request)
            
        finally:
            # CRITICAL: Clean up connection variables before the connection returns to the Django pool!
            # If the process hard-crashes, Postgres drops the connection and wipes state anyway.
            if connection.connection is not None:
                try:
                    with connection.cursor() as cursor:
                        cursor.execute("SELECT set_config('app.bypass_rls', 'off', false)")
                        cursor.execute("SELECT set_config('app.current_tenant', '', false)")
                except Exception:
                    # If the connection is already dead, Postgres will wipe the session state automatically.
                    pass
            
        return response

    @staticmethod
    def _verify_jwt_token(token):
        secret = settings.JWT_SECRET_KEY
        algorithm = settings.JWT_ALGORITHM
        try:
            return jwt.decode(token, secret, algorithms=[algorithm])
        except jwt.ExpiredSignatureError:
            raise jwt.ExpiredSignatureError('Token has expired')
        except jwt.InvalidTokenError as e:
            raise jwt.InvalidTokenError(f'Invalid token: {str(e)}')