"""
django/apps/integrations/urls.py
"""

from django.urls import path

from .views import (
    GitHubAvailableReposView,
    GitHubIntegrationDetailView,
    GitHubIntegrationListCreateView,
    ServiceRepoMappingDetailView,
    ServiceRepoMappingListCreateView,
)

urlpatterns = [
    path(
        "github/",
        GitHubIntegrationListCreateView.as_view(),
        name="github-integration-list",
    ),
    path(
        "github/<uuid:pk>/",
        GitHubIntegrationDetailView.as_view(),
        name="github-integration-detail",
    ),
    path(
        "github/available-repos/",
        GitHubAvailableReposView.as_view(),
        name="github-available-repos",
    ),
    path(
        "mappings/",
        ServiceRepoMappingListCreateView.as_view(),
        name="service-mapping-list",
    ),
    path(
        "mappings/<uuid:mapping_id>/",
        ServiceRepoMappingDetailView.as_view(),
        name="service-mapping-detail",
    ),
]
