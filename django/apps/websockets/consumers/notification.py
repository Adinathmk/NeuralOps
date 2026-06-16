import json

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer


class NotificationConsumer(AsyncWebsocketConsumer):

    async def connect(self):
        self.user_id = self.scope.get("user_id")
        self.tenant_id = self.scope.get("tenant_id")
        requested_user_id = self.scope["url_route"]["kwargs"]["user_id"]

        if not self.user_id:
            await self.close(code=4001)
            return

        # Users can only subscribe to their own notification channel
        if str(self.user_id) != str(requested_user_id):
            await self.close(code=4003)
            return

        self.group_name = f"notifications_{self.tenant_id}_{self.user_id}"
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        if hasattr(self, "group_name"):
            await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def receive(self, text_data):
        # Handle read acknowledgements from client
        data = json.loads(text_data)
        if data.get("type") == "notification.ack":
            await self._mark_notification_read(data.get("notification_id"))

    async def notification_new(self, event):
        """Handles new in-app notifications pushed by the Django notification system."""
        await self.send(
            text_data=json.dumps({"type": "notification.new", "data": event["data"]})
        )

    async def incident_analysis_complete(self, event):
        """Pushed by FastAPI via Redis when AI analysis finishes for an incident
        assigned to or watched by this user."""
        await self.send(
            text_data=json.dumps(
                {"type": "incident.analysis_complete", "data": event["data"]}
            )
        )

    @database_sync_to_async
    def _mark_notification_read(self, notification_id):
        if notification_id:
            # Update notification model (to be created in Phase 4)
            pass
