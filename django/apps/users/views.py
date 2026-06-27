import logging

from core.exceptions import RateLimitException
from core.permissions import IsTenantAdmin
from core.responses import APIResponse
from core.utils.errors import extract_error_message
from django.conf import settings
from drf_spectacular.utils import OpenApiResponse, extend_schema, inline_serializer
from rest_framework import serializers
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.views import APIView

from .authentication import JWTAuthentication
from .cache import cache_manager
from .models import User
from .serializers import (
    ChangePasswordSerializer,
    ConfirmMFASerializer,
    DisableMFASerializer,
    ForgotPasswordSerializer,
    GitHubOAuthCallbackSerializer,
    GoogleOAuthCallbackSerializer,
    InviteEngineerSerializer,
    JoinWithInvitationSerializer,
    LoginSerializer,
    RegisterSerializer,
    ResendVerificationEmailSerializer,
    ResetPasswordSerializer,
    TokenRefreshSerializer,
    UserSerializer,
    ValidateInvitationTokenSerializer,
    VerifyEmailSerializer,
    VerifyMFATokenSerializer,
)
from .services.auth_service import AuthService
from .services.email_verification_service import EmailVerificationService
from .services.invitation_service import InvitationService
from .services.mfa_service import MFAService
from .services.oauth_service_layer import OAuthServiceLayer
from .services.password_service import PasswordService
from .services.session_service import SessionService

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Health Check
# ---------------------------------------------------------------------------


class HealthCheckView(APIView):
    """Health check endpoint - no auth required"""

    permission_classes = [AllowAny]

    @extend_schema(
        summary="API Health Check",
        responses={
            200: inline_serializer(
                name="HealthCheckResponse", fields={"status": serializers.CharField()}
            )
        },
    )
    def get(self, request):
        return APIResponse.success(
            data={"status": "healthy"}, message="Server is healthy"
        )


# ---------------------------------------------------------------------------
# Registration & Login
# ---------------------------------------------------------------------------


class RegisterView(APIView):
    """Email/password owner registration. Creates new tenant + owner account."""

    permission_classes = [AllowAny]

    @extend_schema(
        summary="Register Owner & Tenant",
        request=RegisterSerializer,
        responses={201: UserSerializer},
    )
    def post(self, request):
        frontend_url = request.data.get("frontend_url", settings.FRONTEND_URL)
        serializer = RegisterSerializer(data=request.data)

        if not serializer.is_valid():
            return APIResponse.error(
                message="Registration failed",
                status_code=400,
                code="validation_error",
                errors=serializer.errors,
            )

        user = serializer.save()
        AuthService.register(user, frontend_url)

        return APIResponse.created(
            data=UserSerializer(user).data,
            message="Owner account created. Please check your email to verify.",
            access_token=None,
            refresh_token=None,
        )


class LoginView(APIView):
    """Login user with session tracking."""

    permission_classes = [AllowAny]

    @extend_schema(
        summary="Login User", request=LoginSerializer, responses={200: UserSerializer}
    )
    def post(self, request):
        email = request.data.get("email", "").lower()
        ip = cache_manager.get_client_ip(request)

        is_blocked, reason = cache_manager.is_login_blocked(email, ip)
        if is_blocked:
            raise RateLimitException(reason)

        serializer = LoginSerializer(data=request.data)

        if serializer.is_valid():
            user = serializer.validated_data["user"]
            cache_manager.clear_login_failures(email, ip)
            return AuthService.login(user, request)

        # Handle unverified email — silently resend verification
        email_error = serializer.errors.get("email", [None])[0]
        if email_error == "Please verify your email before logging in.":
            frontend_url = request.data.get("frontend_url", settings.FRONTEND_URL)
            AuthService.handle_unverified_login(email, frontend_url)

        return AuthService.record_login_failure(email, ip, serializer, request)


