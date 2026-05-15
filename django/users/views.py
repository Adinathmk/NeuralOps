from rest_framework.views import APIView
from rest_framework.permissions import AllowAny, IsAuthenticated
from drf_spectacular.utils import extend_schema, OpenApiResponse, inline_serializer
from rest_framework import serializers
from .serializers import (
    RegisterSerializer, LoginSerializer, TokenRefreshSerializer, UserSerializer,VerifyEmailSerializer,ResendVerificationEmailSerializer,
    ResetPasswordSerializer,ChangePasswordSerializer,ForgotPasswordSerializer,GitHubOAuthCallbackSerializer,GoogleOAuthCallbackSerializer,
    ConfirmMFASerializer,VerifyMFATokenSerializer,DisableMFASerializer
)
from .authentication import JWTAuthentication
from .models import User,UserSession,MFAVerificationToken
from .cache import cache_manager
import logging
from datetime import datetime
from core.responses import APIResponse
from core.exceptions import RateLimitException,ValidationException,NotFoundException
from .models import EmailVerification,BackupCode,TOTPDevice
from .email import email_service
from django.conf import settings
from .models import PasswordReset
from .oauth_service import GoogleOAuthService, GitHubOAuthService
from .models import OAuthAccount
from rest_framework.exceptions import ValidationError
from .oauth_handlers import EngineerOAuthHandler, OwnerOAuthHandler
from .models import UserInvitation
from core.permissions import IsTenantAdmin
from .models import Tenant
from .serializers import InviteEngineerSerializer, JoinWithInvitationSerializer
from django.utils import timezone
from datetime import timedelta
from django.db import transaction
from .models import UserInvitation
from core.permissions import IsTenantAdmin
from pyotp import TOTP
import qrcode
from io import BytesIO
import base64
from django.db import transaction
from django.contrib.auth.hashers import check_password



logger = logging.getLogger(__name__)

class HealthCheckView(APIView):
    """Health check endpoint - no auth required"""
    permission_classes = [AllowAny]
    
    @extend_schema(
        summary="API Health Check",
        responses={200: inline_serializer(
            name='HealthCheckResponse',
            fields={'status': serializers.CharField()}
        )}
    )
    def get(self, request):
        return APIResponse.success(
            data={'status': 'healthy'},
            message='Server is healthy'
        )

class RegisterView(APIView):
    """
    Email/password owner registration.
    
    Creates new tenant + owner account.
    Engineers must join via invitation links.
    """
    permission_classes = [AllowAny]
    
    @extend_schema(
        summary="Register Owner & Tenant",
        request=RegisterSerializer,
        responses={201: UserSerializer}
    )
    def post(self, request):
        frontend_url = request.data.get('frontend_url', settings.FRONTEND_URL)
        
        serializer = RegisterSerializer(data=request.data)
        
        if not serializer.is_valid():
            return APIResponse.error(
                message='Registration failed',
                status_code=400,
                code='validation_error',
                errors=serializer.errors
            )
        
        user = serializer.save()
        
        # Create email verification token
        verification = EmailVerification.objects.create(user=user)
        
        # Send verification email
        try:
            email_service.send_verification_email(
                user=user,
                verification_token=verification.token,
                frontend_url=frontend_url
            )
        except Exception as e:
            logger.error(f"Failed to send verification email: {str(e)}")
        
        return APIResponse.created(
            data=UserSerializer(user).data,
            message='Owner account created. Please check your email to verify.',
            access_token=None,
            refresh_token=None
        )


class LoginView(APIView):
    """Login user with session tracking."""
    permission_classes = [AllowAny]
    
    @extend_schema(
        summary="Login User",
        request=LoginSerializer,
        responses={200: UserSerializer}
    )
    def post(self, request):
        email = request.data.get('email', '').lower()
        
        # Check rate limiting
        if cache_manager.is_login_rate_limited(email):
            raise RateLimitException('Too many failed login attempts')
        
        serializer = LoginSerializer(data=request.data)
        
        if serializer.is_valid():
            user = serializer.validated_data['user']
            
            cache_manager.reset_failed_login(email)

            # Check if MFA is enabled
            try:
                device = TOTPDevice.objects.get(user=user, is_confirmed=True)
                
                # MFA is enabled - return temporary MFA token
                mfa_token_obj = MFAVerificationToken.objects.create(user=user)
                
                logger.info(f"MFA verification required for {user.email}")
                
                return APIResponse.success(
                    message='MFA required. Please verify with authenticator app.',
                    mfa_token=mfa_token_obj.token,  # Send this back
                    requires_mfa=True
                )
            except TOTPDevice.DoesNotExist:
                # MFA not enabled - return access tokens directly
                access_token, refresh_token = JWTAuthentication.generate_tokens(
                    user,
                    request
                )

                logger.info(f"User {email} logged in from {JWTAuthentication._get_client_ip(request)}")

                return APIResponse.success(
                    data=UserSerializer(user).data,
                    message='Login successful.',
                    access_token=access_token,
                    refresh_token=refresh_token
                )
            
        
        failed_count = cache_manager.increment_failed_login(email)
        
        if failed_count >= 3:
            logger.warning(f"Multiple failed login attempts for {email}: {failed_count}")

        first_error = next(iter(serializer.errors.values()))[0]
        
        return APIResponse.error(
            message=first_error,
            status_code=401,
            code='auth_error',
            errors=serializer.errors
        )
    
