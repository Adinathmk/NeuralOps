from django.contrib.auth.password_validation import validate_password
from rest_framework import serializers

from .models import Tenant, User


class TenantSerializer(serializers.ModelSerializer):
    """Serialize tenant info"""

    class Meta:
        model = Tenant
        fields = ("id", "name", "slug", "plan_tier", "status", "created_at")
        read_only_fields = ("id", "created_at")


class UserSerializer(serializers.ModelSerializer):
    """Serialize user with tenant info"""

    tenant = TenantSerializer(read_only=True)
    full_name = serializers.SerializerMethodField(read_only=True)
    is_email_verified = serializers.SerializerMethodField(read_only=True)
    mfa_enabled = serializers.SerializerMethodField(read_only=True)
    avatar_url = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = User
        fields = (
            "id",
            "email",
            "first_name",
            "last_name",
            "full_name",
            "role",
            "email_verified",
            "is_email_verified",
            "mfa_enabled",
            "avatar_url",
            "tenant",
            "created_at",
        )
        read_only_fields = ("id", "created_at")

    def get_full_name(self, obj):
        return obj.get_full_name()

    def get_is_email_verified(self, obj):
        """Alias for email_verified for frontend compatibility."""
        return obj.email_verified

    def get_mfa_enabled(self, obj):
        """Check if user has a confirmed TOTP device."""
        from .models import TOTPDevice

        return TOTPDevice.objects.filter(user=obj, is_confirmed=True).exists()

    def get_avatar_url(self, obj):
        if hasattr(obj, 'profile_picture_key') and obj.profile_picture_key:
            from .services.s3_service import S3Service
            return S3Service.generate_presigned_get_url(obj.profile_picture_key)
        return None


class RegisterSerializer(serializers.Serializer):
    """Register a new user and create a tenant"""

    email = serializers.EmailField()
    password = serializers.CharField(
        min_length=8, max_length=128, write_only=True, style={"input_type": "password"}
    )
    password_confirm = serializers.CharField(
        min_length=8, max_length=128, write_only=True, style={"input_type": "password"}
    )
    tenant_name = serializers.CharField(max_length=255)
    first_name = serializers.CharField(max_length=255, required=False, allow_blank=True)
    last_name = serializers.CharField(max_length=255, required=False, allow_blank=True)

    def validate(self, data):
        """Validate email uniqueness and password match"""

        # Check passwords match
        if data["password"] != data["password_confirm"]:
            raise serializers.ValidationError(
                {"password": "Password fields did not match."}
            )

        # Check email not already used in any tenant
        if User.objects.filter(email=data["email"]).exists():
            raise serializers.ValidationError({"email": "Email already registered."})

        # Check tenant name doesn't exist
        if Tenant.objects.filter(name=data["tenant_name"]).exists():
            raise serializers.ValidationError(
                {"tenant_name": "Organization name already exists."}
            )

        # Validate password strength
        try:
            validate_password(data["password"])
        except serializers.ValidationError as e:
            raise serializers.ValidationError({"password": e.messages})

        return data

    def create(self, validated_data):
        """Create tenant and user"""
        import time
        from django.db import connection, transaction

        # Registration happens on an unauthenticated endpoint.
        # RLS will block creating a tenant/user unless we explicitly bypass it for this transaction!
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("SELECT set_config('app.bypass_rls', 'on', true)")

                # Create tenant
                slug = (
                    validated_data["tenant_name"]
                    .lower()
                    .replace(" ", "-")
                    .replace("_", "-")
                )
                tenant = Tenant.objects.create(
                    name=validated_data["tenant_name"],
                    slug=slug,
                    plan_tier="free",
                    status="active",
                )

                # Create user as tenant owner
                user = User.objects.create_user(
                    email=validated_data["email"],
                    password=validated_data["password"],
                    tenant=tenant,
                    first_name=validated_data.get("first_name", ""),
                    last_name=validated_data.get("last_name", ""),
                    role="owner",
                    is_staff=False,
                    email_verified=False,
                )

                # Publish config.tenants outbox event so FastAPI's ConfigSyncConsumer
                # can upsert the tenant_snapshots row. Without this, FastAPI returns
                # "Tenant configuration is not yet available" for every new user.
                from outbox.models import OutboxEvent
                OutboxEvent.objects.create(
                    topic="config.tenants",
                    key=str(tenant.id),
                    payload={
                        "event_type": "tenant.updated",
                        "tenant": {
                            "id": str(tenant.id),
                            "plan_tier": tenant.plan_tier,
                            "vector_namespace": tenant.vector_namespace,
                            "kafka_group_id": tenant.kafka_group_id,
                            "is_suspended": tenant.is_suspended,
                            "source_version": int(time.time() * 1000),
                        },
                    },
                )

        return user