class TokenRefreshView(APIView):
    """Refresh access token using refresh token."""

    permission_classes = [AllowAny]

    @extend_schema(
        summary="Refresh Access Token",
        request=TokenRefreshSerializer,
        responses={
            200: inline_serializer(
                name="TokenRefreshResponse",
                fields={
                    "access_token": serializers.CharField(),
                    "refresh_token": serializers.CharField(),
                },
            )
        },
    )
    def post(self, request):
        serializer = TokenRefreshSerializer(data=request.data)

        if serializer.is_valid():
            access_token, refresh_token = AuthService.refresh_token(
                serializer.validated_data["refresh_token"]
            )
            return APIResponse.success(
                message="Token refreshed successfully.",
                access_token=access_token,
                refresh_token=refresh_token,
            )

        return APIResponse.error(
            message="Token refresh failed",
            status_code=400,
            code="validation_error",
            errors=serializer.errors,
        )


class MeView(APIView):
    """Get current user profile."""

    permission_classes = [IsAuthenticated]
    authentication_classes = [JWTAuthentication]

    @extend_schema(summary="Get Current User Profile", responses={200: UserSerializer})
    def get(self, request):
        return APIResponse.success(
            data=UserSerializer(request.user).data, message="User profile retrieved."
        )


class ListTeamMembersView(APIView):
    """
    List all active team members (users) for the current tenant.
    Requires authentication.
    """

    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="List team members",
        description="Returns all users belonging to the current user's workspace.",
        responses={200: UserSerializer(many=True)},
    )
    def get(self, request):
        # Fetch all active users in the same tenant, ordered by creation date or role
        users = User.objects.filter(
            tenant=request.user.tenant, is_active=True
        ).order_by("created_at")
        serializer = UserSerializer(users, many=True)
        return APIResponse.success(
            data=serializer.data, message="Team members retrieved successfully."
        )


class LogoutView(APIView):
    """Logout user - revoke token and session."""

    permission_classes = [IsAuthenticated]
    authentication_classes = [JWTAuthentication]

    @extend_schema(
        summary="Logout User",
        request=None,
        responses={200: OpenApiResponse(description="Logged out successfully")},
    )
    def post(self, request):
        try:
            AuthService.logout(request)
            return APIResponse.success(message="Logged out successfully.")
        except Exception as e:
            logger.error(f"Logout error: {str(e)}")
            raise


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


class SessionListView(APIView):
    """List all active sessions for current user."""

    permission_classes = [IsAuthenticated]
    authentication_classes = [JWTAuthentication]

    @extend_schema(
        summary="List User Sessions",
        responses={
            200: inline_serializer(
                name="SessionListResponse",
                fields={
                    "id": serializers.CharField(),
                    "device_name": serializers.CharField(),
                    "ip_address": serializers.CharField(),
                    "last_activity": serializers.DateTimeField(),
                    "created_at": serializers.DateTimeField(),
                    "expires_at": serializers.DateTimeField(),
                },
                many=True,
            )
        },
    )
    def get(self, request):
        data = SessionService.get_active_sessions(request.user_id)
        return APIResponse.success(
            data=data, message="Sessions retrieved successfully."
        )


class RevokeSessionView(APIView):
    """Revoke a specific session (force logout from device)."""

    permission_classes = [IsAuthenticated]
    authentication_classes = [JWTAuthentication]

    @extend_schema(
        summary="Revoke User Session",
        request=None,
        responses={200: OpenApiResponse(description="Session revoked successfully")},
    )
    def post(self, request, session_id):
        try:
            SessionService.revoke_session(
                session_id, request.user_id, request.user_email
            )
            return APIResponse.success(message="Session revoked successfully.")
        except Exception as e:
            logger.error(f"Revoke session error: {str(e)}")
            raise


# ---------------------------------------------------------------------------
# Email Verification
# ---------------------------------------------------------------------------


class VerifyEmailView(APIView):
    """Verify email with token."""

    permission_classes = [AllowAny]

    @extend_schema(
        summary="Verify Email",
        request=VerifyEmailSerializer,
        responses={200: UserSerializer},
    )
    def post(self, request):
        serializer = VerifyEmailSerializer(data=request.data)

        if serializer.is_valid():
            verification = serializer.validated_data["token"]
            user, access_token, refresh_token = EmailVerificationService.verify_email(
                verification, request
            )
            return APIResponse.success(
                data=UserSerializer(user).data,
                message="Email verified successfully.",
                access_token=access_token,
                refresh_token=refresh_token,
            )

        return APIResponse.error(
            message="Email verification failed",
            status_code=400,
            code="verification_error",
            errors=serializer.errors,
        )


