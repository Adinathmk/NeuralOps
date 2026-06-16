import json

from channels.generic.websocket import AsyncWebsocketConsumer


class CollaborationConsumer(AsyncWebsocketConsumer):

    async def connect(self):
        self.user_id = self.scope.get("user_id")
        self.tenant_id = self.scope.get("tenant_id")
        requested_tenant_id = self.scope["url_route"]["kwargs"]["tenant_id"]

        if not self.user_id:
            await self.close(code=4001)
            return

        # Users can only subscribe to their own tenant's collaboration channel
        if str(self.tenant_id) != str(requested_tenant_id):
            await self.close(code=4003)
            return

        self.group_name = f"collaboration_{self.tenant_id}"
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        if hasattr(self, "group_name"):
            await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def receive(self, text_data):
        pass

    async def collaboration_message(self, event):
        await self.send(
            text_data=json.dumps(
                {"type": "collaboration.message", "data": event["data"]}
            )
        )

    async def collaboration_status_change(self, event):
        await self.send(
            text_data=json.dumps(
                {"type": "collaboration.status_change", "data": event["data"]}
            )
        )

    async def collaboration_mention(self, event):
        await self.send(
            text_data=json.dumps(
                {"type": "collaboration.mention", "data": event["data"]}
            )
        )

    async def collaboration_assignment(self, event):
        await self.send(
            text_data=json.dumps(
                {"type": "collaboration.assignment", "data": event["data"]}
            )
        )
