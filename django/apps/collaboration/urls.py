from django.urls import path

from .views import (
    ListIncidentStatusTransitionsView,
    ThreadMessageDeleteView,
    ThreadMessageListCreateView,
)

urlpatterns = [
    # GET  — list all messages for an incident thread (auto-creates thread on first call)
    # POST — post a new message to the thread
    path(
        "incidents/<uuid:incident_id>/messages/",
        ThreadMessageListCreateView.as_view(),
        name="thread-messages",
    ),
    # DELETE — soft-delete a specific message
    path(
        "incidents/<uuid:incident_id>/messages/<uuid:message_id>/",
        ThreadMessageDeleteView.as_view(),
        name="thread-message-delete",
    ),
    # GET — list status transitions
    path(
        "incidents/<uuid:incident_id>/status_transitions/",
        ListIncidentStatusTransitionsView.as_view(),
        name="status-transitions",
    ),
]
