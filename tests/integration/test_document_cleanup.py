import asyncio
import os
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.documents.models import Chunk, Document, Job
from app.documents.recovery import DocumentRecovery
from app.documents.repository import DocumentRepository
from app.documents.storage import FileStorage
from app.users.types import Identity

pytestmark = pytest.mark.integration


@pytest.fixture
async def cleanup_database() -> AsyncIterator[tuple[async_sessionmaker[AsyncSession], Identity]]:
    url = os.environ.get("LLM_LAB_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Use the isolated Docker Compose PostgreSQL test environment")
    engine = create_async_engine(url, hide_parameters=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    organization_id, user_id = uuid4(), uuid4()
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text("INSERT INTO organizations (id, name) VALUES (:id, 'Cleanup test org')"),
                {"id": organization_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO users (id, organization_id, name) "
                    "VALUES (:id, :organization_id, 'Cleanup test user')"
                ),
                {"id": user_id, "organization_id": organization_id},
            )
        yield sessions, Identity(user_id, organization_id)
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM organizations WHERE id = :id"),
                {"id": organization_id},
            )
        await engine.dispose()


async def _stored_document(
    sessions: async_sessionmaker[AsyncSession],
    identity: Identity,
    storage: FileStorage,
) -> tuple[UUID, Path]:
    document_id = uuid4()
    path = storage.document_path(identity.organization_id, document_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"%PDF-1.4\nsource")
    await DocumentRepository(sessions).create(identity, document_id, "source.pdf", str(path))
    return document_id, path


async def _add_chunk(
    sessions: async_sessionmaker[AsyncSession], identity: Identity, document_id: UUID
) -> None:
    async with sessions.begin() as session:
        session.add(
            Chunk(
                document_id=document_id,
                organization_id=identity.organization_id,
                ordinal=0,
                page=1,
                text="source",
                embedding=[0.0] * 384,
                embedding_revision="test-v1",
            )
        )


async def test_deleted_document_cleanup_removes_file_chunks_and_job_once(
    cleanup_database, tmp_path
):
    sessions, identity = cleanup_database
    storage = FileStorage(tmp_path, minimum_free_bytes=0)
    document_id, path = await _stored_document(sessions, identity, storage)
    await _add_chunk(sessions, identity, document_id)
    await DocumentRepository(sessions).delete(identity, document_id)

    recovery = DocumentRecovery(sessions, storage)
    first = await recovery.run_once()
    second = await recovery.run_once()

    assert first.deleted_documents == 1
    assert first.failures == 0
    assert second.deleted_documents == 0
    assert not path.exists()
    async with sessions() as session:
        document = await session.get(Document, document_id)
        job = await session.get(Job, document_id)
        chunk_count = await session.scalar(
            select(func.count()).select_from(Chunk).where(Chunk.document_id == document_id)
        )
    assert document is not None and document.deleted_at is not None
    assert job is None
    assert chunk_count == 0


async def test_cleanup_retries_deleted_file_after_storage_failure(
    cleanup_database, tmp_path, monkeypatch, caplog
):
    sessions, identity = cleanup_database
    storage = FileStorage(tmp_path, minimum_free_bytes=0)
    document_id, path = await _stored_document(sessions, identity, storage)
    await _add_chunk(sessions, identity, document_id)
    await DocumentRepository(sessions).delete(identity, document_id)
    recovery = DocumentRecovery(sessions, storage)
    original_remove = storage.remove
    failed = False

    async def fail_once(candidate):
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("simulated storage outage")
        await original_remove(candidate)

    monkeypatch.setattr(storage, "remove", fail_once)

    first = await recovery.run_once()
    assert first.failures == 1
    assert path.exists()
    async with sessions() as session:
        assert await session.get(Job, document_id) is not None
        assert (
            await session.scalar(
                select(func.count()).select_from(Chunk).where(Chunk.document_id == document_id)
            )
            == 1
        )
    assert [record.message for record in caplog.records] == ["document_cleanup_storage_unavailable"]
    assert "simulated" not in caplog.text

    second = await recovery.run_once()
    assert second.deleted_documents == 1
    assert second.failures == 0
    assert not path.exists()
    async with sessions() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(Chunk).where(Chunk.document_id == document_id)
            )
            == 0
        )


async def test_orphan_reconciliation_waits_for_admission_and_preserves_committed_file(
    cleanup_database, tmp_path
):
    sessions, identity = cleanup_database
    storage = FileStorage(tmp_path, minimum_free_bytes=0)
    committed_id, committed = await _stored_document(sessions, identity, storage)
    orphan_id = uuid4()
    orphan = storage.document_path(identity.organization_id, orphan_id)
    orphan.write_bytes(b"%PDF-1.4\norphan")
    old = orphan.stat().st_mtime - timedelta(hours=2).total_seconds()
    os.utime(orphan, (old, old))
    os.utime(committed, (old, old))
    blocker = sessions()
    await blocker.begin()
    await blocker.execute(text("SELECT pg_advisory_xact_lock(741001)"))
    task = asyncio.create_task(DocumentRecovery(sessions, storage).run_once())
    try:
        await asyncio.sleep(0.1)
        assert not task.done()
        assert orphan.exists()
    finally:
        await blocker.rollback()
        await blocker.close()

    result = await task
    assert result.orphaned_files == 1
    assert not orphan.exists()
    assert committed.exists()
    async with sessions() as session:
        assert await session.get(Document, committed_id) is not None
