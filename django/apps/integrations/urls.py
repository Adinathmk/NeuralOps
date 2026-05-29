"""
django/apps/integrations/urls.py
"""

from django.urls import path

from .views import GitHubIntegrationView

urlpatterns = [
    path("github/", GitHubIntegrationView.as_view(), name="github-integration"),
]
