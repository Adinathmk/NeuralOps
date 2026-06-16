from django.urls import path

from .views import DeviceTokenView

urlpatterns = [
    path("register", DeviceTokenView.as_view()),
    path("register/<str:device_id>", DeviceTokenView.as_view()),
]