class TokenRefreshView(APIView):
    """
    Refresh access token using refresh token.
    POST /api/auth/refresh-token
    {
        "refresh_token": "eyJ0eXAi..."
    }
    """
    permission_classes = [AllowAny]
    
    @extend_schema(
        summary="Refresh Access Token",
        request=TokenRefreshSerializer,
        responses={200: inline_serializer(
            name='TokenRefreshResponse',
            fields={'access_token': serializers.CharField(), 'refresh_token': serializers.CharField()}
        )}
    )
    def post(self, request):
        serializer = TokenRefreshSerializer(data=request.data)
        
        if serializer.is_valid():
            payload = JWTAuthentication.verify_token(
                serializer.validated_data['refresh_token']
            )
            
            # Get user from token
            user = User.objects.get(id=payload['user_id'])
            
            # Generate new tokens
            access_token, refresh_token = JWTAuthentication.generate_tokens(user)
            
            return APIResponse.success(
                message='Token refreshed successfully.',
                access_token=access_token,
                refresh_token=refresh_token
            )
        
        return APIResponse.error(
            message='Token refresh failed',
            status_code=400,
            code='validation_error',
            errors=serializer.errors
        )


class MeView(APIView):
    """
    Get current user profile.
    GET /api/auth/me
    Headers: Authorization: Bearer <access_token>
    """
    permission_classes = [IsAuthenticated]
    authentication_classes = [JWTAuthentication]
    
    @extend_schema(
        summary="Get Current User Profile",
        responses={200: UserSerializer}
    )
    def get(self, request):
        user_id = request.user_id
        try:
            user = User.objects.get(id=user_id)
        except User.DoesNotExist:
            raise NotFoundException('User not found')
        return APIResponse.success(
            data=UserSerializer(user).data,
            message='User profile retrieved.'
        )
    

class LogoutView(APIView):
    """Logout user - revoke token and session."""
    permission_classes = [IsAuthenticated]
    authentication_classes = [JWTAuthentication]
    
    @extend_schema(
        summary="Logout User",
        request=None,
        responses={200: OpenApiResponse(description="Logged out successfully")}
    )
    def post(self, request):
        try:
            user_id = request.user_id
            jti = request.auth.get('jti')
            
            if not jti:
                raise ValidationException('Invalid token format')
            
            # Add token to revocation blocklist
            exp_time = request.auth.get('exp')
            if exp_time:
                remaining_seconds = int(exp_time - timezone.now().timestamp())
                if remaining_seconds > 0:
                    cache_manager.blocklist_token(jti, remaining_seconds)
            
            # Revoke session record
            try:
                session = UserSession.objects.get(session_id=jti)
                session.revoke()
            except UserSession.DoesNotExist:
                pass
            
            logger.info(f"User {request.user_email} logged out")
            
            return APIResponse.success(
                message='Logged out successfully.'
            )
        
        except Exception as e:
            logger.error(f"Logout error: {str(e)}")
            raise

# ADD SessionListView (see all active sessions):

class SessionListView(APIView):
    """
    List all active sessions for current user.
    GET /api/auth/sessions
    Headers: Authorization: Bearer <access_token>
    """
    permission_classes = [IsAuthenticated]
    authentication_classes = [JWTAuthentication]
    
    @extend_schema(
        summary="List User Sessions",
        responses={200: inline_serializer(
            name='SessionListResponse',
            fields={
                'id': serializers.CharField(),
                'device_name': serializers.CharField(),
                'ip_address': serializers.CharField(),
                'last_activity': serializers.DateTimeField(),
                'created_at': serializers.DateTimeField(),
                'expires_at': serializers.DateTimeField(),
            },
            many=True
        )}
    )
    def get(self, request):
        user_id = request.user_id
        sessions = UserSession.objects.filter(
            user_id=user_id,
            is_active=True,
            is_revoked=False
        ).order_by('-last_activity_at')
        
        data = [
            {
                'id': str(session.id),
                'device_name': session.device_name,
                'ip_address': session.ip_address,
                'last_activity': session.last_activity_at.isoformat(),
                'created_at': session.created_at.isoformat(),
                'expires_at': session.expires_at.isoformat(),
            }
            for session in sessions
        ]
        
        return APIResponse.success(
            data=data,
            message='Sessions retrieved successfully.'
        )


# ADD RevokeSessionView (force logout from device):

class RevokeSessionView(APIView):
    """
    Revoke a specific session (force logout from device).
    POST /api/auth/sessions/<session_id>/revoke
    Headers: Authorization: Bearer <access_token>
    """
    permission_classes = [IsAuthenticated]
    authentication_classes = [JWTAuthentication]
    
    @extend_schema(
        summary="Revoke User Session",
        request=None,
        responses={200: OpenApiResponse(description="Session revoked successfully")}
    )
    def post(self, request, session_id):
        try:
            session = UserSession.objects.get(
                id=session_id,
                user_id=request.user_id
            )
            
            session.revoke()
            
            # Add token to blocklist (prevent reuse)
            cache_manager.blocklist_token(session.session_id, 86400)
            
            logger.info(f"User {request.user_email} revoked session {session_id}")
            
            return APIResponse.success(
                message='Session revoked successfully.'
            )
        
        except UserSession.DoesNotExist:
            raise NotFoundException('Session not found')
        except Exception as e:
            logger.error(f"Revoke session error: {str(e)}")
            raise


class VerifyEmailView(APIView):
    """
    Verify email with token.
    POST /api/auth/verify-email
    {
        "token": "abc123..."
    }
    """
    permission_classes = [AllowAny]
    
    @extend_schema(
        summary="Verify Email",
        request=VerifyEmailSerializer,
        responses={200: UserSerializer}
    )
    def post(self, request):
        serializer = VerifyEmailSerializer(data=request.data)
        
        if serializer.is_valid():
            verification = serializer.validated_data['token']
            
            # Mark as verified
            verification.verify()
            user = verification.user
            
            # Send welcome email
            try:
                email_service.send_welcome_email(user)
            except Exception as e:
                logger.warning(f"Failed to send welcome email: {str(e)}")
            
            # Generate tokens for auto-login
            access_token, refresh_token = JWTAuthentication.generate_tokens(user, request)
            
            logger.info(f"Email verified for user {user.email}")
            
            return APIResponse.success(
                data=UserSerializer(user).data,
                message='Email verified successfully.',
                access_token=access_token,
                refresh_token=refresh_token
            )
        
        return APIResponse.error(
            message='Email verification failed',
            status_code=400,
            code='verification_error',
            errors=serializer.errors
        )
    

