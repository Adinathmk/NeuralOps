from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import AlertRuleViewSet

router = DefaultRouter()
router.register(r"alert-rules", AlertRuleViewSet, basename="alert-rules")

urlpatterns = [
    path("", include(router.urls)),
]
