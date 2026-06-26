"""
fastapi/tests/unit/test_config_sync_initial_index.py

Unit tests for the Phase 3 initial-indexing dispatch helpers in:
  fastapi/app/queue/kafka/consumers/config_sync.py

  - _resolve_branch_sha()
  - _maybe_dispatch_initial_index()

Patching strategy
-----------------
Both helpers use LOCAL imports to avoid circular dependencies:

  _resolve_branch_sha          → `from app.services.github_auth import get_installation_token`
  _maybe_dispatch_initial_index → `from app.worker.tasks.index_code import index_code`

Because these imports happen INSIDE the function at call time, the names
never appear on the `config_sync` module namespace.  The correct patch
targets are therefore in the SOURCE module:

  "app.services.github_auth.get_installation_token"
  "app.worker.tasks.index_code.index_code"

unittest.mock.patch replaces the name on its owning module and the local
`from … import` picks up the replacement transparently.
"""

from __future__ import annotations

import sys
import httpx
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.services.github_auth  # noqa: F401
from app.queue.kafka.consumers.config_sync import (
    _maybe_dispatch_initial_index,
    _resolve_branch_sha,
)

# ---------------------------------------------------------------------------
# Ensure app.worker.tasks.index_code is importable despite aiokafka stub.
# The stub in conftest stubs aiokafka at the top level; index_code also uses
# celery's @shared_task decorator which needs celery to be importable.
# ---------------------------------------------------------------------------
# (These are already handled by the conftest sys.modules stubs for aiokafka
#  and asyncpg.  celery is now installed in fvenv, so no extra stub needed.)


# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------

_FAKE_SHA = "abc1234def5678abc1234def5678abc1234def56"

_GITHUB_DATA_FULL = {
    "repo_url": "https://github.com/my-org/my-repo",
    "repo_owner": "my-org",
    "repo_name": "my-repo",
    "installation_id": 123456,
    "default_branch": "main",
    "indexing_status": "pending",
    "last_indexed_commit": None,
}


def _make_github_data(**overrides) -> dict:
    data = dict(_GITHUB_DATA_FULL)
    data.update(overrides)
    return data


# ---------------------------------------------------------------------------
# _resolve_branch_sha — unit tests
# ---------------------------------------------------------------------------


