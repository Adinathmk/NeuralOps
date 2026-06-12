from django.urls import re_path
from apps.websockets.consumers.incident import IncidentConsumer
from apps.websockets.consumers.notification import NotificationConsumer
from apps.websockets.consumers.collaboration import CollaborationConsumer

websocket_urlpatterns = [
    re_path(
        r"ws/incidents/(?P<incident_id>[0-9a-f-]+)/$",
        IncidentConsumer.as_asgi()
    ),
    re_path(
        r"ws/notifications/(?P<user_id>[0-9a-f-]+)/$",
        NotificationConsumer.as_asgi()
    ),
    re_path(
        r"ws/collaboration/(?P<tenant_id>[0-9a-f-]+)/$",
        CollaborationConsumer.as_asgi()
    ),
]
