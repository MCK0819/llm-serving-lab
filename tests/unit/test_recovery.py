import os
from datetime import timedelta
from uuid import uuid4


async def _no_deleted_documents():
    return []


async def test_reconciliation_removes_only_old_unregistered_payloads(tmp_path, monkeypatch):
    from app.documents.recovery import DocumentRecovery
    from app.documents.storage import FileStorage

    storage = FileStorage(tmp_path, minimum_free_bytes=0)
    organization_id = uuid4()
    old_orphan_id, fresh_orphan_id, committed_id = uuid4(), uuid4(), uuid4()
    directory = tmp_path / str(organization_id)
    directory.mkdir()
    old_orphan = directory / f"{old_orphan_id}.pdf"
    fresh_orphan = directory / f"{fresh_orphan_id}.part"
    committed = directory / f"{committed_id}.pdf"
    for path in (old_orphan, fresh_orphan, committed):
        path.write_bytes(b"payload")
    old = old_orphan.stat().st_mtime - timedelta(hours=2).total_seconds()
    os.utime(old_orphan, (old, old))
    os.utime(committed, (old, old))

    recovery = DocumentRecovery(None, storage)

    async def remove_unregistered(candidate):
        assert candidate.organization_id == organization_id
        if candidate.document_id == committed_id:
            return False
        candidate.path.unlink()
        return True

    monkeypatch.setattr(recovery, "_deleted_document_ids", _no_deleted_documents)
    monkeypatch.setattr(recovery, "_remove_unregistered", remove_unregistered)

    await recovery.run_once()

    assert not old_orphan.exists()
    assert fresh_orphan.exists()
    assert committed.exists()


async def test_reconciliation_skips_old_payload_while_upload_lock_is_held(tmp_path, monkeypatch):
    from app.documents.recovery import DocumentRecovery
    from app.documents.storage import FileStorage

    storage = FileStorage(tmp_path, minimum_free_bytes=0)
    organization_id, document_id = uuid4(), uuid4()
    path = tmp_path / str(organization_id) / f"{document_id}.part"
    path.parent.mkdir()
    path.write_bytes(b"partial")
    old = path.stat().st_mtime - timedelta(hours=2).total_seconds()
    os.utime(path, (old, old))
    recovery = DocumentRecovery(None, storage)

    async def unexpected_removal(*_):
        raise AssertionError("active upload must not reach removal")

    monkeypatch.setattr(recovery, "_deleted_document_ids", _no_deleted_documents)
    monkeypatch.setattr(recovery, "_remove_unregistered", unexpected_removal)

    async with storage.track_upload(organization_id, document_id):
        await recovery.run_once()

    assert path.exists()


async def test_reconciliation_ignores_paths_outside_generated_uuid_layout(tmp_path, monkeypatch):
    from app.documents.recovery import DocumentRecovery
    from app.documents.storage import FileStorage

    storage = FileStorage(tmp_path, minimum_free_bytes=0)
    unsafe_paths = [
        tmp_path / "not-an-organization" / f"{uuid4()}.pdf",
        tmp_path / str(uuid4()) / "not-a-document.pdf",
        tmp_path / str(uuid4()) / f"{uuid4()}.txt",
    ]
    for path in unsafe_paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"leave me")
        old = path.stat().st_mtime - timedelta(hours=2).total_seconds()
        os.utime(path, (old, old))
    recovery = DocumentRecovery(None, storage)

    async def unexpected_database_lookup(*_):
        raise AssertionError("invalid storage paths must not reach the database")

    monkeypatch.setattr(recovery, "_deleted_document_ids", _no_deleted_documents)
    monkeypatch.setattr(recovery, "_remove_unregistered", unexpected_database_lookup)

    await recovery.run_once()

    assert all(path.exists() for path in unsafe_paths)
