import datetime
import secrets
import uuid
from datetime import timedelta

import pyotp
from django.contrib.auth.models import (
    AbstractBaseUser,
    BaseUserManager,
    PermissionsMixin,
)
from django.db import models
from django.utils import timezone
from django.utils.text import slugify
from tenants.managers import TenantManager
from tenants.models import Tenant


def generate_token():
    return secrets.token_urlsafe(32)


def get_expiry_time():
    return timezone.now() + timedelta(days=2)


def generate_mfa_token():
    return secrets.token_urlsafe(32)


def mfa_token_expiry():
    return timezone.now() + timedelta(minutes=5)


class UserManager(BaseUserManager):
    """Custom user manager for multi-tenant User model."""

    def create_user(self, email, password, tenant=None, **extra_fields):
        """Create a regular user (requires tenant)."""
        if not email:
            raise ValueError("Email is required")

        if not tenant and not extra_fields.get("is_superuser"):
            raise ValueError("Tenant is required for regular users")

        email = self.normalize_email(email)

        # Check email uniqueness per tenant (or globally if no tenant)
        if tenant and self.filter(email=email, tenant=tenant).exists():
            raise ValueError(f"User with email {email} already exists in this tenant")

        user = self.model(email=email, tenant=tenant, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)

        return user

    def create_superuser(self, email, password, **extra_fields):
        """Create a platform superuser (no tenant required)."""
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        extra_fields.setdefault("is_superadmin", True)
        extra_fields.setdefault("role", "owner")

        if extra_fields.get("is_staff") is not True:
            raise ValueError("Superuser must have is_staff=True")

        if extra_fields.get("is_superuser") is not True:
            raise ValueError("Superuser must have is_superuser=True")

        # Platform superusers don't need a tenant
        return self.create_user(email, password, tenant=None, **extra_fields)


