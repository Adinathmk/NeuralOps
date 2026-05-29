from django.urls import path

from .views import TenantConfigView

urlpatterns = [
    path("config/", TenantConfigView.as_view(), name="tenant-config"),
]
