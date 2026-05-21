import logging

from ..models import UserSession, AuditLog
from ..cache import cache_manager
from core.exceptions import NotFoundException

logger = logging.getLogger(__name__)


class SessionService:

    @staticmethod
    def get_active_sessions(user_id):
        """
        Returns a serialized list of all active, non-revoked sessions
        for the given user, ordered by most recent activity.
        """
        sessions = UserSession.objects.filter(
            user_id=user_id,
            is_active=True,
            is_revoked=False
        ).order_by('-last_activity_at')

        return [
            {
                'id': str(session.id),
                'device_name': session.device_name,
                'ip_address': session.ip_address,
                'last_activity': session.last_activity_at.isoformat(),
                'created_at': session.created_at.isoformat(),
                'expires_at': session.expires_at.isoformat(),
            }
            for session in sessions
        ]

    @staticmethod
    def revoke_session(session_id, user_id, user_email):
        """
        Revokes a specific session by ID (scoped to the requesting user),
        blocklists the token to prevent reuse, and writes a TOKEN_REVOKED
        audit log entry.
        Raises NotFoundException if session does not exist.
        """
        try:
            session = UserSession.objects.get(
                id=session_id,
                user_id=user_id
            )
        except UserSession.DoesNotExist:
            raise NotFoundException('Session not found')

        session.revoke()

        # Add token to blocklist (prevent reuse)
        cache_manager.blocklist_token(session.session_id, 86400)

        logger.info(f"User {user_email} revoked session {session_id}")

        AuditLog.log(
            action='TOKEN_REVOKED',
            user_email=user_email,
            tenant=session.tenant,
            resource_type='UserSession',
            resource_id=str(session_id),
            description=f"Session revoked — device: {session.device_name or 'unknown'}",
        )

