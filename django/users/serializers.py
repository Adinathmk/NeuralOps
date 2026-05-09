from rest_framework import serializers
from django.contrib.auth.password_validation import validate_password
from .models import User, Tenant


class TenantSerializer(serializers.ModelSerializer):
    """Serialize tenant info"""
    class Meta:
        model = Tenant
        fields = ('id', 'name', 'slug', 'plan_tier', 'status', 'created_at')
        read_only_fields = ('id', 'created_at')


class UserSerializer(serializers.ModelSerializer):
    """Serialize user with tenant info"""
    tenant = TenantSerializer(read_only=True)
    full_name = serializers.SerializerMethodField(read_only=True)
    
    class Meta:
        model = User
        fields = ('id', 'email', 'first_name', 'last_name', 'full_name', 'role', 'tenant', 'created_at')
        read_only_fields = ('id', 'created_at')
    
    def get_full_name(self, obj):
        return obj.get_full_name()


class RegisterSerializer(serializers.Serializer):
    """Register a new user and create a tenant"""
    email = serializers.EmailField()
    password = serializers.CharField(
        min_length=8,
        max_length=128,
        write_only=True,
        style={'input_type': 'password'}
    )
    password_confirm = serializers.CharField(
        min_length=8,
        max_length=128,
        write_only=True,
        style={'input_type': 'password'}
    )
    tenant_name = serializers.CharField(max_length=255)
    first_name = serializers.CharField(max_length=255, required=False, allow_blank=True)
    last_name = serializers.CharField(max_length=255, required=False, allow_blank=True)
    
    def validate(self, data):
        """Validate email uniqueness and password match"""
        
        # Check passwords match
        if data['password'] != data['password_confirm']:
            raise serializers.ValidationError(
                {'password': 'Password fields did not match.'}
            )
        
        # Check email not already used in any tenant
        if User.objects.filter(email=data['email']).exists():
            raise serializers.ValidationError(
                {'email': 'Email already registered.'}
            )
        
        # Check tenant name doesn't exist
        if Tenant.objects.filter(name=data['tenant_name']).exists():
            raise serializers.ValidationError(
                {'tenant_name': 'Organization name already exists.'}
            )
        
        # Validate password strength
        try:
            validate_password(data['password'])
        except serializers.ValidationError as e:
            raise serializers.ValidationError({'password': e.messages})
        
        return data
    
    def create(self, validated_data):
        """Create tenant and user"""
        
        # Create tenant
        slug = validated_data['tenant_name'].lower().replace(' ', '-').replace('_', '-')
        tenant = Tenant.objects.create(
            name=validated_data['tenant_name'],
            slug=slug,
            plan_tier='free',
            status='active'
        )
        
        # Create user as tenant owner
        user = User.objects.create_user(
            email=validated_data['email'],
            password=validated_data['password'],
            tenant=tenant,
            first_name=validated_data.get('first_name', ''),
            last_name=validated_data.get('last_name', ''),
            role='owner',
            is_staff=False
        )
        
        return user


class LoginSerializer(serializers.Serializer):
    """Login with email, password, and tenant slug"""
    email = serializers.EmailField()
    password = serializers.CharField(
        write_only=True,
        style={'input_type': 'password'}
    )
    tenant_slug = serializers.CharField()
    
    def validate(self, data):
        """Validate credentials"""
        
        # Get tenant by slug
        try:
            tenant = Tenant.objects.get(slug=data['tenant_slug'])
        except Tenant.DoesNotExist:
            raise serializers.ValidationError(
                {'tenant_slug': 'Organization not found.'}
            )
        
        # Get user by email and tenant
        try:
            user = User.objects.get(email=data['email'], tenant=tenant)
        except User.DoesNotExist:
            raise serializers.ValidationError(
                {'email': 'Invalid email or password.'}
            )
        
        # Check password
        if not user.check_password(data['password']):
            raise serializers.ValidationError(
                {'password': 'Invalid email or password.'}
            )
        
        # Check user is active
        if not user.is_active:
            raise serializers.ValidationError(
                {'email': 'User account is inactive.'}
            )
        
        data['user'] = user
        return data


class TokenRefreshSerializer(serializers.Serializer):
    """Refresh access token using refresh token"""
    refresh_token = serializers.CharField()
    
    def validate_refresh_token(self, value):
        """Validate refresh token"""
        from .authentication import JWTAuthentication
        payload = JWTAuthentication.verify_token(value)
        
        if payload.get('type') != 'refresh':
            raise serializers.ValidationError('Invalid refresh token.')
        
        return value