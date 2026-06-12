from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync


def push_incident_update(incident_id: str, data: dict):
    """Push an incident update to all subscribers of that incident."""
    channel_layer = get_channel_layer()
    async_to_sync(channel_layer.group_send)(
        f"incident_{incident_id}",
        {
            "type": "incident.update",
            "data": data
        }
    )


def push_incident_analysis_complete(incident_id: str, tenant_id: str, data: dict):
    """
    Push analysis completion to the incident channel AND to all
    per-user notification channels for users assigned to this incident.
    """
    channel_layer = get_channel_layer()
    # Push to incident stream
    async_to_sync(channel_layer.group_send)(
        f"incident_{incident_id}",
        {"type": "incident.analysis_complete", "data": data}
    )


def push_notification(tenant_id: str, user_id: str, data: dict):
    """Push a notification to a specific user."""
    channel_layer = get_channel_layer()
    async_to_sync(channel_layer.group_send)(
        f"notifications_{tenant_id}_{user_id}",
        {"type": "notification.new", "data": data}
    )


def push_collaboration_event(tenant_id: str, event_type: str, data: dict):
    """Push a collaboration event (message, mention, assignment) to a tenant."""
    channel_layer = get_channel_layer()
    async_to_sync(channel_layer.group_send)(
        f"collaboration_{tenant_id}",
        {"type": f"collaboration.{event_type}", "data": data}
    )