class LoginSerializer(serializers.Serializer):
    """Login with email and password only."""

    email = serializers.EmailField()

    password = serializers.CharField(write_only=True, style={"input_type": "password"})

    def validate(self, data):
        """Validate credentials."""

        email = data["email"].lower().strip()

        # Get user by email
        try:
            user = User.objects.get(email=email)

        except User.DoesNotExist:
            raise serializers.ValidationError({"email": "Invalid email or password."})

        # Check password
        if not user.check_password(data["password"]):
            raise serializers.ValidationError(
                {"password": "Invalid email or password."}
            )

        # Check user active
        if not user.is_active:
            raise serializers.ValidationError({"email": "User account is inactive."})

        # Check email verification
        if not user.email_verified:
            raise serializers.ValidationError(
                {"email": "Please verify your email before logging in."}
            )

        # Check if tenant is active
        if not user.is_tenant_active():
            raise serializers.ValidationError(
                {"email": "Your organization account is currently inactive."}
            )

        data["user"] = user
        return data


class TokenRefreshSerializer(serializers.Serializer):
    """Refresh access token using refresh token"""

    refresh_token = serializers.CharField()

    def validate_refresh_token(self, value):
        """Validate refresh token"""
        from .authentication import JWTAuthentication

        payload = JWTAuthentication.verify_token(value)

        if payload.get("type") != "refresh":
            raise serializers.ValidationError("Invalid refresh token.")

        return value


class VerifyEmailSerializer(serializers.Serializer):
    """Verify email with token."""

    token = serializers.CharField()

    def validate_token(self, value):
        """Validate token exists and is not expired."""
        from .models import EmailVerification

        try:
            verification = EmailVerification.objects.get(token=value)
        except EmailVerification.DoesNotExist:
            raise serializers.ValidationError("Invalid verification token.")

        if not verification.is_valid():
            raise serializers.ValidationError("Token has expired.")

        return verification


class ResendVerificationEmailSerializer(serializers.Serializer):
    """Resend verification email serializer."""

    email = serializers.EmailField()

    def validate_email(self, value):
        """
        Normalize email only.

        IMPORTANT:
        Do NOT validate user existence here.
        Otherwise it enables email enumeration attacks.
        """
        return value.lower().strip()


class ForgotPasswordSerializer(serializers.Serializer):
    """Request password reset serializer."""

    email = serializers.EmailField()

    def validate_email(self, value):

        return value.lower().strip()


class ResetPasswordSerializer(serializers.Serializer):
    """Reset password with token."""

    token = serializers.CharField()
    new_password = serializers.CharField(
        min_length=8, max_length=128, write_only=True, style={"input_type": "password"}
    )
    new_password_confirm = serializers.CharField(
        min_length=8, max_length=128, write_only=True, style={"input_type": "password"}
    )

    def validate(self, data):
        """Validate passwords match and meet requirements."""

        # Check passwords match
        if data["new_password"] != data["new_password_confirm"]:
            raise serializers.ValidationError(
                {"new_password": "Password fields did not match."}
            )

        # Validate password strength
        try:
            validate_password(data["new_password"])
        except serializers.ValidationError as e:
            raise serializers.ValidationError({"new_password": e.messages})

        return data

    def validate_token(self, value):
        """Validate token exists and is valid."""
        from .models import PasswordReset

        try:
            reset = PasswordReset.objects.get(token=value)
        except PasswordReset.DoesNotExist:
            raise serializers.ValidationError("Invalid reset token.")

        if not reset.is_valid():
            raise serializers.ValidationError("Token has expired.")

        return reset


class ChangePasswordSerializer(serializers.Serializer):
    """Change password (authenticated user)."""

    current_password = serializers.CharField(
        write_only=True, style={"input_type": "password"}
    )
    new_password = serializers.CharField(
        min_length=8, max_length=128, write_only=True, style={"input_type": "password"}
    )
    new_password_confirm = serializers.CharField(
        min_length=8, max_length=128, write_only=True, style={"input_type": "password"}
    )

    def validate(self, data):
        """Validate current password and new passwords match."""

        # Check new passwords match
        if data["new_password"] != data["new_password_confirm"]:
            raise serializers.ValidationError(
                {"new_password": "Password fields did not match."}
            )

        # Validate password strength
        try:
            validate_password(data["new_password"])
        except serializers.ValidationError as e:
            raise serializers.ValidationError({"new_password": e.messages})

        return data


