from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from .serializers import (
    RegisterSerializer, LoginSerializer, TokenRefreshSerializer, UserSerializer
)
from .authentication import JWTAuthentication
from .models import User


class HealthCheckView(APIView):
    """Health check endpoint - no auth required"""
    permission_classes = [AllowAny]
    
    def get(self, request):
        return Response({'status': 'healthy'}, status=status.HTTP_200_OK)


class RegisterView(APIView):
    """
    Register a new user and tenant.
    POST /api/auth/register
    {
        "email": "alice@example.com",
        "password": "SecurePass123",
        "password_confirm": "SecurePass123",
        "tenant_name": "Acme Corp",
        "first_name": "Alice",
        "last_name": "Engineer"
    }
    """
    permission_classes = [AllowAny]
    
    def post(self, request):
        serializer = RegisterSerializer(data=request.data)
        
        if serializer.is_valid():
            user = serializer.save()
            
            # Generate tokens
            access_token, refresh_token = JWTAuthentication.generate_tokens(user)
            
            return Response({
                'message': 'User registered successfully.',
                'user': UserSerializer(user).data,
                'access_token': access_token,
                'refresh_token': refresh_token,
            }, status=status.HTTP_201_CREATED)
        
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class LoginView(APIView):
    """
    Login user.
    POST /api/auth/login
    {
        "email": "alice@example.com",
        "password": "SecurePass123",
        "tenant_slug": "acme-corp"
    }
    """
    permission_classes = [AllowAny]
    
    def post(self, request):
        serializer = LoginSerializer(data=request.data)
        
        if serializer.is_valid():
            user = serializer.validated_data['user']
            
            # Generate tokens
            access_token, refresh_token = JWTAuthentication.generate_tokens(user)
            
            return Response({
                'message': 'Login successful.',
                'user': UserSerializer(user).data,
                'access_token': access_token,
                'refresh_token': refresh_token,
            }, status=status.HTTP_200_OK)
        
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class TokenRefreshView(APIView):
    """
    Refresh access token using refresh token.
    POST /api/auth/refresh-token
    {
        "refresh_token": "eyJ0eXAi..."
    }
    """
    permission_classes = [AllowAny]
    
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
            
            return Response({
                'access_token': access_token,
                'refresh_token': refresh_token,
            }, status=status.HTTP_200_OK)
        
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class MeView(APIView):
    """
    Get current user profile.
    GET /api/auth/me
    Headers: Authorization: Bearer <access_token>
    """
    permission_classes = [IsAuthenticated]
    authentication_classes = [JWTAuthentication]
    
    def get(self, request):
        user_id = request.user_id
        user = User.objects.get(id=user_id)
        return Response(UserSerializer(user).data, status=status.HTTP_200_OK)