class ResendVerificationEmailView(APIView):
    """
    Resend verification email.

    POST /api/auth/resend-verification

    {
        "email": "user@example.com"
    }
    """

    permission_classes = [AllowAny]

    @extend_schema(
        summary="Resend Verification Email",
        request=ResendVerificationEmailSerializer,
        responses={200: OpenApiResponse(description="Verification email sent")}
    )
    def post(self, request):
        serializer = ResendVerificationEmailSerializer(data=request.data)

        if not serializer.is_valid():
            return APIResponse.error(
                message='Failed to resend verification',
                status_code=400,
                code='validation_error',
                errors=serializer.errors
            )

        email = serializer.validated_data['email']

        try:
            user = User.objects.get(email=email)

            # User exists but already verified
            if user.email_verified:
                return APIResponse.error(
                    message='Email already verified.',
                    status_code=400,
                    code='already_verified'
                )

            # Always use backend-configured frontend URL
            frontend_url = settings.FRONTEND_URL

            # Delete old tokens
            EmailVerification.objects.filter(user=user).delete()

            # Create new token
            verification = EmailVerification.objects.create(user=user)

            # Send verification email
            try:
                email_service.send_verification_email(
                    user=user,
                    verification_token=verification.token,
                    frontend_url=frontend_url
                )

            except Exception as e:
                logger.error(
                    f"Failed to send verification email to {email}: {str(e)}"
                )

            logger.info(f"Verification email resent to {email}")

        except User.DoesNotExist:
            # IMPORTANT:
            # Do NOT reveal whether user exists
            logger.warning(
                f"Verification resend requested for non-existent email: {email}"
            )

        return APIResponse.success(
            message=(
                'If an account with this email exists, '
                'a verification email has been sent.'
            )
        )
        


class ForgotPasswordView(APIView):
    """
    Request password reset.

    POST /api/auth/forgot-password

    {
        "email": "user@example.com"
    }
    """

    permission_classes = [AllowAny]

    @extend_schema(
        summary="Forgot Password",
        request=ForgotPasswordSerializer,
        responses={200: OpenApiResponse(description="If email exists, reset link sent")}
    )
    def post(self, request):
        serializer = ForgotPasswordSerializer(data=request.data)

        if not serializer.is_valid():
            return APIResponse.error(
                message='Invalid email',
                status_code=400,
                code='validation_error',
                errors=serializer.errors
            )

        email = serializer.validated_data['email']

        try:
            user = User.objects.get(email=email)

            # Always use backend-configured frontend URL
            frontend_url = settings.FRONTEND_URL

            # Delete old reset tokens
            PasswordReset.objects.filter(user=user).delete()

            # Get client IP
            ip_address = JWTAuthentication._get_client_ip(request)

            # Create new reset token
            reset = PasswordReset.objects.create(
                user=user,
                ip_address=ip_address
            )

            # Send password reset email
            try:
                email_service.send_password_reset_email(
                    user=user,
                    reset_token=reset.token,
                    frontend_url=frontend_url
                )

            except Exception as e:
                logger.error(
                    f"Failed to send password reset email to {email}: {str(e)}"
                )

            logger.info(f"Password reset requested for {email}")

        except User.DoesNotExist:
            # IMPORTANT:
            # Do NOT reveal whether email exists
            logger.warning(
                f"Password reset requested for non-existent email: {email}"
            )

        # ALWAYS return success
        return APIResponse.success(
            message=(
                'If this email exists, '
                'you will receive a password reset link.'
            )
        )


# ADD ResetPasswordView:

class ResetPasswordView(APIView):
    """
    Reset password with token.
    POST /api/auth/reset-password
    {
        "token": "abc123...",
        "new_password": "NewPass123",
        "new_password_confirm": "NewPass123"
    }
    """
    permission_classes = [AllowAny]
    
    @extend_schema(
        summary="Reset Password",
        request=ResetPasswordSerializer,
        responses={200: OpenApiResponse(description="Password reset successfully")}
    )
    def post(self, request):
        serializer = ResetPasswordSerializer(data=request.data)
        
        if serializer.is_valid():
            reset = serializer.validated_data['token']
            new_password = serializer.validated_data['new_password']
            
            user = reset.user
            
            # Update password
            user.set_password(new_password)
            user.save()
            
            # Mark token as used
            reset.use()
            
            # Revoke all sessions (force re-login)
            UserSession.objects.filter(user=user).delete()
            
            
            # Send notification email
            try:
                email_service.send_password_changed_notification(user)
            except Exception as e:
                logger.warning(f"Failed to send password changed email: {str(e)}")
            
            logger.info(f"Password reset for user {user.email}")
            
            return APIResponse.success(
                message='Password reset successfully. Please log in with your new password.'
            )
        
        return APIResponse.error(
            message='Password reset failed',
            status_code=400,
            code='validation_error',
            errors=serializer.errors
        )


# ADD ChangePasswordView (for authenticated users):