class ResendVerificationEmailView(APIView):
    """Resend verification email."""

    permission_classes = [AllowAny]

    @extend_schema(
        summary="Resend Verification Email",
        request=ResendVerificationEmailSerializer,
        responses={200: OpenApiResponse(description="Verification email sent")},
    )
    def post(self, request):
        serializer = ResendVerificationEmailSerializer(data=request.data)

        if not serializer.is_valid():
            return APIResponse.error(
                message="Failed to resend verification",
                status_code=400,
                code="validation_error",
                errors=serializer.errors,
            )

        email = serializer.validated_data["email"]
        already_verified = EmailVerificationService.resend_verification(email)

        if already_verified:
            return APIResponse.error(
                message="Email already verified.",
                status_code=400,
                code="already_verified",
            )

        return APIResponse.success(
            message=(
                "If an account with this email exists, "
                "a verification email has been sent."
            )
        )


# ---------------------------------------------------------------------------
# Password
# ---------------------------------------------------------------------------


class ForgotPasswordView(APIView):
    """Request password reset."""

    permission_classes = [AllowAny]

    @extend_schema(
        summary="Forgot Password",
        request=ForgotPasswordSerializer,
        responses={
            200: OpenApiResponse(description="If email exists, reset link sent")
        },
    )
    def post(self, request):
        serializer = ForgotPasswordSerializer(data=request.data)

        if not serializer.is_valid():
            return APIResponse.error(
                message="Invalid email",
                status_code=400,
                code="validation_error",
                errors=serializer.errors,
            )

        PasswordService.forgot_password(serializer.validated_data["email"], request)

        return APIResponse.success(
            message=("If this email exists, " "you will receive a password reset link.")
        )


class ResetPasswordView(APIView):
    """Reset password with token."""

    permission_classes = [AllowAny]

    @extend_schema(
        summary="Reset Password",
        request=ResetPasswordSerializer,
        responses={200: OpenApiResponse(description="Password reset successfully")},
    )
    def post(self, request):
        serializer = ResetPasswordSerializer(data=request.data)

        if serializer.is_valid():
            PasswordService.reset_password(
                serializer.validated_data["token"],
                serializer.validated_data["new_password"],
            )
            return APIResponse.success(
                message="Password reset successfully. Please log in with your new password."
            )

        return APIResponse.error(
            message="Password reset failed",
            status_code=400,
            code="validation_error",
            errors=serializer.errors,
        )


class ChangePasswordView(APIView):
    """Change password (authenticated user)."""

    permission_classes = [IsAuthenticated]
    authentication_classes = [JWTAuthentication]

    @extend_schema(
        summary="Change Password",
        request=ChangePasswordSerializer,
        responses={200: OpenApiResponse(description="Password changed successfully")},
    )
    def post(self, request):
        serializer = ChangePasswordSerializer(data=request.data)

        if serializer.is_valid():
            success, error = PasswordService.change_password(
                request.user,
                serializer.validated_data["current_password"],
                serializer.validated_data["new_password"],
            )
            if not success:
                return APIResponse.error(
                    message=error, status_code=400, code="auth_error"
                )
            return APIResponse.success(
                message="Password changed successfully. Please log in again."
            )

        return APIResponse.error(
            message="Password change failed",
            status_code=400,
            code="validation_error",
            errors=serializer.errors,
        )


# ---------------------------------------------------------------------------
# OAuth
# ---------------------------------------------------------------------------


