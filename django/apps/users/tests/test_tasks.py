from datetime import timedelta

import pytest
from django.utils import timezone
from users.models import MFAVerificationToken, PasswordReset, UserInvitation
from users.tasks import cleanup_expired_invitations_task, cleanup_expired_tokens_task


@pytest.mark.django_db
def test_cleanup_expired_invitations(tenant, owner_user):
    # Create a pending invitation that is active
    inv1 = UserInvitation.objects.create(
        email="active@example.com",
        tenant=tenant,
        invited_by=owner_user,
        role="engineer",
        status="pending",
        expires_at=timezone.now() + timedelta(days=1),
    )

    # Create a pending invitation that is expired
    inv2 = UserInvitation.objects.create(
        email="expired@example.com",
        tenant=tenant,
        invited_by=owner_user,
        role="engineer",
        status="pending",
        expires_at=timezone.now() - timedelta(days=1),
    )

    # Run task
    count = cleanup_expired_invitations_task()

    # Check
    inv1.refresh_from_db()
    inv2.refresh_from_db()

    assert count == 1
    assert inv1.status == "pending"
    assert inv2.status == "expired"


@pytest.mark.django_db
def test_cleanup_expired_tokens(owner_user):
    # Expired token
    expired_token = MFAVerificationToken.objects.create(
        user=owner_user, expires_at=timezone.now() - timedelta(minutes=1)
    )
    # Active token
    active_token = MFAVerificationToken.objects.create(
        user=owner_user, expires_at=timezone.now() + timedelta(minutes=10)
    )

    # Expired PasswordReset
    expired_reset = PasswordReset.objects.create(
        user=owner_user,
        status="pending",
        expires_at=timezone.now() - timedelta(hours=1),
    )
    # Active PasswordReset
    active_reset = PasswordReset.objects.create(
        user=owner_user,
        status="pending",
        expires_at=timezone.now() + timedelta(hours=2),
    )

    # Run task
    result = cleanup_expired_tokens_task()

    assert result["mfa_deleted"] == 1
    assert result["password_resets_deleted"] == 1

    # Check database
    assert not MFAVerificationToken.objects.filter(id=expired_token.id).exists()
    assert MFAVerificationToken.objects.filter(id=active_token.id).exists()

    assert not PasswordReset.objects.filter(id=expired_reset.id).exists()
    assert PasswordReset.objects.filter(id=active_reset.id).exists()


def test_celery_beat_schedule_configuration():
    from django.conf import settings

    # Verify schedule exists
    assert settings.CELERY_BEAT_SCHEDULE is not None

    # Check cleanup invitations schedule
    inv_schedule = settings.CELERY_BEAT_SCHEDULE.get(
        "cleanup-expired-invitations-hourly"
    )
    assert inv_schedule is not None
    assert inv_schedule["task"] == "users.tasks.cleanup_expired_invitations_task"
    assert inv_schedule["kwargs"] == {"is_superadmin": True}

    # Check cleanup tokens schedule
    token_schedule = settings.CELERY_BEAT_SCHEDULE.get("cleanup-expired-tokens-hourly")
    assert token_schedule is not None
    assert token_schedule["task"] == "users.tasks.cleanup_expired_tokens_task"
    assert token_schedule["kwargs"] == {"is_superadmin": True}
