from django.urls import path

from .views import (
    CancelInvitationView,
    ChangePasswordView,
    ConfirmMFAView,
    DisableMFAView,
    ForgotPasswordView,
    GitHubOAuthCallbackView,
    GoogleOAuthCallbackView,
    HealthCheckView,
    InviteEngineerView,
    JoinWithEmailPasswordView,
    ListInvitationsView,
    ListNotificationsView,
    ListTeamMembersView,
    LoginView,
    LogoutView,
    MarkNotificationReadView,
    MeView,
    RegisterView,
    ResendInvitationView,
    ResendVerificationEmailView,
    ResetPasswordView,
    RevokeSessionView,
    SessionListView,
    SetupMFAView,
    TokenRefreshView,
    ValidateInvitationView,
    VerifyEmailView,
    VerifyMFATokenView,
)

urlpatterns = [
    path("health", HealthCheckView.as_view(), name="health"),
    path("auth/register", RegisterView.as_view(), name="register"),
    path("auth/login", LoginView.as_view(), name="login"),
    path("auth/logout", LogoutView.as_view(), name="logout"),
    path("auth/refresh-token", TokenRefreshView.as_view(), name="refresh"),
    path("auth/me", MeView.as_view(), name="me"),
    path("auth/sessions", SessionListView.as_view(), name="sessions"),
    path(
        "auth/sessions/<uuid:session_id>/revoke",
        RevokeSessionView.as_view(),
        name="revoke-session",
    ),
    path("auth/verify-email", VerifyEmailView.as_view(), name="verify_email"),
    path(
        "auth/resend-verification",
        ResendVerificationEmailView.as_view(),
        name="resend_verification",
    ),
    path("auth/forgot-password", ForgotPasswordView.as_view(), name="forgot_password"),
    path("auth/reset-password", ResetPasswordView.as_view(), name="reset_password"),
    path("auth/change-password", ChangePasswordView.as_view(), name="change_password"),
    path(
        "auth/google/callback",
        GoogleOAuthCallbackView.as_view(),
        name="google_oauth_callback",
    ),  # ← ADD
    path(
        "auth/github/callback",
        GitHubOAuthCallbackView.as_view(),
        name="github_oauth_callback",
    ),  # ← ADD
    path("invitations/send", InviteEngineerView.as_view(), name="invite_engineer"),
    path(
        "invitations/validate",
        ValidateInvitationView.as_view(),
        name="validate_invitation",
    ),
    path(
        "invitations/join",
        JoinWithEmailPasswordView.as_view(),
        name="join_with_invitation",
    ),
    path("team/members", ListTeamMembersView.as_view(), name="list_team_members"),
    path("invitations/", ListInvitationsView.as_view(), name="list_invitations"),
    path(
        "invitations/<uuid:invitation_id>/cancel",
        CancelInvitationView.as_view(),
        name="cancel_invitation",
    ),
    path(
        "invitations/<uuid:invitation_id>/resend",
        ResendInvitationView.as_view(),
        name="resend_invitation",
    ),
    path("auth/mfa/setup", SetupMFAView.as_view(), name="mfa_setup"),
    path("auth/mfa/confirm", ConfirmMFAView.as_view(), name="mfa_confirm"),
    path("auth/mfa/verify", VerifyMFATokenView.as_view(), name="mfa_verify"),
    path("auth/mfa/disable", DisableMFAView.as_view(), name="mfa_disable"),
    # Notifications
    path(
        "users/<uuid:user_id>/notifications",
        ListNotificationsView.as_view(),
        name="list_notifications",
    ),
    path(
        "notifications/<uuid:notification_id>/read",
        MarkNotificationReadView.as_view(),
        name="mark_notification_read",
    ),
]