class GoogleOAuthCallbackView(APIView):
    """Google OAuth callback."""

    permission_classes = [AllowAny]

    @extend_schema(
        summary="Google OAuth Callback",
        request=GoogleOAuthCallbackSerializer,
        responses={200: UserSerializer},
    )
    def post(self, request):
        serializer = GoogleOAuthCallbackSerializer(data=request.data)

        if not serializer.is_valid():
            return APIResponse.error(
                message="OAuth authentication failed",
                status_code=400,
                code="validation_error",
                errors=serializer.errors,
            )

        try:
            result = OAuthServiceLayer.handle_google_oauth(
                access_token=serializer.validated_data["code"],
                invitation=serializer.validated_data.get("invite_token"),
                request=request,
            )

            logger.info(result["log_message"])

            if result["requires_mfa"]:
                return APIResponse.success(
                    message="MFA required. Please verify with authenticator app.",
                    mfa_token=result["mfa_token"],
                    requires_mfa=True,
                )

            return APIResponse.success(
                data=UserSerializer(result["user"]).data,
                message="Authenticated via Google.",
                access_token=result["access_token"],
                refresh_token=result["refresh_token"],
            )

        except ValidationError as e:
            error_msg = extract_error_message(e)
            logger.warning(f"Google OAuth error: {error_msg}")
            return APIResponse.error(
                message=error_msg, status_code=400, code="auth_error"
            )
        except Exception as e:
            logger.error(f"Google OAuth error: {str(e)}", exc_info=True)
            return APIResponse.error(
                message="Google authentication failed",
                status_code=400,
                code="oauth_error",
            )


class GitHubOAuthCallbackView(APIView):
    """GitHub OAuth callback."""

    permission_classes = [AllowAny]

    @extend_schema(
        summary="GitHub OAuth Callback",
        request=GitHubOAuthCallbackSerializer,
        responses={200: UserSerializer},
    )
    def post(self, request):
        serializer = GitHubOAuthCallbackSerializer(data=request.data)

        if not serializer.is_valid():
            return APIResponse.error(
                message="OAuth authentication failed",
                status_code=400,
                code="validation_error",
                errors=serializer.errors,
            )

        try:
            result = OAuthServiceLayer.handle_github_oauth(
                access_token=serializer.validated_data["code"],
                invitation=serializer.validated_data.get("invite_token"),
                request=request,
            )

            logger.info(result["log_message"])

            if result["requires_mfa"]:
                return APIResponse.success(
                    message="MFA required. Please verify with authenticator app.",
                    mfa_token=result["mfa_token"],
                    requires_mfa=True,
                )

            return APIResponse.success(
                data=UserSerializer(result["user"]).data,
                message="Authenticated via GitHub.",
                access_token=result["access_token"],
                refresh_token=result["refresh_token"],
            )

        except ValidationError as e:
            error_msg = extract_error_message(e)
            logger.warning(f"GitHub OAuth error: {error_msg}")
            return APIResponse.error(
                message=error_msg, status_code=400, code="auth_error"
            )
        except Exception as e:
            logger.error(f"GitHub OAuth error: {str(e)}", exc_info=True)
            return APIResponse.error(
                message="GitHub authentication failed",
                status_code=400,
                code="oauth_error",
            )


# ---------------------------------------------------------------------------
# Invitations
# ---------------------------------------------------------------------------


