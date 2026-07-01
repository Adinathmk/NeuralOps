import logging

from django.utils import timezone
from core.exceptions import NotFoundException

from ..cache import cache_manager
from ..models import AuditLog, UserSession

logger = logging.getLogger(__name__)


class SessionService:

    @staticmethod
    def get_active_sessions(user_id, current_sid=None):
        """
        Returns a serialized list of all active, non-revoked sessions
        for the given user, ordered by most recent activity.
        """
        sessions = list(UserSession.objects.filter(
            user_id=user_id, is_active=True, is_revoked=False, expires_at__gt=timezone.now()
        ))

        session_data = []
        for session in sessions:
            real_time_activity = cache_manager.get_session_activity(str(session.id))
            last_activity = real_time_activity if real_time_activity else session.last_activity_at.isoformat()
            
            session_data.append({
                "id": str(session.id),
                "device_name": session.device_name,
                "ip_address": session.ip_address,
                "is_current": str(session.id) == str(current_sid),
                "last_activity": last_activity,
                "created_at": session.created_at.isoformat(),
                "expires_at": session.expires_at.isoformat(),
            })

        # Sort by most recent activity
        session_data.sort(key=lambda x: x["last_activity"], reverse=True)
        return session_data

    @staticmethod
    def revoke_session(session_id, user_id, user_email):
        """
        Revokes a specific session by ID (scoped to the requesting user),
        blocklists the token to prevent reuse, and writes a TOKEN_REVOKED
        audit log entry.
        Raises NotFoundException if session does not exist.
        """
        try:
            session = UserSession.objects.get(id=session_id, user_id=user_id)
        except UserSession.DoesNotExist:
            raise NotFoundException("Session not found")

        session.revoke()

        # Add session to blocklist (prevent reuse of any tokens tied to it)
        remaining = int((session.expires_at - timezone.now()).total_seconds())
        if remaining > 0:
            cache_manager.blocklist_session(str(session.id), remaining)

        logger.info(f"User {user_email} revoked session {session_id}")

        AuditLog.log(
            action="TOKEN_REVOKED",
            user_email=user_email,
            tenant=session.tenant,
            resource_type="UserSession",
            resource_id=str(session_id),
            description=f"Session revoked — device: {session.device_name or 'unknown'}",
        )