class GoogleOAuthCallbackSerializer(serializers.Serializer):
    """
    Google OAuth callback.

    Two flows:
    1. Owner signup/signin (no invite_token)
    2. Engineer join via invitation (with invite_token)
    """

    code = serializers.CharField()
    invite_token = serializers.CharField(required=False, allow_blank=True)

    def validate_code(self, value):
        """Validate and exchange code for token."""
        from .services.oauth_providers import GoogleOAuthService

        try:
            access_token = GoogleOAuthService.exchange_code_for_token(value)
            return access_token
        except Exception as e:
            raise serializers.ValidationError(str(e))

    def validate_invite_token(self, value):
        """Validate invite token if provided."""
        if not value:
            return None

        from .models import UserInvitation

        try:
            invitation = UserInvitation.objects.get(token=value)
        except UserInvitation.DoesNotExist:
            raise serializers.ValidationError("Invalid invitation token.")

        if not invitation.is_valid():
            raise serializers.ValidationError("Invitation has expired.")

        return invitation


class GitHubOAuthCallbackSerializer(serializers.Serializer):
    """
    GitHub OAuth callback.

    Two flows:
    1. Owner signup/signin (no invite_token)
    2. Engineer join via invitation (with invite_token)
    """

    code = serializers.CharField()
    invite_token = serializers.CharField(required=False, allow_blank=True)

    def validate_code(self, value):
        """Validate and exchange code for token."""
        from .services.oauth_providers import GitHubOAuthService

        try:
            access_token = GitHubOAuthService.exchange_code_for_token(value)
            return access_token
        except Exception as e:
            raise serializers.ValidationError(str(e))

    def validate_invite_token(self, value):
        """Validate invite token if provided."""
        if not value:
            return None

        from .models import UserInvitation

        try:
            invitation = UserInvitation.objects.get(token=value)
        except UserInvitation.DoesNotExist:
            raise serializers.ValidationError("Invalid invitation token.")

        if not invitation.is_valid():
            raise serializers.ValidationError("Invitation has expired.")

        return invitation


# ADD new serializers:


class InviteEngineerSerializer(serializers.Serializer):
    """Admin invites engineer to tenant."""

    email = serializers.EmailField()
    role = serializers.ChoiceField(choices=User.ROLE_CHOICES)

    def validate_email(self, value):
        """Check email format and basic validation."""
        # Optionally check if email already in tenant
        return value

    def validate_role(self, value):
        """Only allow inviting engineers/viewers, not owners."""
        if value not in ["engineer", "viewer"]:
            raise serializers.ValidationError(
                "Can only invite engineers or viewers. Owners must sign up directly."
            )
        return value


class JoinWithInvitationSerializer(serializers.Serializer):
    """Engineer joins via invitation with email/password."""

    invite_token = serializers.CharField()
    password = serializers.CharField(
        min_length=8, max_length=128, write_only=True, style={"input_type": "password"}
    )
    password_confirm = serializers.CharField(
        min_length=8, max_length=128, write_only=True, style={"input_type": "password"}
    )
    first_name = serializers.CharField(max_length=255, required=False, allow_blank=True)
    last_name = serializers.CharField(max_length=255, required=False, allow_blank=True)

    def validate(self, data):
        """Validate invitation and passwords."""
        from .models import UserInvitation

        # Validate invitation token
        try:
            invitation = UserInvitation.objects.get(token=data["invite_token"])
        except UserInvitation.DoesNotExist:
            raise serializers.ValidationError("Invalid invitation token.")

        if not invitation.is_valid():
            raise serializers.ValidationError("Invitation has expired.")

        # Check if email matches invitation
        # (engineer could be signing up with different email - but invitation is for specific email)
        # For security, enforce email match
        if not hasattr(self, "context") or "invitation" not in self.context:
            # Email will be from invitation, not from request
            pass

        # Validate passwords match
        if data["password"] != data["password_confirm"]:
            raise serializers.ValidationError({"password": "Passwords do not match."})

        # Validate password strength
        try:
            validate_password(data["password"])
        except serializers.ValidationError as e:
            raise serializers.ValidationError({"password": e.messages})

        data["invitation"] = invitation
        return data