class InviteEngineerView(APIView):
    """Admin invites engineer to tenant."""

    permission_classes = [IsAuthenticated, IsTenantAdmin]
    authentication_classes = [JWTAuthentication]

    @extend_schema(
        summary="Invite Engineer to Tenant",
        request=InviteEngineerSerializer,
        responses={
            201: inline_serializer(
                name="InviteResponse",
                fields={
                    "id": serializers.CharField(),
                    "email": serializers.EmailField(),
                    "role": serializers.CharField(),
                    "tenant": serializers.CharField(),
                    "created_at": serializers.CharField(),
                    "expires_at": serializers.CharField(),
                },
            )
        },
    )
    def post(self, request):
        try:
            inviter = User.objects.get(id=request.user_id)
        except User.DoesNotExist:
            from core.exceptions import NotFoundException

            raise NotFoundException("User not found")

        serializer = InviteEngineerSerializer(data=request.data)

        if not serializer.is_valid():
            return APIResponse.error(
                message="Invitation failed",
                status_code=400,
                code="validation_error",
                errors=serializer.errors,
            )

        frontend_url = request.data.get("frontend_url", settings.FRONTEND_URL)

        invitation, error = InvitationService.send_invitation(
            tenant_id=request.tenant_id,
            inviter=inviter,
            email=serializer.validated_data["email"],
            role=serializer.validated_data["role"],
            frontend_url=frontend_url,
        )

        if error == "email_failed":
            return APIResponse.error(
                message="Invitation created but email delivery failed. Please retry.",
                status_code=500,
                code="email_error",
            )
        if error:
            return APIResponse.error(message=error, status_code=409, code="conflict")

        return APIResponse.created(
            data={
                "id": str(invitation.id),
                "email": invitation.email,
                "role": invitation.role,
                "tenant": invitation.tenant.name,
                "created_at": invitation.created_at.isoformat(),
                "expires_at": invitation.expires_at.isoformat(),
            },
            message=f"Invitation sent to {invitation.email}",
        )


class ValidateInvitationView(APIView):
    """Validate invitation token before signup."""

    permission_classes = [AllowAny]

    @extend_schema(
        summary="Validate Invitation Token",
        responses={
            200: inline_serializer(
                name="ValidateInvitationResponse",
                fields={
                    "token": serializers.CharField(),
                    "email": serializers.EmailField(),
                    "role": serializers.CharField(),
                    "tenant": serializers.DictField(),
                    "expires_at": serializers.CharField(),
                },
            )
        },
    )
    def get(self, request):
        token = request.query_params.get("token")
        invitation, error = InvitationService.validate_invitation(token)

        if error == "missing_token":
            return APIResponse.error(
                message="Invitation token required",
                status_code=400,
                code="validation_error",
            )
        if error == "not_found":
            return APIResponse.error(
                message="Invalid invitation token", status_code=404, code="not_found"
            )
        if error == "expired":
            return APIResponse.error(
                message="Invitation has expired",
                status_code=403,
                code="invitation_expired",
            )

        return APIResponse.success(
            data={
                "token": token,
                "email": invitation.email,
                "role": invitation.role,
                "tenant": {
                    "id": str(invitation.tenant.id),
                    "name": invitation.tenant.name,
                    "slug": invitation.tenant.slug,
                },
                "expires_at": invitation.expires_at.isoformat(),
            },
            message="Invitation is valid",
        )


class JoinWithEmailPasswordView(APIView):
    """Engineer joins via invitation with email/password."""

    permission_classes = [AllowAny]

    @extend_schema(
        summary="Join via Invitation",
        request=JoinWithInvitationSerializer,
        responses={201: UserSerializer},
    )
    def post(self, request):
        serializer = JoinWithInvitationSerializer(data=request.data)

        if not serializer.is_valid():
            return APIResponse.error(
                message="Failed to join",
                status_code=400,
                code="validation_error",
                errors=serializer.errors,
            )

        try:
            user, access_token, refresh_token = InvitationService.join_with_password(
                invitation=serializer.validated_data["invitation"],
                password=serializer.validated_data["password"],
                first_name=serializer.validated_data.get("first_name", ""),
                last_name=serializer.validated_data.get("last_name", ""),
                request=request,
            )
            return APIResponse.created(
                data=UserSerializer(user).data,
                message=f"Welcome to {user.tenant.name}!",
                access_token=access_token,
                refresh_token=refresh_token,
            )
        except Exception as e:
            logger.error(f"Failed to join via invitation: {str(e)}", exc_info=True)
            return APIResponse.error(
                message="Failed to join organization",
                status_code=400,
                code="join_error",
            )