class User(AbstractBaseUser, PermissionsMixin):
    """
    Custom multi-tenant User model with platform admin support.

    Two types of users:
    1. Tenant users (tenant_id set) — belong to specific tenant
    2. Platform admins (tenant_id null) — manage all tenants globally
    """

    ROLE_CHOICES = [
        ("owner", "Owner - Full access, can manage billing"),
        ("admin", "Admin - Full access, cannot manage billing"),
        ("engineer", "Engineer - Can view/interact with incidents"),
        ("viewer", "Viewer - Read-only access"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField(max_length=255, unique=True)
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="users",
        null=True,
        blank=True,
        help_text="Null for platform admins, set for tenant users",
    )
    first_name = models.CharField(max_length=255, blank=True)
    last_name = models.CharField(max_length=255, blank=True)
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default="engineer")
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    is_superuser = models.BooleanField(default=False)
    is_superadmin = models.BooleanField(
        default=False, help_text="Platform operator flag"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    email_verified = models.BooleanField(
        default=False, help_text="Email has been verified"
    )

    objects = UserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    class Meta:
        db_table = "users"
        indexes = [
            models.Index(fields=["tenant", "email"]),
            models.Index(fields=["is_active"]),
            models.Index(fields=["created_at"]),
        ]
        ordering = ["-created_at"]

    def __str__(self):
        if self.tenant:
            return f"{self.email} ({self.tenant.name} - {self.role})"
        return f"{self.email} (Platform Admin)"

    def get_full_name(self):
        full_name = f"{self.first_name} {self.last_name}".strip()
        return full_name or self.email

    def get_short_name(self):
        return self.first_name or self.email

    def is_tenant_owner(self):
        return self.role == "owner" and self.tenant is not None

    def is_tenant_admin(self):
        return self.role in ["admin", "owner"] and self.tenant is not None

    def is_platform_admin(self):
        """Check if user is a platform administrator (no tenant)."""
        return self.is_superuser and self.tenant is None

    def is_tenant_active(self) -> bool:
        """Return True only if the user's tenant is in 'active' status."""
        if self.tenant is None:
            # Platform superadmins have no tenant — always allowed.
            return True
        return self.tenant.status == "active"


# ============================================================================
# USER INVITATION
# ============================================================================


class UserInvitation(models.Model):
    """
    Invite engineers to join tenant.

    Flow:
    1. Admin creates invitation → sends email
    2. Engineer opens email link → visits /join?token=xyz
    3. Engineer signs up (email/password or OAuth) → joins tenant
    4. Invitation marked accepted
    """

    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("accepted", "Accepted"),
        ("expired", "Expired"),
        ("cancelled", "Cancelled"),
    ]

    objects = TenantManager()

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Invitation details
    email = models.EmailField()
    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, related_name="invitations"
    )
    invited_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, related_name="invitations_sent"
    )
    role = models.CharField(
        max_length=20, choices=User.ROLE_CHOICES, default="engineer"
    )

    # Token
    token = models.CharField(
        max_length=255, unique=True, default=generate_token, db_index=True
    )

    # Status
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")

    # When accepted
    accepted_by = models.OneToOneField(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="invitation_accepted",
    )

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(default=get_expiry_time)
    accepted_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)

    # Optional: Track if email was sent
    email_sent_at = models.DateTimeField(null=True, blank=True)
    email_resent_count = models.IntegerField(default=0)

    class Meta:
        db_table = "user_invitations"
        unique_together = ("tenant", "email", "status")
        indexes = [
            models.Index(fields=["token"]),
            models.Index(fields=["tenant", "status"]),
            models.Index(fields=["expires_at"]),
            models.Index(fields=["email", "status"]),
        ]

    def __str__(self):
        return f"{self.email} → {self.tenant.name} ({self.status})"

    def is_valid(self):
        """Check if invitation is still valid."""
        return self.status == "pending" and timezone.now() <= self.expires_at

    def accept(self, user):
        """Mark invitation as accepted."""
        if self.status != "pending":
            raise ValueError(f"Cannot accept invitation with status: {self.status}")

        self.status = "accepted"
        self.accepted_by = user
        self.accepted_at = timezone.now()
        self.save()

        # Log to audit
        AuditLog.log(
            action="USER_INVITE_ACCEPTED",
            user=user,
            tenant=self.tenant,
            resource_type="UserInvitation",
            resource_id=str(self.id),
            description=f"User accepted invitation to join as {user.role}",
        )

    def cancel(self):
        """Cancel invitation."""
        self.status = "cancelled"
        self.cancelled_at = timezone.now()
        self.save()

    def expire(self):
        """Expire invitation."""
        self.status = "expired"
        self.save()

    def mark_email_sent(self):
        """Mark email as sent."""
        self.email_sent_at = timezone.now()
        self.save()

    def increment_resend_count(self):
        """Track email resends."""
        self.email_resent_count += 1
        self.save()


# ============================================================================
# AUDIT LOG
# ============================================================================