class ChangePasswordView(APIView):
    """
    Change password (authenticated user).
    POST /api/auth/change-password
    Headers: Authorization: Bearer <access_token>
    {
        "current_password": "OldPass123",
        "new_password": "NewPass123",
        "new_password_confirm": "NewPass123"
    }
    """
    permission_classes = [IsAuthenticated]
    authentication_classes = [JWTAuthentication]
    
    @extend_schema(
        summary="Change Password",
        request=ChangePasswordSerializer,
        responses={200: OpenApiResponse(description="Password changed successfully")}
    )
    def post(self, request):
        user_id = request.user_id
        user = User.objects.get(id=user_id)
        
        serializer = ChangePasswordSerializer(data=request.data)
        
        if serializer.is_valid():
            current_password = serializer.validated_data['current_password']
            new_password = serializer.validated_data['new_password']
            
            # Verify current password
            if not user.check_password(current_password):
                return APIResponse.error(
                    message='Current password is incorrect',
                    status_code=400,
                    code='auth_error'
                )
            
            # Update password
            user.set_password(new_password)
            user.save()
            
            # Revoke all sessions (force re-login on all devices)
            UserSession.objects.filter(user=user).delete()
            
            
            # Send notification email
            try:
                email_service.send_password_changed_notification(user)
            except Exception as e:
                logger.warning(f"Failed to send password changed email: {str(e)}")
            
            logger.info(f"Password changed for user {user.email}")
            
            return APIResponse.success(
                message='Password changed successfully. Please log in again.'
            )
        
        return APIResponse.error(
            message='Password change failed',
            status_code=400,
            code='validation_error',
            errors=serializer.errors
        )
    
class GoogleOAuthCallbackView(APIView):
    """
    Google OAuth callback.
    
    POST /api/auth/google/callback
    {
        "code": "authorization_code",
        "invite_token": "engineer_invitation_token"  // optional
    }
    
    Flows:
    1. Owner signup (no invite_token): BLOCKED ❌
       → Returns error: "Please sign up with email/password"
       
    2. Owner login (no invite_token, existing account): ALLOWED ✅
       → Sign in to existing owner account
       
    3. Engineer signup (with invite_token): ALLOWED ✅
       → Create engineer account in invited tenant
       
    4. Engineer login (no invite_token, existing account): ALLOWED ✅
       → Sign in to existing engineer account
    """
    permission_classes = [AllowAny]
    
    @extend_schema(
        summary="Google OAuth Callback",
        request=GoogleOAuthCallbackSerializer,
        responses={200: UserSerializer}
    )
    def post(self, request):
        serializer = GoogleOAuthCallbackSerializer(data=request.data)
        
        if not serializer.is_valid():
            return APIResponse.error(
                message='OAuth authentication failed',
                status_code=400,
                code='validation_error',
                errors=serializer.errors
            )
        
        try:
            access_token = serializer.validated_data['code']
            invitation = serializer.validated_data.get('invite_token')
            
            # Get user info from Google
            user_info = GoogleOAuthService.get_user_info(access_token)
            user_info['provider'] = 'google'
            
            # Route to appropriate handler
            if invitation:
                # Engineer joining via invitation
                logger.info(f"Engineer OAuth signup via invitation - {user_info['email']}")
                user = EngineerOAuthHandler.process_oauth_invitation(
                    user_info,
                    invitation
                )
                log_message = (
                    f"Engineer {user.email} joined {invitation.tenant.name} "
                    f"via Google OAuth"
                )
            else:
                # No invitation → Try owner login (signup is BLOCKED)
                logger.info(f"Owner OAuth login attempt - {user_info['email']}")
                user = OwnerOAuthHandler.process_oauth_login(user_info)
                log_message = f"Owner {user.email} signed in via Google OAuth"
            
            # Generate tokens
            access_token, refresh_token = JWTAuthentication.generate_tokens(
                user,
                request
            )
            
            logger.info(log_message)
            
            return APIResponse.success(
                data=UserSerializer(user).data,
                message='Authenticated via Google.',
                access_token=access_token,
                refresh_token=refresh_token
            )
        
        except ValidationError as e:
            error_msg = str(e.detail) if hasattr(e, 'detail') else str(e)
            logger.warning(f"Google OAuth error: {error_msg}")
            return APIResponse.error(
                message=error_msg,
                status_code=400,
                code='auth_error'
            )
        except Exception as e:
            logger.error(f"Google OAuth error: {str(e)}", exc_info=True)
            return APIResponse.error(
                message='Google authentication failed',
                status_code=400,
                code='oauth_error'
            )
 
 
class GitHubOAuthCallbackView(APIView):
    """
    GitHub OAuth callback.
    
    POST /api/auth/github/callback
    {
        "code": "authorization_code",
        "invite_token": "engineer_invitation_token"  // optional
    }
    
    Flows:
    1. Owner signup (no invite_token): BLOCKED ❌
       → Returns error: "Please sign up with email/password"
       
    2. Owner login (no invite_token, existing account): ALLOWED ✅
       → Sign in to existing owner account
       
    3. Engineer signup (with invite_token): ALLOWED ✅
       → Create engineer account in invited tenant
       
    4. Engineer login (no invite_token, existing account): ALLOWED ✅
       → Sign in to existing engineer account
    """
    permission_classes = [AllowAny]
    
    @extend_schema(
        summary="GitHub OAuth Callback",
        request=GitHubOAuthCallbackSerializer,
        responses={200: UserSerializer}
    )
    def post(self, request):
        serializer = GitHubOAuthCallbackSerializer(data=request.data)
        
        if not serializer.is_valid():
            return APIResponse.error(
                message='OAuth authentication failed',
                status_code=400,
                code='validation_error',
                errors=serializer.errors
            )
        
        try:
            access_token = serializer.validated_data['code']
            invitation = serializer.validated_data.get('invite_token')
            
            # Get user info from GitHub
            user_info = GitHubOAuthService.get_user_info(access_token)
            user_info['provider'] = 'github'
            
            # Route to appropriate handler
            if invitation:
                # Engineer joining via invitation
                logger.info(f"Engineer OAuth signup via invitation - {user_info['email']}")
                user = EngineerOAuthHandler.process_oauth_invitation(
                    user_info,
                    invitation
                )
                log_message = (
                    f"Engineer {user.email} joined {invitation.tenant.name} "
                    f"via GitHub OAuth"
                )
            else:
                # No invitation → Try owner login (signup is BLOCKED)
                logger.info(f"Owner OAuth login attempt - {user_info['email']}")
                user = OwnerOAuthHandler.process_oauth_login(user_info)
                log_message = f"Owner {user.email} signed in via GitHub OAuth"
            
            # Generate tokens
            access_token, refresh_token = JWTAuthentication.generate_tokens(
                user,
                request
            )
            
            logger.info(log_message)
            
            return APIResponse.success(
                data=UserSerializer(user).data,
                message='Authenticated via GitHub.',
                access_token=access_token,
                refresh_token=refresh_token
            )
        
        except ValidationError as e:
            error_msg = str(e.detail) if hasattr(e, 'detail') else str(e)
            logger.warning(f"GitHub OAuth error: {error_msg}")
            return APIResponse.error(
                message=error_msg,
                status_code=400,
                code='auth_error'
            )
        except Exception as e:
            logger.error(f"GitHub OAuth error: {str(e)}", exc_info=True)
            return APIResponse.error(
                message='GitHub authentication failed',
                status_code=400,
                code='oauth_error'
            )