class ListInvitationsView(APIView):
    """List pending invitations for tenant."""

    permission_classes = [IsAuthenticated, IsTenantAdmin]
    authentication_classes = [JWTAuthentication]

    @extend_schema(
        summary="List Pending Invitations",
        responses={
            200: inline_serializer(
                name="ListInvitationsResponse",
                fields={
                    "id": serializers.CharField(),
                    "email": serializers.EmailField(),
                    "role": serializers.CharField(),
                    "status": serializers.CharField(),
                    "invited_by": serializers.EmailField(allow_null=True),
                    "created_at": serializers.CharField(),
                    "expires_at": serializers.CharField(),
                    "accepted_at": serializers.CharField(allow_null=True),
                },
                many=True,
            )
        },
    )
    def get(self, request):
        status = request.query_params.get("status", "pending")
        data = InvitationService.list_invitations(request.tenant_id, status)
        return APIResponse.success(data=data, message=f"Found {len(data)} invitations")


class CancelInvitationView(APIView):
    """Cancel pending invitation."""

    permission_classes = [IsAuthenticated, IsTenantAdmin]
    authentication_classes = [JWTAuthentication]

    @extend_schema(
        summary="Cancel Invitation",
        request=None,
        responses={200: OpenApiResponse(description="Invitation cancelled")},
    )
    def post(self, request, invitation_id):
        invitation, error = InvitationService.cancel_invitation(
            invitation_id, request.tenant_id
        )
        if error:
            return APIResponse.error(
                message=error, status_code=400, code="invalid_status"
            )
        return APIResponse.success(message="Invitation cancelled")


class ResendInvitationView(APIView):
    """Resend invitation email."""

    permission_classes = [IsAuthenticated, IsTenantAdmin]
    authentication_classes = [JWTAuthentication]

    @extend_schema(
        summary="Resend Invitation Email",
        request=None,
        responses={200: OpenApiResponse(description="Invitation resent")},
    )
    def post(self, request, invitation_id):
        frontend_url = request.data.get("frontend_url", settings.FRONTEND_URL)
        success, error = InvitationService.resend_invitation(
            invitation_id, request.tenant_id, frontend_url
        )
        if not success:
            return APIResponse.error(message=error, status_code=500, code="email_error")
        from .models import UserInvitation

        inv = UserInvitation.objects.get(id=invitation_id)
        return APIResponse.success(message=f"Invitation resent to {inv.email}")


# ---------------------------------------------------------------------------
# MFA
# ---------------------------------------------------------------------------


class SetupMFAView(APIView):
    """Start MFA setup - generate secret & QR code."""

    permission_classes = [IsAuthenticated]
    authentication_classes = [JWTAuthentication]

    @extend_schema(
        summary="Setup MFA",
        responses={200: OpenApiResponse(description="MFA Setup details")},
    )
    def get(self, request):
        data, error = MFAService.setup(request.user)

        if error == "already_enabled":
            return APIResponse.error(
                message="MFA is already enabled for this account.",
                status_code=400,
                code="mfa_already_enabled",
            )

        return APIResponse.success(data=data)


class ConfirmMFAView(APIView):
    """Verify TOTP code to confirm MFA setup."""

    permission_classes = [IsAuthenticated]
    authentication_classes = [JWTAuthentication]

    @extend_schema(
        summary="Confirm MFA Setup",
        request=ConfirmMFASerializer,
        responses={200: OpenApiResponse(description="MFA enabled successfully")},
    )
    def post(self, request):
        serializer = ConfirmMFASerializer(data=request.data)
        if not serializer.is_valid():
            return APIResponse.error(
                message="Invalid code",
                status_code=400,
                code="validation_error",
                errors=serializer.errors,
            )

        backup_codes, error = MFAService.confirm(
            request.user, serializer.validated_data["code"]
        )

        if error == "setup_required":
            return APIResponse.error(
                message="MFA setup not started. Please go to /api/auth/mfa/setup first.",
                status_code=400,
                code="setup_required",
            )
        if error == "invalid_code":
            return APIResponse.error(
                message="Invalid code. Please check your authenticator app.",
                status_code=400,
                code="invalid_code",
            )
        if error == "mfa_error":
            return APIResponse.error(
                message="Failed to confirm MFA", status_code=500, code="mfa_error"
            )

        return APIResponse.success(
            data={
                "message": "MFA enabled successfully",
                "backup_codes": backup_codes,
                "warning": "Save these backup codes in a safe place. Each can be used once if you lose your authenticator.",
            }
        )