class AuditLog(models.Model):
    ACTION_CHOICES = [
        # --- existing ---
        ("USER_CREATED", "User Created"),
        ("USER_UPDATED", "User Updated"),
        ("USER_DELETED", "User Deleted"),
        ("ROLE_CHANGED", "Role Changed"),
        ("API_KEY_CREATED", "API Key Created"),
        ("API_KEY_REVOKED", "API Key Revoked"),
        # --- new: invitation lifecycle ---
        ("USER_INVITED", "User Invited"),
        ("USER_INVITE_ACCEPTED", "User Invite Accepted"),
        ("USER_INVITE_CANCELLED", "User Invite Cancelled"),
        ("USER_INVITE_RESENT", "User Invite Resent"),
        # --- new: authentication events ---
        ("LOGIN", "Login"),
        ("LOGIN_FAILED", "Login Failed"),
        ("LOGOUT", "Logout"),
        ("TOKEN_REVOKED", "Token Revoked"),
        ("PASSWORD_RESET_REQUESTED", "Password Reset Requested"),
        ("PASSWORD_RESET_COMPLETED", "Password Reset Completed"),
        ("EMAIL_VERIFIED", "Email Verified"),
        # --- new: MFA events ---
        ("MFA_SETUP", "MFA Setup"),
        ("MFA_VERIFIED", "MFA Verified"),
        ("MFA_DISABLED", "MFA Disabled"),
        # --- new: tenant lifecycle ---
        ("TENANT_CREATED", "Tenant Created"),
        ("TENANT_SUSPENDED", "Tenant Suspended"),
        ("TENANT_REACTIVATED", "Tenant Reactivated"),
        ("TENANT_CONFIG_UPDATED", "Tenant Config Updated"),
    ]

    objects = TenantManager()

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user_email = models.EmailField()
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, null=True, blank=True)
    action = models.CharField(max_length=50, choices=ACTION_CHOICES)
    resource_type = models.CharField(max_length=50, blank=True)
    resource_id = models.CharField(max_length=255, blank=True)
    description = models.TextField(blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    success = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    user = models.ForeignKey(
        "users.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="audit_logs",
        help_text="FK to the user at time of event; preserved even if email changes.",
    )

    class Meta:
        db_table = "audit_logs"
        indexes = [
            models.Index(fields=["tenant"]),
            models.Index(fields=["action"]),
            models.Index(fields=["created_at"]),
        ]

    def __str__(self):
        return f"[{self.action}] {self.user_email} @ {self.created_at:%Y-%m-%d %H:%M}"

    @classmethod
    def log(
        cls,
        action,
        user=None,
        tenant=None,
        user_email=None,
        resource_type="",
        resource_id="",
        description="",
        ip_address=None,
        success=True,
    ):
        """
        Convenience factory for writing audit log entries.

        All service methods should use this instead of calling
        .objects.create() directly to avoid repeating boilerplate.

        ``tenant`` and ``user_email`` are auto-resolved from ``user``
        when not provided explicitly.
        """
        resolved_tenant = (
            tenant if tenant is not None else (user.tenant if user else None)
        )
        resolved_email = user_email if user_email else (user.email if user else "")
        cls.objects.create(
            action=action,
            user=user,
            tenant=resolved_tenant,
            user_email=resolved_email,
            resource_type=resource_type,
            resource_id=resource_id,
            description=description,
            ip_address=ip_address,
            success=success,
        )


# ============================================================================
# API KEY
# ============================================================================


class APIKey(models.Model):
    objects = TenantManager()

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, related_name="api_keys"
    )
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    name = models.CharField(max_length=255)
    key = models.CharField(max_length=255, unique=True, db_index=True)
    is_active = models.BooleanField(default=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "api_keys"
        unique_together = ("tenant", "name")
        indexes = [
            models.Index(fields=["tenant"]),
        ]

    def is_valid(self):
        return self.is_active


# ============================================================================
# USER SESSION (ADD TO EXISTING FILE)
# ============================================================================


class UserSession(models.Model):
    """
    Track user login sessions for security and compliance.

    When user logs in, a session record is created with:
    - Session ID (jti from JWT)
    - Device info (browser, OS)
    - IP address
    - Login time
    - Expiry time (matches JWT expiry)

    Enables:
    - See all active sessions
    - Force logout from a device
    - Detect suspicious login patterns
    """

    objects = TenantManager()

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # Link to user and tenant
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="sessions")
    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, related_name="sessions", null=True, blank=True
    )

    # Session identifier (JWT jti claim)
    session_id = models.CharField(max_length=255, unique=True, db_index=True)

    # Device and location info
    device_name = models.CharField(max_length=255, blank=True)
    ip_address = models.GenericIPAddressField()
    user_agent = models.TextField(blank=True)

    # Status
    is_active = models.BooleanField(default=True)
    is_revoked = models.BooleanField(default=False)
    revoked_at = models.DateTimeField(null=True, blank=True)

    # Activity tracking
    last_activity_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()

    class Meta:
        db_table = "user_sessions"
        indexes = [
            models.Index(fields=["tenant"]),
            models.Index(fields=["user", "is_active"]),
            models.Index(fields=["session_id"]),
            models.Index(fields=["expires_at"]),
        ]

    def __str__(self):
        status = "Active" if self.is_active and not self.is_revoked else "Revoked"
        return f"{self.user.email} | {self.device_name} | {self.ip_address} | {status}"

    def is_valid(self):
        """Check if session is still valid."""
        return (
            self.is_active and not self.is_revoked and timezone.now() <= self.expires_at
        )

    def revoke(self):
        """Revoke this session (force logout from device)."""
        self.is_revoked = True
        self.revoked_at = timezone.now()
        self.save()


