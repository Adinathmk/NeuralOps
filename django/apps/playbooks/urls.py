from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import PlaybookViewSet

router = DefaultRouter()
router.register(r"playbooks", PlaybookViewSet, basename="playbooks")

urlpatterns = [
    path("", include(router.urls)),
]