class VerifyMFATokenView(APIView):
    """Verify TOTP code and exchange MFA token for access tokens."""

    permission_classes = [AllowAny]

    @extend_schema(
        summary="Verify MFA Code",
        request=VerifyMFATokenSerializer,
        responses={
            200: inline_serializer(
                name="MFAVerifyResponse",
                fields={
                    "access_token": serializers.CharField(),
                    "refresh_token": serializers.CharField(),
                },
            )
        },
    )
    def post(self, request):
        serializer = VerifyMFATokenSerializer(data=request.data)

        if not serializer.is_valid():
            return APIResponse.error(
                message="Verification failed",
                status_code=400,
                code="validation_error",
                errors=serializer.errors,
            )

        result, error = MFAService.verify_token(
            mfa_token=serializer.validated_data["mfa_token"],
            code=serializer.validated_data["code"],
            request=request,
        )

        error_map = {
            "invalid_token": (
                "Invalid MFA token. Please log in again.",
                400,
                "invalid_token",
            ),
            "token_expired": (
                "MFA token expired. Please log in again.",
                400,
                "token_expired",
            ),
            "rate_limited": (
                "Too many failed attempts. Try again in 15 minutes.",
                429,
                "rate_limited",
            ),
            "invalid_code": (
                "Invalid code. Check your authenticator app.",
                400,
                "invalid_code",
            ),
        }

        if error:
            msg, status, code = error_map.get(
                error, ("Verification failed.", 400, "mfa_error")
            )
            return APIResponse.error(message=msg, status_code=status, code=code)

        if result.get("used_backup_code"):
            return APIResponse.success(
                message="Backup code verified. Generate new backup codes from settings.",
                access_token=result["access_token"],
                refresh_token=result["refresh_token"],
            )

        return APIResponse.success(
            data=UserSerializer(result["user"]).data,
            message="MFA verified. Welcome!",
            access_token=result["access_token"],
            refresh_token=result["refresh_token"],
        )


class DisableMFAView(APIView):
    """Disable MFA - requires password + TOTP verification."""

    permission_classes = [IsAuthenticated]
    authentication_classes = [JWTAuthentication]

    @extend_schema(
        summary="Disable MFA",
        request=DisableMFASerializer,
        responses={200: OpenApiResponse(description="MFA disabled successfully")},
    )
    def post(self, request):
        serializer = DisableMFASerializer(data=request.data)
        if not serializer.is_valid():
            return APIResponse.error(
                message="Disable MFA failed",
                status_code=400,
                code="validation_error",
                errors=serializer.errors,
            )

        success, error = MFAService.disable(
            user=request.user,
            password=serializer.validated_data["password"],
            code=serializer.validated_data.get("code"),
        )

        error_map = {
            "wrong_password": ("Incorrect password", 400, "auth_error"),
            "mfa_not_enabled": ("MFA not enabled", 400, "mfa_not_enabled"),
            "code_required": (
                "MFA code required to disable MFA",
                400,
                "mfa_code_required",
            ),
            "invalid_code": ("Invalid MFA code", 400, "invalid_code"),
            "mfa_error": ("Failed to disable MFA", 500, "mfa_error"),
        }

        if not success:
            msg, status, code = error_map.get(
                error, ("Failed to disable MFA", 500, "mfa_error")
            )
            return APIResponse.error(message=msg, status_code=status, code=code)

        return APIResponse.success(message="MFA disabled successfully")


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------


class ListNotificationsView(APIView):
    """List notifications for the user."""

    permission_classes = [IsAuthenticated]
    authentication_classes = [JWTAuthentication]

    @extend_schema(
        summary="List Notifications",
        responses={200: OpenApiResponse(description="List of notifications")},
    )
    def get(self, request, user_id):
        if str(request.user.id) != str(user_id):
            return APIResponse.error(
                message="Forbidden", status_code=403, code="forbidden"
            )

        from .models import Notification
        from .serializers import NotificationSerializer

        notifications = Notification.objects.filter(user_id=user_id).order_by(
            "-created_at"
        )[:50]
        serializer = NotificationSerializer(notifications, many=True)
        return APIResponse.success(data=serializer.data)