# ADD InviteEngineerView (admin action):

class InviteEngineerView(APIView):
    """
    Admin invites engineer to tenant.
    POST /api/invitations/send
    Headers: Authorization: Bearer <access_token>
    {
        "email": "bob@company.com",
        "role": "engineer"
    }
    """
    permission_classes = [IsAuthenticated, IsTenantAdmin]
    authentication_classes = [JWTAuthentication]
    
    @extend_schema(
        summary="Invite Engineer to Tenant",
        request=InviteEngineerSerializer,
        responses={201: inline_serializer(
            name='InviteResponse',
            fields={
                'id': serializers.CharField(),
                'email': serializers.EmailField(),
                'role': serializers.CharField(),
                'tenant': serializers.CharField(),
                'created_at': serializers.CharField(),
                'expires_at': serializers.CharField()
            }
        )}
    )
    def post(self, request):
        """Invite engineer to tenant."""
        tenant_id = request.tenant_id
        user_id = request.user_id
        
        try:
            tenant = Tenant.objects.get(id=tenant_id)
            inviter = User.objects.get(id=user_id)
        except (Tenant.DoesNotExist, User.DoesNotExist):
            raise NotFoundException('Tenant or user not found')
        
        serializer = InviteEngineerSerializer(data=request.data)
        
        if not serializer.is_valid():
            return APIResponse.error(
                message='Invitation failed',
                status_code=400,
                code='validation_error',
                errors=serializer.errors
            )
        
        email = serializer.validated_data['email']
        role = serializer.validated_data['role']
        frontend_url = request.data.get('frontend_url', settings.FRONTEND_URL)
        
        try:
            # Check if user already exists in tenant
            existing_user = User.objects.get(email=email, tenant=tenant)
            return APIResponse.error(
                message=f'User {email} already exists in {tenant.name}',
                status_code=409,
                code='conflict'
            )
        except User.DoesNotExist:
            pass
        
        # Create or update invitation
        invitation, created = UserInvitation.objects.get_or_create(
            tenant=tenant,
            email=email,
            status='pending',
            defaults={
                'invited_by': inviter,
                'role': role,
            }
        )
        
        # If invitation already existed but was expired, reset it
        if not created and invitation.status == 'expired':
            invitation.status = 'pending'
            invitation.expires_at = timezone.now() + timedelta(days=7)
            invitation.invited_by = inviter
            invitation.role = role
            invitation.save()
        
        # Send invitation email
        try:
            email_service.send_invitation_email(invitation, frontend_url)
        except Exception as e:
            logger.error(f"Failed to send invitation email: {str(e)}")
            return APIResponse.error(
                message='Invitation created but email delivery failed. Please retry.',
                status_code=500,
                code='email_error'
            )
        
        logger.info(
            f"Admin {inviter.email} invited {email} to {tenant.name} as {role}"
        )
        
        return APIResponse.created(
            data={
                'id': str(invitation.id),
                'email': invitation.email,
                'role': invitation.role,
                'tenant': invitation.tenant.name,
                'created_at': invitation.created_at.isoformat(),
                'expires_at': invitation.expires_at.isoformat(),
            },
            message=f'Invitation sent to {email}'
        )


# ADD ValidateInvitationView (public - no auth):

class ValidateInvitationView(APIView):
    """
    Validate invitation token before signup.
    GET /api/invitations/validate?token=xyz
    """
    permission_classes = [AllowAny]
    
    @extend_schema(
        summary="Validate Invitation Token",
        responses={200: inline_serializer(
            name='ValidateInvitationResponse',
            fields={
                'token': serializers.CharField(),
                'email': serializers.EmailField(),
                'role': serializers.CharField(),
                'tenant': serializers.DictField(),
                'expires_at': serializers.CharField()
            }
        )}
    )
    def get(self, request):
        """Validate invitation and return details."""
        token = request.query_params.get('token')
        
        if not token:
            return APIResponse.error(
                message='Invitation token required',
                status_code=400,
                code='validation_error'
            )
        
        try:
            invitation = UserInvitation.objects.get(token=token)
        except UserInvitation.DoesNotExist:
            return APIResponse.error(
                message='Invalid invitation token',
                status_code=404,
                code='not_found'
            )
        
        if not invitation.is_valid():
            return APIResponse.error(
                message='Invitation has expired',
                status_code=403,
                code='invitation_expired'
            )
        
        return APIResponse.success(
            data={
                'token': token,
                'email': invitation.email,
                'role': invitation.role,
                'tenant': {
                    'id': str(invitation.tenant.id),
                    'name': invitation.tenant.name,
                    'slug': invitation.tenant.slug,
                },
                'expires_at': invitation.expires_at.isoformat(),
            },
            message='Invitation is valid'
        )