class TestResolveBranchSha:
    """Tests for _resolve_branch_sha()."""

    @pytest.mark.asyncio
    async def test_returns_sha_on_success(self):
        """Happy path: GitHub returns 200 with a commit SHA."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"commit": {"sha": _FAKE_SHA}}

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)

        with (
            patch(
                "app.services.github_auth.get_installation_token",
                new_callable=AsyncMock,
                return_value="plaintext_token",
            ),
            patch("httpx.AsyncClient") as mock_client_cls,
        ):
            mock_client_cls.return_value.__aenter__ = AsyncMock(
                return_value=mock_client
            )
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=None)

            result = await _resolve_branch_sha("my-org", "my-repo", "main", 123456)

        assert result == _FAKE_SHA

    @pytest.mark.asyncio
    async def test_returns_none_on_non_200(self):
        """GitHub returns a non-200 status (e.g. 401 bad PAT) → None."""
        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_response.text = "Bad credentials"

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)

        with (
            patch(
                "app.services.github_auth.get_installation_token",
                new_callable=AsyncMock,
                return_value="plaintext_token",
            ),
            patch("httpx.AsyncClient") as mock_client_cls,
        ):
            mock_client_cls.return_value.__aenter__ = AsyncMock(
                return_value=mock_client
            )
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=None)

            result = await _resolve_branch_sha("my-org", "my-repo", "main", 123456)

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_token_fetch_failure(self):
        """Token fetch fails → None (no network call made)."""
        with patch(
            "app.services.github_auth.get_installation_token",
            new_callable=AsyncMock,
            side_effect=RuntimeError("bad auth"),
        ):
            result = await _resolve_branch_sha("my-org", "my-repo", "main", 123456)

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_network_error(self):
        """httpx raises a connection error → None."""
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=Exception("connection refused"))

        with (
            patch(
                "app.services.github_auth.get_installation_token",
                new_callable=AsyncMock,
                return_value="plaintext_token",
            ),
            patch("httpx.AsyncClient") as mock_client_cls,
        ):
            mock_client_cls.return_value.__aenter__ = AsyncMock(
                return_value=mock_client
            )
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=None)

            result = await _resolve_branch_sha("my-org", "my-repo", "main", 123456)

        assert result is None


# ---------------------------------------------------------------------------
# _maybe_dispatch_initial_index — unit tests
# ---------------------------------------------------------------------------

# Correct patch targets (names live in the SOURCE module, not config_sync):
_PATCH_RESOLVE = "app.queue.kafka.consumers.config_sync._resolve_branch_sha"
_PATCH_TASK = "app.worker.tasks.index_code.index_code"


class TestMaybeDispatchInitialIndex:
    """
    Tests for _maybe_dispatch_initial_index().

    Guards covered:
      1. incoming_status != 'pending'              → skip
      2. previous_status in ('indexing','indexed') → skip  (idempotency)
      3. Missing required fields                   → skip
      4. _resolve_branch_sha returns None          → skip
      otherwise                                    → dispatch
    """

    # ── Guard 1: incoming status is not 'pending' ──────────────────────────

    @pytest.mark.asyncio
    async def test_skips_when_incoming_status_is_not_pending(self):
        """If the incoming payload status is 'indexed', do nothing."""
        data = _make_github_data(indexing_status="indexed")

        with patch(_PATCH_TASK) as mock_task:
            await _maybe_dispatch_initial_index(
                tenant_id="tenant-uuid-123",
                github_data=data,
                previous_status=None,
            )

        mock_task.delay.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_when_incoming_status_is_none(self):
        """If indexing_status key is absent from payload, do nothing."""
        data = _make_github_data(indexing_status=None)

        with patch(_PATCH_TASK) as mock_task:
            await _maybe_dispatch_initial_index(
                tenant_id="tenant-uuid-123",
                github_data=data,
                previous_status=None,
            )

        mock_task.delay.assert_not_called()

    # ── Guard 2: idempotency ───────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_skips_when_previous_status_is_indexed(self):
        """Kafka replay: DB already says 'indexed' → do not re-dispatch."""
        data = _make_github_data(indexing_status="pending")

        with patch(_PATCH_TASK) as mock_task:
            await _maybe_dispatch_initial_index(
                tenant_id="tenant-uuid-123",
                github_data=data,
                previous_status="indexed",
            )

        mock_task.delay.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_when_previous_status_is_indexing(self):
        """Worker already mid-run ('indexing') → do not launch duplicate."""
        data = _make_github_data(indexing_status="pending")

        with patch(_PATCH_TASK) as mock_task:
            await _maybe_dispatch_initial_index(
                tenant_id="tenant-uuid-123",
                github_data=data,
                previous_status="indexing",
            )

        mock_task.delay.assert_not_called()

    # ── Guard 3: missing required fields ──────────────────────────────────

    @pytest.mark.asyncio
    async def test_skips_when_installation_id_is_missing(self):
        """installation_id absent → cannot fetch token → skip."""
        data = _make_github_data(indexing_status="pending", installation_id=None)

        with patch(_PATCH_TASK) as mock_task:
            await _maybe_dispatch_initial_index(
                tenant_id="tenant-uuid-123",
                github_data=data,
                previous_status=None,
            )

        mock_task.delay.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_when_repo_owner_is_missing(self):
        """repo_owner absent → skip."""
        data = _make_github_data(indexing_status="pending", repo_owner=None)

        with patch(_PATCH_TASK) as mock_task:
            await _maybe_dispatch_initial_index(
                tenant_id="tenant-uuid-123",
                github_data=data,
                previous_status=None,
            )

        mock_task.delay.assert_not_called()

    # ── Guard 4: SHA resolution fails ─────────────────────────────────────

    @pytest.mark.asyncio
    async def test_skips_when_sha_resolution_fails(self):
        """
        All fields present but GitHub API returns None.
        Row stays 'pending'; index_code.delay must NOT be called.
        """
        data = _make_github_data(indexing_status="pending")

        with (
            patch(
                _PATCH_RESOLVE,
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(_PATCH_TASK) as mock_task,
        ):
            await _maybe_dispatch_initial_index(
                tenant_id="tenant-uuid-123",
                github_data=data,
                previous_status=None,
            )

        mock_task.delay.assert_not_called()

    # ── Happy paths ────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_dispatches_on_fresh_connection_no_prior_status(self):
        """
        Brand-new row (previous_status=None), incoming='pending'.
        All guards pass → index_code.delay() called once with is_initial=True.
        """
        data = _make_github_data(indexing_status="pending")

        with (
            patch(
                _PATCH_RESOLVE,
                new_callable=AsyncMock,
                return_value=_FAKE_SHA,
            ),
            patch(_PATCH_TASK) as mock_task,
        ):
            await _maybe_dispatch_initial_index(
                tenant_id="tenant-uuid-123",
                github_data=data,
                previous_status=None,
            )

        mock_task.delay.assert_called_once_with(
            tenant_id="tenant-uuid-123",
            repo_url="https://github.com/my-org/my-repo",
            commit_sha=_FAKE_SHA,
            is_initial=True,
        )

    @pytest.mark.asyncio
    async def test_dispatches_when_previous_status_is_failed(self):
        """
        Previous run failed; user re-connected (new 'pending' event).
        We must retry → dispatch.
        """
        data = _make_github_data(indexing_status="pending")

        with (
            patch(
                _PATCH_RESOLVE,
                new_callable=AsyncMock,
                return_value=_FAKE_SHA,
            ),
            patch(_PATCH_TASK) as mock_task,
        ):
            await _maybe_dispatch_initial_index(
                tenant_id="tenant-uuid-123",
                github_data=data,
                previous_status="failed",
            )

        mock_task.delay.assert_called_once()

    @pytest.mark.asyncio
    async def test_dispatches_when_previous_status_is_pending(self):
        """
        Duplicate Kafka delivery before any task ran.
        previous='pending' is not in the skip set → still dispatch.
        """
        data = _make_github_data(indexing_status="pending")

        with (
            patch(
                _PATCH_RESOLVE,
                new_callable=AsyncMock,
                return_value=_FAKE_SHA,
            ),
            patch(_PATCH_TASK) as mock_task,
        ):
            await _maybe_dispatch_initial_index(
                tenant_id="tenant-uuid-123",
                github_data=data,
                previous_status="pending",
            )

        mock_task.delay.assert_called_once()

    @pytest.mark.asyncio
    async def test_uses_main_as_default_branch_when_absent(self):
        """
        'default_branch' absent from payload → helper falls back to 'main'
        when calling _resolve_branch_sha.
        """
        data = _make_github_data(indexing_status="pending", default_branch=None)

        with (
            patch(
                _PATCH_RESOLVE,
                new_callable=AsyncMock,
                return_value=_FAKE_SHA,
            ) as mock_resolve,
            patch(_PATCH_TASK),
        ):
            await _maybe_dispatch_initial_index(
                tenant_id="tenant-uuid-123",
                github_data=data,
                previous_status=None,
            )

        # Third positional arg to _resolve_branch_sha is `branch`.
        args = mock_resolve.call_args.args
        assert args[2] == "main"