class MarkNotificationReadView(APIView):
    """Mark a single notification as read."""

    permission_classes = [IsAuthenticated]
    authentication_classes = [JWTAuthentication]

    @extend_schema(
        summary="Mark Notification as Read",
        responses={200: OpenApiResponse(description="Notification marked as read")},
    )
    def patch(self, request, notification_id):
        from .models import Notification
        from .serializers import NotificationSerializer

        try:
            notification = Notification.objects.get(
                id=notification_id, user=request.user
            )
            notification.is_read = True
            notification.save()
            return APIResponse.success(data=NotificationSerializer(notification).data)
        except Notification.DoesNotExist:
            return APIResponse.error(
                message="Not found", status_code=404, code="not_found"
            )


# ---------------------------------------------------------------------------
# API Keys
# ---------------------------------------------------------------------------

class ListAPIKeysView(APIView):
    """List all API keys for the current tenant."""

    permission_classes = [IsAuthenticated]
    authentication_classes = [JWTAuthentication]

    @extend_schema(
        summary="List API Keys",
        responses={200: OpenApiResponse(description="List of API keys")},
    )
    def get(self, request):
        from .models import APIKey
        from .serializers import APIKeySerializer

        keys = APIKey.objects.filter(tenant=request.user.tenant).order_by("-created_at")
        serializer = APIKeySerializer(keys, many=True)
        return APIResponse.success(data=serializer.data)


class CreateAPIKeyView(APIView):
    """Create a new API key."""

    permission_classes = [IsAuthenticated]
    authentication_classes = [JWTAuthentication]

    @extend_schema(
        summary="Create API Key",
        responses={201: OpenApiResponse(description="API Key created")},
    )
    def post(self, request):
        from .serializers import APIKeyCreateSerializer

        serializer = APIKeyCreateSerializer(data=request.data, context={"request": request})
        if serializer.is_valid():
            key = serializer.save()
            return APIResponse.success(
                data=APIKeyCreateSerializer(key).data,
                message="API key created. Please save it now, you will not be able to see it again.",
                status_code=201
            )
        return APIResponse.error(
            message="Failed to create API key",
            status_code=400,
            code="validation_error",
            errors=serializer.errors,
        )


class RevokeAPIKeyView(APIView):
    """Revoke (deactivate) an API key."""

    permission_classes = [IsAuthenticated]
    authentication_classes = [JWTAuthentication]

    @extend_schema(
        summary="Revoke API Key",
        responses={200: OpenApiResponse(description="API Key revoked")},
    )
    def post(self, request, key_id):
        from .models import APIKey
        from outbox.models import OutboxEvent

        try:
            key = APIKey.objects.get(id=key_id, tenant=request.user.tenant)
            key.is_active = False
            key.save()
            
            # Publish CDC outbox event for FastAPI to snapshot
            OutboxEvent.objects.create(
                topic="config.api_keys",
                payload={
                    "id": str(key.id),
                    "tenant_id": str(key.tenant_id),
                    "key": key.key,
                    "is_active": key.is_active,
                }
            )
            
            return APIResponse.success(message="API key revoked")
        except APIKey.DoesNotExist:
            return APIResponse.error(
                message="API key not found", status_code=404, code="not_found"
            )

from rest_framework.permissions import AllowAny

class InternalResolveAPIKeyView(APIView):
    """Internal endpoint for FastAPI to resolve API keys to tenant IDs."""
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request):
        api_key = request.headers.get("X-Api-Key")
        if not api_key:
            return APIResponse.error(message="Missing X-Api-Key header", status_code=400)

        from .models import APIKey
        try:
            key_obj = APIKey.objects.get(key=api_key, is_active=True)
            return APIResponse.success(data={"tenant_id": str(key_obj.tenant.id)})
        except APIKey.DoesNotExist:
            return APIResponse.error(message="Invalid or inactive API key", status_code=401)