# ADD JoinWithEmailPasswordView (engineer signup with invitation):

class JoinWithEmailPasswordView(APIView):
    """
    Engineer joins via invitation with email/password.
    POST /api/invitations/join
    {
        "invite_token": "xyz123",
        "password": "SecurePass123",
        "password_confirm": "SecurePass123",
        "first_name": "Bob",
        "last_name": "Engineer"
    }
    """
    permission_classes = [AllowAny]
    
    @extend_schema(
        summary="Join via Invitation",
        request=JoinWithInvitationSerializer,
        responses={201: UserSerializer}
    )
    def post(self, request):
        """Join tenant via invitation with password."""
        serializer = JoinWithInvitationSerializer(data=request.data)
        
        if not serializer.is_valid():
            return APIResponse.error(
                message='Failed to join',
                status_code=400,
                code='validation_error',
                errors=serializer.errors
            )
        
        invitation = serializer.validated_data['invitation']
        password = serializer.validated_data['password']
        first_name = serializer.validated_data.get('first_name', '')
        last_name = serializer.validated_data.get('last_name', '')
        
        try:
            with transaction.atomic():
                # Create user in invited tenant
                user = User.objects.create_user(
                    email=invitation.email,
                    password=password,
                    tenant=invitation.tenant,
                    first_name=first_name,
                    last_name=last_name,
                    role=invitation.role,
                    is_staff=False,
                    email_verified=True  # Email is verified via invitation
                )
                
                # Mark invitation as accepted
                invitation.accept(user)
                
                # Send notification to admin
                try:
                    email_service.send_team_member_joined_notification(invitation)
                except Exception as e:
                    logger.warning(f"Failed to send joined notification: {str(e)}")
                
                # Generate tokens
                access_token, refresh_token = JWTAuthentication.generate_tokens(
                    user,
                    request
                )
                
                logger.info(
                    f"Engineer {user.email} joined {invitation.tenant.name} "
                    f"as {user.role} via email/password"
                )
                
                return APIResponse.created(
                    data=UserSerializer(user).data,
                    message=f'Welcome to {invitation.tenant.name}!',
                    access_token=access_token,
                    refresh_token=refresh_token
                )
        
        except Exception as e:
            logger.error(f"Failed to join via invitation: {str(e)}", exc_info=True)
            return APIResponse.error(
                message='Failed to join organization',
                status_code=400,
                code='join_error'
            )


# ADD ListInvitationsView (admin sees sent invitations):

class ListInvitationsView(APIView):
    """
    List pending invitations for tenant.
    GET /api/invitations?status=pending
    Headers: Authorization: Bearer <access_token>
    """
    permission_classes = [IsAuthenticated, IsTenantAdmin]
    authentication_classes = [JWTAuthentication]
    
    @extend_schema(
        summary="List Pending Invitations",
        responses={200: inline_serializer(
            name='ListInvitationsResponse',
            fields={
                'id': serializers.CharField(),
                'email': serializers.EmailField(),
                'role': serializers.CharField(),
                'status': serializers.CharField(),
                'invited_by': serializers.EmailField(allow_null=True),
                'created_at': serializers.CharField(),
                'expires_at': serializers.CharField(),
                'accepted_at': serializers.CharField(allow_null=True)
            },
            many=True
        )}
    )
    def get(self, request):
        """List invitations for tenant."""
        tenant_id = request.tenant_id
        status = request.query_params.get('status', 'pending')
        
        invitations = UserInvitation.objects.filter(
            tenant_id=tenant_id,
            status=status
        ).order_by('-created_at')
        
        data = [
            {
                'id': str(inv.id),
                'email': inv.email,
                'role': inv.role,
                'status': inv.status,
                'invited_by': inv.invited_by.email if inv.invited_by else None,
                'created_at': inv.created_at.isoformat(),
                'expires_at': inv.expires_at.isoformat(),
                'accepted_at': inv.accepted_at.isoformat() if inv.accepted_at else None,
            }
            for inv in invitations
        ]
        
        return APIResponse.success(
            data=data,
            message=f'Found {len(data)} invitations'
        )


# ADD CancelInvitationView (admin cancels invitation):

class CancelInvitationView(APIView):
    """
    Cancel pending invitation.
    POST /api/invitations/{invitation_id}/cancel
    Headers: Authorization: Bearer <access_token>
    """
    permission_classes = [IsAuthenticated, IsTenantAdmin]
    authentication_classes = [JWTAuthentication]
    
    @extend_schema(
        summary="Cancel Invitation",
        request=None,
        responses={200: OpenApiResponse(description="Invitation cancelled")}
    )
    def post(self, request, invitation_id):
        """Cancel invitation."""
        tenant_id = request.tenant_id
        
        try:
            invitation = UserInvitation.objects.get(
                id=invitation_id,
                tenant_id=tenant_id
            )
        except UserInvitation.DoesNotExist:
            raise NotFoundException('Invitation not found')
        
        if invitation.status != 'pending':
            return APIResponse.error(
                message=f'Cannot cancel invitation with status: {invitation.status}',
                status_code=400,
                code='invalid_status'
            )
        
        invitation.cancel()
        
        logger.info(f"Invitation {invitation_id} cancelled")
        
        return APIResponse.success(
            message='Invitation cancelled'
        )


# ADD ResendInvitationView (admin resends invitation):

