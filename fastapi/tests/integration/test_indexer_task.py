import io
import tarfile
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.models.code_index import CodeIndex
from app.models.snapshots import TenantSnapshot
from app.worker.tasks.index_code import _decrypt, _run_index, index_code


@pytest.mark.asyncio
class TestIndexerTask:
    """
    Integration tests for the index_code worker task.
    Mocks out GitHub API downloads/uploads, feeds simulated repository archives,
    and asserts database upserts, incremental prunes, and edge/error paths.
    """

    @pytest.fixture(autouse=True)
    def patch_session_local(self, db_conn):
        """
        Patch AsyncSessionLocal to use the transactional connection db_conn.
        Ensures all DB operations in the indexing coroutine are isolated and rolled back.
        """
        SessionLocal = async_sessionmaker(
            bind=db_conn,
            class_=AsyncSession,
            expire_on_commit=False,
            autocommit=False,
            autoflush=False,
        )
        with patch("app.worker.tasks.index_code.AsyncSessionLocal", new=SessionLocal):
            yield

    @pytest.fixture
    def setup_fernet_key(self):
        """Ensure FERNET_ENCRYPTION_KEY is populated so credentials decryption works."""
        settings = get_settings()
        old_key = getattr(settings, "FERNET_ENCRYPTION_KEY", None)
        if not old_key:
            key = Fernet.generate_key().decode()
            settings.FERNET_ENCRYPTION_KEY = key
            yield key
            settings.FERNET_ENCRYPTION_KEY = old_key
        else:
            yield old_key

    def make_in_memory_tarball(self, files_dict):
        """Create a compressed tarball in memory containing files from files_dict."""
        tar_stream = io.BytesIO()
        with tarfile.open(fileobj=tar_stream, mode="w:gz") as tar:
            for filepath, content in files_dict.items():
                content_bytes = content.encode("utf-8")
                tarinfo = tarfile.TarInfo(name=f"test-repo-main/{filepath}")
                tarinfo.size = len(content_bytes)
                tar.addfile(tarinfo, io.BytesIO(content_bytes))
        return tar_stream.getvalue()

    # ── Happy Path Tests ──────────────────────────────────────────────────

    async def test_index_code_task_success_initial(self, db_session, setup_fernet_key):
        """Verify initial full-repository import AST indexing and database saves."""
        tenant_uuid = uuid.uuid4()
        tenant_id_str = str(tenant_uuid)

        f = Fernet(setup_fernet_key)
        encrypted_pat = f.encrypt(b"ghp_dummypat").decode()

        snapshot = TenantSnapshot(
            tenant_id=tenant_uuid,
            plan_tier="enterprise",
            github_repo_url="https://github.com/neuralops/backend",
            github_repo_owner="neuralops",
            github_repo_name="backend",
            encrypted_github_pat=encrypted_pat,
            github_default_branch="main",
            github_indexing_status="pending",
            is_suspended=False,
            source_version=1,
        )
        db_session.add(snapshot)
        await db_session.flush()

        files = {
            "services/charge.py": (
                "class ChargeService:\n" "    def process(self):\n" "        pass\n"
            ),
            "utils/Logger.java": (
                "package com.neuralops.utils;\n"
                "public class Logger {\n"
                "    public void log(String msg) {}\n"
                "}\n"
            ),
        }
        tarball_bytes = self.make_in_memory_tarball(files)

        mock_upload = AsyncMock()
        mock_download = AsyncMock(return_value=tarball_bytes)

        with (
            patch("app.worker.tasks.index_code._upload_file_to_s3", new=mock_upload),
            patch(
                "app.worker.tasks.index_code._download_repo_tarball", new=mock_download
            ),
        ):

            await _run_index(
                tenant_id_str=tenant_id_str,
                repo_url="https://github.com/neuralops/backend",
                commit_sha="a1b2c3d4e5f6",
                changed_files=[],
                removed_files=[],
                is_initial=True,
            )

        db_session.expire_all()
        updated_snap = await db_session.get(TenantSnapshot, tenant_uuid)
        assert updated_snap.github_indexing_status == "indexed"
        assert updated_snap.github_last_indexed_commit == "a1b2c3d4e5f6"

        result = await db_session.execute(
            select(CodeIndex).where(CodeIndex.tenant_id == tenant_uuid)
        )
        symbols = result.scalars().all()
        assert len(symbols) >= 2

        charge_service_sym = next(
            s for s in symbols if s.symbol_name == "ChargeService"
        )
        assert charge_service_sym.chunk_type == "class"
        assert charge_service_sym.file_path == "services/charge.py"
        assert charge_service_sym.last_commit == "a1b2c3d4e5f6"

    async def test_index_code_incremental_update_and_remove(
        self, db_session, setup_fernet_key
    ):
        """Verify incremental push-webhook AST indexing updates and file prunes."""
        tenant_uuid = uuid.uuid4()
        tenant_id_str = str(tenant_uuid)

        f = Fernet(setup_fernet_key)
        encrypted_pat = f.encrypt(b"ghp_dummypat").decode()

        snapshot = TenantSnapshot(
            tenant_id=tenant_uuid,
            plan_tier="enterprise",
            github_repo_url="https://github.com/neuralops/backend",
            github_repo_owner="neuralops",
            github_repo_name="backend",
            encrypted_github_pat=encrypted_pat,
            github_default_branch="main",
            github_indexing_status="indexed",
            github_last_indexed_commit="a1b2c3d4",
            is_suspended=False,
            source_version=1,
        )
        db_session.add(snapshot)

        existing_index = CodeIndex(
            id=uuid.uuid4(),
            tenant_id=tenant_uuid,
            repo_url="https://github.com/neuralops/backend",
            file_path="app/core.py",
            symbol_name="CoreService",
            chunk_type="class",
            start_line=1,
            end_line=10,
            calls=[],
            imports=[],
            s3_key="code/tenant/backend/a1b2c3d4/app/core.py",
            last_commit="a1b2c3d4",
        )
        db_session.add(existing_index)
        await db_session.flush()

        # ── Step A: Test Incremental Update ──────────────────────────────────
        updated_code = (
            "class CoreService:\n" "    def initialize(self):\n" "        pass\n"
        )

        mock_upload = AsyncMock()
        mock_fetch = AsyncMock(return_value=updated_code.encode("utf-8"))

        with (
            patch("app.worker.tasks.index_code._upload_file_to_s3", new=mock_upload),
            patch("app.worker.tasks.index_code._fetch_file_content", new=mock_fetch),
        ):

            await _run_index(
                tenant_id_str=tenant_id_str,
                repo_url="https://github.com/neuralops/backend",
                commit_sha="f6e5d4c3",
                changed_files=["app/core.py"],
                removed_files=[],
                is_initial=False,
            )

        db_session.expire_all()
        result = await db_session.execute(
            select(CodeIndex).where(
                CodeIndex.tenant_id == tenant_uuid, CodeIndex.file_path == "app/core.py"
            )
        )
        symbols = result.scalars().all()
        assert len(symbols) == 2
        assert all(s.last_commit == "f6e5d4c3" for s in symbols)

        # ── Step B: Test Incremental Removal ──────────────────────────────────
        with patch(
            "app.worker.tasks.index_code._invalidate_redis_cache", new=AsyncMock()
        ):
            await _run_index(
                tenant_id_str=tenant_id_str,
                repo_url="https://github.com/neuralops/backend",
                commit_sha="99887766",
                changed_files=[],
                removed_files=["app/core.py"],
                is_initial=False,
            )

        db_session.expire_all()
        result = await db_session.execute(
            select(CodeIndex).where(
                CodeIndex.tenant_id == tenant_uuid, CodeIndex.file_path == "app/core.py"
            )
        )
        symbols = result.scalars().all()
        assert len(symbols) == 0

    # ── Edge Cases and Error Paths ────────────────────────────────────────

    async def test_index_code_missing_snapshot_raises_error(self):
        """Verify error is raised when tenant snapshot is missing in DB."""
        tenant_id_str = str(uuid.uuid4())
        with pytest.raises(RuntimeError) as exc_info:
            await _run_index(
                tenant_id_str=tenant_id_str,
                repo_url="https://github.com/neuralops/backend",
                commit_sha="a1b2c3d4",
                changed_files=[],
                removed_files=[],
                is_initial=True,
            )
        assert "TenantSnapshot not found" in str(exc_info.value)

    async def test_index_code_missing_pat_raises_error(self, db_session):
        """Verify error is raised when encrypted PAT is missing from snapshot."""
        tenant_uuid = uuid.uuid4()
        tenant_id_str = str(tenant_uuid)

        snapshot = TenantSnapshot(
            tenant_id=tenant_uuid,
            plan_tier="enterprise",
            github_repo_url="https://github.com/neuralops/backend",
            github_repo_owner="neuralops",
            github_repo_name="backend",
            encrypted_github_pat=None,  # Missing PAT
            is_suspended=False,
            source_version=1,
        )
        db_session.add(snapshot)
        await db_session.flush()

        with pytest.raises(RuntimeError) as exc_info:
            await _run_index(
                tenant_id_str=tenant_id_str,
                repo_url="https://github.com/neuralops/backend",
                commit_sha="a1b2c3d4",
                changed_files=[],
                removed_files=[],
                is_initial=True,
            )
        assert "No encrypted GitHub PAT configured" in str(exc_info.value)

    async def test_index_code_decryption_failure(self, db_session, setup_fernet_key):
        """Verify error is raised if Fernet decryption fails on corrupted pat ciphertext."""
        tenant_uuid = uuid.uuid4()
        tenant_id_str = str(tenant_uuid)

        snapshot = TenantSnapshot(
            tenant_id=tenant_uuid,
            plan_tier="enterprise",
            github_repo_url="https://github.com/neuralops/backend",
            github_repo_owner="neuralops",
            github_repo_name="backend",
            encrypted_github_pat="corrupt-cipher-text",
            is_suspended=False,
            source_version=1,
        )
        db_session.add(snapshot)
        await db_session.flush()

        with pytest.raises(RuntimeError) as exc_info:
            await _run_index(
                tenant_id_str=tenant_id_str,
                repo_url="https://github.com/neuralops/backend",
                commit_sha="a1b2c3d4",
                changed_files=[],
                removed_files=[],
                is_initial=True,
            )
        assert "PAT decryption failed" in str(exc_info.value)

    async def test_index_code_unsupported_extensions_gracefully_ignored(
        self, db_session, setup_fernet_key
    ):
        """Verify files with unsupported extensions are uploaded/processed, but produce no CodeIndex entries."""
        tenant_uuid = uuid.uuid4()
        tenant_id_str = str(tenant_uuid)

        f = Fernet(setup_fernet_key)
        encrypted_pat = f.encrypt(b"ghp_dummypat").decode()

        snapshot = TenantSnapshot(
            tenant_id=tenant_uuid,
            plan_tier="enterprise",
            github_repo_url="https://github.com/neuralops/backend",
            github_repo_owner="neuralops",
            github_repo_name="backend",
            encrypted_github_pat=encrypted_pat,
            github_default_branch="main",
            github_indexing_status="pending",
            is_suspended=False,
            source_version=1,
        )
        db_session.add(snapshot)
        await db_session.flush()

        # In-memory tarball with only unsupported file extensions (e.g. .go, .txt)
        files = {
            "main.go": "package main\nfunc main() {}\n",
            "README.md": "# NeuralOps Backend\n",
        }
        tarball_bytes = self.make_in_memory_tarball(files)

        mock_upload = AsyncMock()
        mock_download = AsyncMock(return_value=tarball_bytes)

        with (
            patch("app.worker.tasks.index_code._upload_file_to_s3", new=mock_upload),
            patch(
                "app.worker.tasks.index_code._download_repo_tarball", new=mock_download
            ),
        ):

            await _run_index(
                tenant_id_str=tenant_id_str,
                repo_url="https://github.com/neuralops/backend",
                commit_sha="a1b2c3d4e5f6",
                changed_files=[],
                removed_files=[],
                is_initial=True,
            )

        db_session.expire_all()
        # Verify status is indexed
        updated_snap = await db_session.get(TenantSnapshot, tenant_uuid)
        assert updated_snap.github_indexing_status == "indexed"

        # Verify no symbols inserted
        result = await db_session.execute(
            select(CodeIndex).where(CodeIndex.tenant_id == tenant_uuid)
        )
        symbols = result.scalars().all()
        assert len(symbols) == 0

    # ── Celery Wrapper Task Testing ───────────────────────────────────────

    async def test_celery_task_entrypoint(self):
        """Verify the synchronous Celery wrapper task executes without errors."""
        import inspect

        from app.worker.celery_app import celery_app as app

        tenant_uuid = uuid.uuid4()
        tenant_id_str = str(tenant_uuid)

        # Capture the coroutine passed to asyncio.run() so we can inspect it.
        captured_coros = []

        def fake_asyncio_run(coro):
            """Consume the coroutine without actually running an event loop."""
            captured_coros.append(coro)
            # Only close if it's an actual coroutine (AsyncMock returns one).
            if inspect.iscoroutine(coro):
                coro.close()

        # AsyncMock returns a real coroutine when called — required for asyncio.run().
        mock_run_index = AsyncMock(return_value=None)

        # task_always_eager makes apply() execute the task synchronously inline.
        app.conf.task_always_eager = True

        try:
            with (
                patch("app.worker.tasks.index_code._run_index", new=mock_run_index),
                patch(
                    "app.worker.tasks.index_code.asyncio.run",
                    side_effect=fake_asyncio_run,
                ),
            ):

                result = index_code.apply(
                    kwargs=dict(
                        tenant_id=tenant_id_str,
                        repo_url="https://github.com/neuralops/backend",
                        commit_sha="a1b2c3d4",
                        changed_files=["app.py"],
                        removed_files=["old.py"],
                        is_initial=False,
                    )
                )

            # EagerResult.get() returns the task return value.
            assert result.get() == {
                "status": "ok",
                "tenant_id": tenant_id_str,
                "commit_sha": "a1b2c3d4",
                "is_initial": False,
            }

            # Verify _run_index was called with the correct keyword arguments.
            mock_run_index.assert_called_once_with(
                tenant_id_str=tenant_id_str,
                repo_url="https://github.com/neuralops/backend",
                commit_sha="a1b2c3d4",
                changed_files=["app.py"],
                removed_files=["old.py"],
                is_initial=False,
            )

            # Verify asyncio.run() was called exactly once (wrapping the coroutine).
            assert len(captured_coros) == 1

        finally:
            # Restore default so other tests are not affected.
            app.conf.task_always_eager = False
