"""
django/apps/integrations/urls.py
"""

from django.urls import path

from .views import GitHubIntegrationView, GitHubAvailableReposView

urlpatterns = [
    path("github/", GitHubIntegrationView.as_view(), name="github-integration"),
    path("github/available-repos/", GitHubAvailableReposView.as_view(), name="github-available-repos"),
]
