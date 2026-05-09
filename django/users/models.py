from django.db import models
from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from tenants.models import Tenant
import uuid
import secrets
from datetime import timedelta
from django.utils import timezone
from django.utils.text import slugify
import secrets
from datetime import timedelta
from django.utils import timezone


def generate_token():
    return secrets.token_urlsafe(32)

def get_expiry_time():
    return timezone.now() + timedelta(days=2)



class UserManager(BaseUserManager):
    """Custom user manager for multi-tenant User model."""
    
    def create_user(self, email, password, tenant=None, **extra_fields):
        """Create a regular user (requires tenant)."""
        if not email:
            raise ValueError('Email is required')
        
        if not tenant and not extra_fields.get('is_superuser'):
            raise ValueError('Tenant is required for regular users')
        
        email = self.normalize_email(email)
        
        # Check email uniqueness per tenant (or globally if no tenant)
        if tenant and self.filter(email=email, tenant=tenant).exists():
            raise ValueError(f'User with email {email} already exists in this tenant')
        
        user = self.model(email=email, tenant=tenant, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        
        return user
    
    def create_superuser(self, email, password, **extra_fields):
        """Create a platform superuser (no tenant required)."""
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)
        extra_fields.setdefault('is_superadmin', True)
        extra_fields.setdefault('role', 'owner')
        
        if extra_fields.get('is_staff') is not True:
            raise ValueError('Superuser must have is_staff=True')
        
        if extra_fields.get('is_superuser') is not True:
            raise ValueError('Superuser must have is_superuser=True')
        
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
        ('owner', 'Owner - Full access, can manage billing'),
        ('admin', 'Admin - Full access, cannot manage billing'),
        ('engineer', 'Engineer - Can view/interact with incidents'),
        ('viewer', 'Viewer - Read-only access'),
    ]
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField(max_length=255, unique=True)
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name='users',
        null=True,
        blank=True,
        help_text="Null for platform admins, set for tenant users"
    )
    first_name = models.CharField(max_length=255, blank=True)
    last_name = models.CharField(max_length=255, blank=True)
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default='engineer')
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    is_superuser = models.BooleanField(default=False)
    is_superadmin = models.BooleanField(default=False, help_text="Platform operator flag")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    objects = UserManager()
    
    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = []
    
    class Meta:
        db_table = 'users'
        unique_together = ('tenant', 'email')
        indexes = [
            models.Index(fields=['tenant', 'email']),
            models.Index(fields=['is_active']),
            models.Index(fields=['created_at']),
        ]
        ordering = ['-created_at']
    
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
        return self.role == 'owner' and self.tenant is not None
    
    def is_tenant_admin(self):
        return self.role in ['admin', 'owner'] and self.tenant is not None
    
    def is_platform_admin(self):
        """Check if user is a platform administrator (no tenant)."""
        return self.is_superuser and self.tenant is None

# ============================================================================
# USER INVITATION
# ============================================================================

class UserInvitation(models.Model):
    STATUS_CHOICES = [('pending', 'Pending'), ('accepted', 'Accepted'), ('expired', 'Expired')]
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField()
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name='invitations')
    invited_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    role = models.CharField(max_length=20, choices=User.ROLE_CHOICES, default='engineer')
    token = models.CharField(max_length=255, unique=True, default=generate_token)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(default=get_expiry_time)
    accepted_by = models.OneToOneField(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='invitation_accepted')
    
    class Meta:
        db_table = 'user_invitations'
        unique_together = ('tenant', 'email')
    
    def is_valid(self):
        return self.status == 'pending' and timezone.now() <= self.expires_at


# ============================================================================
# AUDIT LOG
# ============================================================================

class AuditLog(models.Model):
    ACTION_CHOICES = [
        ('USER_CREATED', 'User Created'),
        ('USER_UPDATED', 'User Updated'),
        ('USER_DELETED', 'User Deleted'),
        ('LOGIN', 'Login'),
        ('LOGOUT', 'Logout'),
        ('API_KEY_CREATED', 'API Key Created'),
    ]
    
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
    
    class Meta:
        db_table = 'audit_logs'


# ============================================================================
# API KEY
# ============================================================================

class APIKey(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name='api_keys')
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    name = models.CharField(max_length=255)
    key = models.CharField(max_length=255, unique=True, db_index=True)
    is_active = models.BooleanField(default=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        db_table = 'api_keys'
        unique_together = ('tenant', 'name')
    
    def is_valid(self):
        return self.is_active