class ResendInvitationView(APIView):
    """
    Resend invitation email.
    POST /api/invitations/{invitation_id}/resend
    Headers: Authorization: Bearer <access_token>
    """
    permission_classes = [IsAuthenticated, IsTenantAdmin]
    authentication_classes = [JWTAuthentication]
    
    @extend_schema(
        summary="Resend Invitation Email",
        request=None,
        responses={200: OpenApiResponse(description="Invitation resent")}
    )
    def post(self, request, invitation_id):
        """Resend invitation email."""
        tenant_id = request.tenant_id
        frontend_url = request.data.get('frontend_url', settings.FRONTEND_URL)
        
        try:
            invitation = UserInvitation.objects.get(
                id=invitation_id,
                tenant_id=tenant_id
            )
        except UserInvitation.DoesNotExist:
            raise NotFoundException('Invitation not found')
        
        if invitation.status != 'pending':
            return APIResponse.error(
                message=f'Cannot resend invitation with status: {invitation.status}',
                status_code=400,
                code='invalid_status'
            )
        
        # Check if invitation expired
        if not invitation.is_valid():
            # Reset expiration
            invitation.expires_at = timezone.now() + timedelta(days=7)
            invitation.save()
        
        try:
            email_service.send_invitation_reminder_email(invitation, frontend_url)
        except Exception as e:
            logger.error(f"Failed to resend invitation: {str(e)}")
            return APIResponse.error(
                message='Failed to resend invitation',
                status_code=500,
                code='email_error'
            )
        
        logger.info(f"Invitation {invitation_id} resent to {invitation.email}")
        
        return APIResponse.success(
            message=f'Invitation resent to {invitation.email}'
        )
    


class SetupMFAView(APIView):
    """
    Start MFA setup - generate secret & QR code.
    
    GET /api/auth/mfa/setup
    Headers: Authorization: Bearer <access_token>
    
    Response:
    {
        "secret": "JBSWY3DPEBLW64TMMQ======",
        "qr_code": "data:image/png;base64,iVBORw0KGgoAAAANS...",
        "setup_url": "otpauth://totp/..."
    }
    """
    permission_classes = [IsAuthenticated]
    authentication_classes = [JWTAuthentication]
    
    def get(self, request):
        """Generate TOTP secret and QR code."""
        user_id = request.user_id
        user = User.objects.get(id=user_id)
        
        # Delete existing unconfirmed device
        TOTPDevice.objects.filter(user=user, is_confirmed=False).delete()
        
        # Generate new secret
        secret = TOTPDevice.generate_secret()
        
        # Create device (not confirmed yet)
        device = TOTPDevice.objects.create(
            user=user,
            secret_key=secret,
            is_confirmed=False
        )
        
        # Get QR code URL
        qr_url = device.get_qr_code(user.email)
        
        # Generate QR code image
        qr = qrcode.QRCode()
        qr.add_data(qr_url)
        qr.make()
        
        img = qr.make_image(fill_color="black", back_color="white")
        img_bytes = BytesIO()
        img.save(img_bytes, format='PNG')
        img_base64 = base64.b64encode(img_bytes.getvalue()).decode()
        
        logger.info(f"MFA setup started for {user.email}")
        
        return APIResponse.success(
            data={
                'secret': secret,
                'qr_code': f'data:image/png;base64,{img_base64}',
                'setup_url': qr_url,
                'message': 'Scan QR code with Google Authenticator, Authy, or Microsoft Authenticator'
            }
        )


class ConfirmMFAView(APIView):
    """
    Verify TOTP code to confirm MFA setup.
    
    POST /api/auth/mfa/confirm
    Headers: Authorization: Bearer <access_token>
    {
        "code": "123456"
    }
    
    Response: MFA now active, 10 backup codes generated
    """
    permission_classes = [IsAuthenticated]
    authentication_classes = [JWTAuthentication]
    
    def post(self, request):
        """Confirm MFA by verifying TOTP code."""
        user_id = request.user_id
        user = User.objects.get(id=user_id)
        
        serializer = ConfirmMFASerializer(data=request.data)
        if not serializer.is_valid():
            return APIResponse.error(
                message='Invalid code',
                status_code=400,
                code='validation_error',
                errors=serializer.errors
            )
        
        code = serializer.validated_data['code']
        
        try:
            # Get unconfirmed device
            device = TOTPDevice.objects.get(
                user=user,
                is_confirmed=False
            )
        except TOTPDevice.DoesNotExist:
            return APIResponse.error(
                message='MFA setup not started. Please go to /api/auth/mfa/setup first.',
                status_code=400,
                code='setup_required'
            )
        
        # Verify code
        if not device.verify_token(code):
            logger.warning(f"Invalid MFA code for {user.email}")
            return APIResponse.error(
                message='Invalid code. Please check your authenticator app.',
                status_code=400,
                code='invalid_code'
            )
        
        try:
            with transaction.atomic():
                # Confirm device
                device.confirm()
                
                # Delete old backup codes
                BackupCode.objects.filter(user=user).delete()
                
                # Generate new backup codes
                codes = BackupCode.generate_codes(count=10)
                backup_codes = []
                
                for code in codes:
                    code_hash = BackupCode.hash_code(code)
                    BackupCode.objects.create(
                        user=user,
                        code_hash=code_hash
                    )
                    backup_codes.append(code)
                
                logger.info(f"MFA confirmed for {user.email}")
                
                return APIResponse.success(
                    data={
                        'message': 'MFA enabled successfully',
                        'backup_codes': backup_codes,
                        'warning': 'Save these backup codes in a safe place. Each can be used once if you lose your authenticator.'
                    }
                )
        
        except Exception as e:
            logger.error(f"MFA confirmation error: {str(e)}")
            return APIResponse.error(
                message='Failed to confirm MFA',
                status_code=500,
                code='mfa_error'
            )


