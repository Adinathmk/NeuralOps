from django.urls import path
from .views import (
    HealthCheckView, RegisterView, LoginView, TokenRefreshView, MeView
)

urlpatterns = [
    path('health', HealthCheckView.as_view(), name='health'),
    path('auth/register', RegisterView.as_view(), name='register'),
    path('auth/login', LoginView.as_view(), name='login'),
    path('auth/refresh-token', TokenRefreshView.as_view(), name='refresh-token'),
    path('auth/me', MeView.as_view(), name='me'),
]