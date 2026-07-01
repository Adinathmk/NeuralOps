from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import DashboardAnalyticsViewSet

router = DefaultRouter()
router.register(r"dashboard", DashboardAnalyticsViewSet, basename="dashboard-analytics")

urlpatterns = [
    path("", include(router.urls)),
]