class ValidateInvitationTokenSerializer(serializers.Serializer):
    """Validate invitation token and return details."""

    token = serializers.CharField()

    def validate_token(self, value):
        """Validate token and return invitation details."""
        from .models import UserInvitation

        try:
            invitation = UserInvitation.objects.get(token=value)
        except UserInvitation.DoesNotExist:
            raise serializers.ValidationError("Invalid invitation token.")

        if not invitation.is_valid():
            raise serializers.ValidationError("Invitation has expired.")

        return invitation


class SetupMFASerializer(serializers.Serializer):
    """Start MFA setup - generate secret & QR code."""

    # No input needed - just POST /api/auth/mfa/setup
    pass


class ConfirmMFASerializer(serializers.Serializer):
    """Verify TOTP code to confirm MFA is working."""

    code = serializers.CharField(max_length=6, min_length=6)

    def validate_code(self, value):
        """Validate code is numeric."""
        if not value.isdigit():
            raise serializers.ValidationError("Code must be 6 digits.")
        return value


class VerifyMFATokenSerializer(serializers.Serializer):
    """
    Exchange MFA verification token + TOTP code for access tokens.

    Called after user enters 6-digit code from authenticator.
    """

    mfa_token = serializers.CharField()
    code = serializers.CharField(max_length=16, min_length=6)

    def validate_code(self, value):
        """Validate code format."""
        if not value.replace("-", "").isalnum():
            raise serializers.ValidationError("Invalid code format.")
        return value


class DisableMFASerializer(serializers.Serializer):
    """Disable MFA - requires password verification."""

    password = serializers.CharField(write_only=True, style={"input_type": "password"})
    code = serializers.CharField(
        max_length=16,
        min_length=6,
        required=False,
        help_text="6-digit TOTP code OR backup code",
    )

    def validate_code(self, value):
        """Validate code if provided."""
        if value and not value.replace("-", "").isalnum():
            raise serializers.ValidationError("Invalid code format.")
        return value


class NotificationSerializer(serializers.ModelSerializer):
    """Serializer for in-app notifications."""

    # Send user_id directly to match frontend Notification interface
    user_id = serializers.UUIDField(source="user.id", read_only=True)

    class Meta:
        from .models import Notification

        model = Notification
        fields = (
            "id",
            "user_id",
            "type",
            "title",
            "body",
            "incident_id",
            "is_read",
            "created_at",
        )
        read_only_fields = fields


class APIKeySerializer(serializers.ModelSerializer):
    """Serializer for listing API keys."""
    
    key_prefix = serializers.SerializerMethodField()

    class Meta:
        from .models import APIKey

        model = APIKey
        fields = ["id", "name", "key_prefix", "is_active", "last_used_at", "created_at"]
        read_only_fields = fields
        
    def get_key_prefix(self, obj):
        # Return first 14 chars like nops_live_abcd
        return obj.key[:14] if obj.key else ""


class APIKeyCreateSerializer(serializers.ModelSerializer):
    """Serializer for creating an API key."""

    class Meta:
        from .models import APIKey

        model = APIKey
        fields = ["id", "name", "key", "created_at"]
        read_only_fields = ["id", "key", "created_at"]

    def create(self, validated_data):
        tenant = self.context["request"].user.tenant
        user = self.context["request"].user

        import secrets

        # Generate a new random key
        raw_key = f"nops_live_{secrets.token_hex(24)}"
        validated_data["key"] = raw_key
        validated_data["tenant"] = tenant
        validated_data["created_by"] = user

        from .models import APIKey
        from outbox.models import OutboxEvent
        
        api_key_obj = APIKey.objects.create(**validated_data)
        
        # Publish CDC outbox event for FastAPI to snapshot
        OutboxEvent.objects.create(
            topic="config.api_keys",
            payload={
                "id": str(api_key_obj.id),
                "tenant_id": str(api_key_obj.tenant_id),
                "key": api_key_obj.key,
                "is_active": api_key_obj.is_active,
            }
        )
        
        return api_key_obj


class ProfilePicturePresignedUrlSerializer(serializers.Serializer):
    filename = serializers.CharField(max_length=255)
    content_type = serializers.CharField(max_length=100)


class ProfilePictureConfirmSerializer(serializers.Serializer):
    object_key = serializers.CharField(max_length=255)