# ============================================================================
# EMAIL VERIFICATION (ADD TO EXISTING FILE)
# ============================================================================


class EmailVerification(models.Model):
    """
    Track email verification tokens.

    When user registers, email verification token is created.
    User clicks link in email to verify.
    After verification, email_verified flag is set on User.
    """

    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("verified", "Verified"),
        ("expired", "Expired"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Link to user
    user = models.OneToOneField(
        User, on_delete=models.CASCADE, related_name="email_verification"
    )

    # Token
    token = models.CharField(max_length=255, unique=True, default=generate_token)

    # Status
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(default=get_expiry_time)
    verified_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "email_verifications"
        indexes = [
            models.Index(fields=["token"]),
            models.Index(fields=["user", "status"]),
            models.Index(fields=["expires_at"]),
        ]

    def __str__(self):
        return f"{self.user.email} - {self.status}"

    def is_valid(self):
        """Check if token is still valid."""
        return self.status == "pending" and timezone.now() <= self.expires_at

    def verify(self):
        """Mark email as verified."""
        self.status = "verified"
        self.verified_at = timezone.now()
        self.save()

        # Update user
        self.user.email_verified = True
        self.user.save()


# ============================================================================
# PASSWORD RESET (ADD TO EXISTING FILE)
# ============================================================================


class PasswordReset(models.Model):
    """
    Track password reset tokens.

    When user requests password reset, token is created.
    User clicks link in email to reset password.
    Token expires after 24 hours.
    """

    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("used", "Used"),
        ("expired", "Expired"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Link to user
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="password_resets"
    )

    # Token
    token = models.CharField(max_length=255, unique=True, default=generate_token)

    # Status
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(default=get_expiry_time)
    used_at = models.DateTimeField(null=True, blank=True)

    # IP address for security audit
    ip_address = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        db_table = "password_resets"
        indexes = [
            models.Index(fields=["token"]),
            models.Index(fields=["user", "status"]),
            models.Index(fields=["expires_at"]),
        ]

    def __str__(self):
        return f"{self.user.email} - {self.status}"

    def is_valid(self):
        """Check if token is still valid."""
        return self.status == "pending" and timezone.now() <= self.expires_at

    def use(self):
        """Mark token as used."""
        self.status = "used"
        self.used_at = timezone.now()
        self.save()


# ============================================================================
# OAUTH ACCOUNT
# ============================================================================


class OAuthAccount(models.Model):
    """
    Link OAuth provider accounts to users.

    Allows users to:
    - Sign up via Google/GitHub
    - Link existing account to OAuth provider
    - Sign in via OAuth even if registered with email/password
    """

    PROVIDER_CHOICES = [
        ("google", "Google"),
        ("github", "GitHub"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Link to user
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="oauth_accounts"
    )

    # Provider info
    provider = models.CharField(max_length=20, choices=PROVIDER_CHOICES)
    provider_user_id = models.CharField(max_length=255)  # OAuth provider's user ID
    provider_email = models.EmailField()
    provider_name = models.CharField(max_length=255, blank=True)
    provider_picture_url = models.URLField(blank=True)

    # Tokens (for future use)
    access_token = models.TextField(
        blank=True, help_text="Encrypted OAuth access token"
    )
    refresh_token = models.TextField(
        blank=True, help_text="Encrypted OAuth refresh token"
    )

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "oauth_accounts"
        unique_together = ("provider", "provider_user_id")
        indexes = [
            models.Index(fields=["user", "provider"]),
            models.Index(fields=["provider", "provider_user_id"]),
        ]

    def __str__(self):
        return f"{self.user.email} - {self.provider}"


class TOTPDevice(models.Model):
    """
    Store TOTP (Two-Factor Authentication) settings for user.

    When user enables MFA:
    1. Generate secret key
    2. User scans QR code with Google Authenticator/Authy
    3. User verifies with 6-digit code
    4. Confirmed = True, MFA active
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(
        User, on_delete=models.CASCADE, related_name="totp_device"
    )

    # TOTP Secret Key (encrypted in real production)
    secret_key = models.CharField(max_length=255)

    # Status
    is_confirmed = models.BooleanField(
        default=False, help_text="True = MFA is active, user must use TOTP on login"
    )

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "totp_devices"

    def __str__(self):
        return f"{self.user.email} - {'Confirmed' if self.is_confirmed else 'Pending'}"

    @staticmethod
    def generate_secret():
        """Generate a new TOTP secret key."""
        return pyotp.random_base32()

    def get_totp(self):
        """Get TOTP object for this device."""
        return pyotp.TOTP(self.secret_key)

    def get_qr_code(self, user_email):
        """
        Generate QR code URL for user to scan.
        User scans with Google Authenticator, Authy, Microsoft Authenticator, etc.
        """
        totp = self.get_totp()
        return totp.provisioning_uri(name=user_email, issuer_name="NeuralOps")

    def verify_token(self, token):
        """
        Verify 6-digit TOTP code.
        Allows 1 backward & 1 forward time window (30-second windows).
        """
        totp = self.get_totp()
        current_utc_time = datetime.datetime.now(datetime.timezone.utc)
        return totp.verify(token, valid_window=1, for_time=current_utc_time)

    def confirm(self):
        """Mark MFA as confirmed & active."""
        self.is_confirmed = True
        self.confirmed_at = timezone.now()
        self.save()


class BackupCode(models.Model):
    """
    One-time backup codes for account recovery.

    Generated when user sets up MFA.
    User can use instead of TOTP if they lose authenticator app.
    Each code can only be used ONCE.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="backup_codes"
    )

    # Code is hashed (never store plaintext)
    code_hash = models.CharField(max_length=255)

    # Status
    is_used = models.BooleanField(default=False)
    used_at = models.DateTimeField(null=True, blank=True)

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "backup_codes"
        unique_together = ("user", "code_hash")

    def __str__(self):
        status = "Used" if self.is_used else "Available"
        return f"{self.user.email} - {status}"

    @staticmethod
    def generate_codes(count=10):
        """Generate list of backup codes."""
        return [secrets.token_hex(4) for _ in range(count)]

    @staticmethod
    def hash_code(code):
        """Hash backup code (use Django's make_password)."""
        from django.contrib.auth.hashers import make_password

        return make_password(code)

    def use(self):
        """Mark code as used."""
        self.is_used = True
        self.used_at = timezone.now()
        self.save()


class MFAVerificationToken(models.Model):
    """
    Temporary token issued after password verification.

    Flow:
    1. User logs in with email/password
    2. If MFA enabled, return MFA_VERIFICATION_TOKEN (not access token)
    3. User verifies TOTP code
    4. Exchange MFA_VERIFICATION_TOKEN + TOTP code for access/refresh tokens

    Token expires in 5 minutes.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="mfa_tokens")

    token = models.CharField(
        max_length=255, unique=True, default=generate_mfa_token, db_index=True
    )

    # Expires in 5 minutes
    expires_at = models.DateTimeField(default=mfa_token_expiry)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "mfa_verification_tokens"

    def is_valid(self):
        """Check if token is still valid."""
        return timezone.now() <= self.expires_at


class Notification(models.Model):
    """
    In-app notifications for users (e.g. mentions, assignments).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    
    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.CASCADE,
        related_name="notifications",
        db_index=True,
    )
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="notifications",
        db_index=True,
    )

    # E.g., 'mention', 'assignment'
    type = models.CharField(max_length=50, db_index=True)
    
    title = models.CharField(max_length=255)
    body = models.TextField()
    
    # Optional incident context
    incident_id = models.UUIDField(null=True, blank=True, db_index=True)
    
    is_read = models.BooleanField(default=False, db_index=True)
    
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "notifications"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["tenant", "user", "-created_at"]),
        ]

    def __str__(self):
        return f"Notification(user={self.user_id}, type={self.type}, read={self.is_read})"