class VerifyMFATokenView(APIView):
    """
    Verify TOTP code and exchange MFA token for access tokens.
    
    POST /api/auth/mfa/verify
    {
        "mfa_token": "temporary-mfa-token",
        "code": "123456"
    }
    
    Response: access_token, refresh_token
    """
    permission_classes = [AllowAny]
    
    @extend_schema(
        summary="Verify MFA Code",
        request=VerifyMFATokenSerializer,
        responses={200: inline_serializer(
            name='MFAVerifyResponse',
            fields={
                'access_token': serializers.CharField(),
                'refresh_token': serializers.CharField()
            }
        )}
    )
    def post(self, request):
        """Verify TOTP code and return access tokens."""
        serializer = VerifyMFATokenSerializer(data=request.data)
        
        if not serializer.is_valid():
            return APIResponse.error(
                message='Verification failed',
                status_code=400,
                code='validation_error',
                errors=serializer.errors
            )
        
        mfa_token = serializer.validated_data['mfa_token']
        code = serializer.validated_data['code']
        
        # Verify MFA token exists and is valid
        try:
            mfa_token_obj = MFAVerificationToken.objects.get(token=mfa_token)
        except MFAVerificationToken.DoesNotExist:
            logger.warning(f"Invalid MFA token used")
            return APIResponse.error(
                message='Invalid MFA token. Please log in again.',
                status_code=400,
                code='invalid_token'
            )
        
        if not mfa_token_obj.is_valid():
            mfa_token_obj.delete()
            return APIResponse.error(
                message='MFA token expired. Please log in again.',
                status_code=400,
                code='token_expired'
            )
        
        user = mfa_token_obj.user
        
        # Check rate limiting on MFA attempts
        mfa_attempts = cache_manager.get_mfa_attempts(user.email)
        if mfa_attempts >= 5:
            # Lock MFA verification for 15 minutes
            cache_manager.lock_mfa_verification(user.email, 15)
            logger.warning(f"MFA verification rate limited for {user.email}")
            return APIResponse.error(
                message='Too many failed attempts. Try again in 15 minutes.',
                status_code=429,
                code='rate_limited'
            )
        
        # Try TOTP code first
        device = TOTPDevice.objects.get(user=user)
        
        if device.verify_token(code):
            # Valid TOTP code
            cache_manager.reset_mfa_attempts(user.email)
            mfa_token_obj.delete()
            
            # Generate access/refresh tokens
            access_token, refresh_token = JWTAuthentication.generate_tokens(
                user,
                request
            )
            
            logger.info(f"MFA verification success for {user.email}")
            
            return APIResponse.success(
                message='MFA verified. Welcome!',
                access_token=access_token,
                refresh_token=refresh_token
            )
        
        # Try backup code
        backup = BackupCode.objects.filter(
            user=user,
            is_used=False
        ).first()
        
        if backup:
            from django.contrib.auth.hashers import check_password
            if check_password(code, backup.code_hash):
                # Valid backup code
                backup.use()
                cache_manager.reset_mfa_attempts(user.email)
                mfa_token_obj.delete()
                
                access_token, refresh_token = JWTAuthentication.generate_tokens(
                    user,
                    request
                )
                
                logger.warning(f"Backup code used by {user.email}")
                
                return APIResponse.success(
                    message='Backup code verified. Generate new backup codes from settings.',
                    access_token=access_token,
                    refresh_token=refresh_token
                )
        
        # Invalid code
        cache_manager.increment_mfa_attempts(user.email)
        logger.warning(f"Invalid MFA code for {user.email}")
        
        return APIResponse.error(
            message='Invalid code. Check your authenticator app.',
            status_code=400,
            code='invalid_code'
        )


class DisableMFAView(APIView):
    """
    Disable MFA - requires password + TOTP verification.
    
    POST /api/auth/mfa/disable
    Headers: Authorization: Bearer <access_token>
    {
        "password": "user_password",
        "code": "123456"  // TOTP code
    }
    """
    permission_classes = [IsAuthenticated]
    authentication_classes = [JWTAuthentication]
    
    def post(self, request):
        """Disable MFA for user."""
        user_id = request.user_id
        user = User.objects.get(id=user_id)
        
        serializer = DisableMFASerializer(data=request.data)
        if not serializer.is_valid():
            return APIResponse.error(
                message='Disable MFA failed',
                status_code=400,
                code='validation_error',
                errors=serializer.errors
            )
        
        password = serializer.validated_data['password']
        code = serializer.validated_data.get('code')
        
        # Verify password
        if not user.check_password(password):
            logger.warning(f"Password verification failed for MFA disable - {user.email}")
            return APIResponse.error(
                message='Incorrect password',
                status_code=400,
                code='auth_error'
            )
        
        # Verify MFA code if MFA is active
        try:
            device = TOTPDevice.objects.get(user=user, is_confirmed=True)
        except TOTPDevice.DoesNotExist:
            return APIResponse.error(
                message='MFA not enabled',
                status_code=400,
                code='mfa_not_enabled'
            )
        
        if not code:
            return APIResponse.error(
                message='MFA code required to disable MFA',
                status_code=400,
                code='mfa_code_required'
            )
        
        # Verify code
        if not device.verify_token(code):
            logger.warning(f"Invalid MFA code during disable - {user.email}")
            return APIResponse.error(
                message='Invalid MFA code',
                status_code=400,
                code='invalid_code'
            )
        
        # Disable MFA
        try:
            with transaction.atomic():
                device.delete()
                BackupCode.objects.filter(user=user).delete()
                
                logger.info(f"MFA disabled for {user.email}")
                
                return APIResponse.success(
                    message='MFA disabled successfully'
                )
        except Exception as e:
            logger.error(f"MFA disable error: {str(e)}")
            return APIResponse.error(
                message='Failed to disable MFA',
                status_code=500,
                code='mfa_error'
            )