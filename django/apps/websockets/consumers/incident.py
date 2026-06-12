import json
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from analytics.models import IncidentSnapshot


class IncidentConsumer(AsyncWebsocketConsumer):
    
    async def connect(self):
        self.incident_id = self.scope["url_route"]["kwargs"]["incident_id"]
        self.tenant_id = self.scope.get("tenant_id")
        self.user_id = self.scope.get("user_id")
        
        # Reject unauthenticated connections
        if not self.user_id or not self.tenant_id:
            await self.close(code=4001)
            return
        
        # Verify this tenant owns this incident (DB-1 incident_snapshots)
        if not await self._verify_incident_access():
            await self.close(code=4003)
            return
        
        # Join the incident channel group
        self.group_name = f"incident_{self.incident_id}"
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()
        
        # Send current incident state immediately on connect
        snapshot = await self._get_incident_snapshot()
        if snapshot:
            await self.send(text_data=json.dumps({
                "type": "incident.current_state",
                "data": snapshot
            }))
    
    async def disconnect(self, close_code):
        if hasattr(self, "group_name"):
            await self.channel_layer.group_discard(self.group_name, self.channel_name)
    
    async def receive(self, text_data):
        # Incidents are read-only streams; clients don't send data
        pass
    
    # --- Channel layer message handlers ---
    # Method names must match the "type" field in group_send() calls,
    # with dots replaced by underscores.
    
    async def incident_update(self, event):
        """Handler for type: 'incident.update' messages from the channel layer."""
        await self.send(text_data=json.dumps({
            "type": "incident.update",
            "data": event["data"]
        }))
    
    async def incident_analysis_complete(self, event):
        """Handler for type: 'incident.analysis_complete' — pushed by FastAPI via Redis."""
        await self.send(text_data=json.dumps({
            "type": "incident.analysis_complete",
            "data": event["data"]
        }))
    
    @database_sync_to_async
    def _verify_incident_access(self):
        return IncidentSnapshot.objects.filter(
            incident_id=self.incident_id,
            tenant_id=self.tenant_id
        ).exists()
    
    @database_sync_to_async
    def _get_incident_snapshot(self):
        try:
            snap = IncidentSnapshot.objects.get(
                incident_id=self.incident_id,
                tenant_id=self.tenant_id
            )
            return {
                "incident_id": str(snap.incident_id),
                "status": snap.status,
                "severity": snap.severity,
                "error_type": snap.error_type,
                "service_name": snap.service_name,
                "root_cause": snap.root_cause,
                "suggested_fix": snap.suggested_fix,
                "confidence_score": snap.confidence_score,
                "occurrence_count": snap.occurrence_count,
            }
        except IncidentSnapshot.DoesNotExist:
            return None
