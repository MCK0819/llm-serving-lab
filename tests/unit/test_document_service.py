from uuid import uuid4

import pytest
from sqlalchemy.exc import SQLAlchemyError

from app.core.errors import AppError
from app.users.types import Identity


async def test_db_failure_removes_saved_pdf_and_returns_safe_error(tmp_path):
    from app.documents.service import DocumentService
    from app.documents.storage import FileStorage

    class UnavailableRepository:
        async def create(self, *args):
            raise SQLAlchemyError("private connection string")

    async def body():
        yield b"%PDF-1.4\n"

    service = DocumentService(UnavailableRepository(), FileStorage(tmp_path, minimum_free_bytes=0))
    with pytest.raises(AppError) as error:
        await service.accept(Identity(uuid4(), uuid4()), "policy.pdf", body())
    assert error.value.status == 503
    assert "private" not in str(error.value)
    assert not [path for path in tmp_path.rglob("*") if path.is_file()]


@pytest.mark.parametrize("filename", ["", "x" * 256, "bad\x00name.pdf", "bad\nname.pdf"])
async def test_invalid_filename_rejected_before_storage(tmp_path, filename):
    from app.documents.service import DocumentService
    from app.documents.storage import FileStorage

    async def body():
        raise AssertionError("Must reject metadata before consuming file")
        yield b""

    service = DocumentService(None, FileStorage(tmp_path, minimum_free_bytes=0))
    with pytest.raises(AppError) as error:
        await service.accept(Identity(uuid4(), uuid4()), filename, body())
    assert error.value.status == 422
    assert not list(tmp_path.iterdir())


async def test_broker_failure_after_commit_keeps_receipt_and_file(tmp_path):
    from datetime import UTC, datetime

    from app.documents.schemas import DocumentReceipt
    from app.documents.service import DocumentService
    from app.documents.storage import FileStorage

    committed = []
    committed_at_notification = []

    class Repository:
        async def create(self, identity, document_id, filename, path):
            committed.append(document_id)
            return DocumentReceipt(
                document_id=document_id, status="queued", created_at=datetime.now(UTC)
            )

    class BrokenNotification:
        async def notify(self, document_id):
            committed_at_notification.extend(committed)
            raise ConnectionError("private broker url")

    async def body():
        yield b"%PDF-1.4\n"

    service = DocumentService(
        Repository(), FileStorage(tmp_path, minimum_free_bytes=0), BrokenNotification()
    )
    result = await service.accept(Identity(uuid4(), uuid4()), "policy.pdf", body())
    assert result.status == "queued"
    assert committed == [result.document_id]
    assert committed_at_notification == [result.document_id]
    assert len(list(tmp_path.rglob("*.pdf"))) == 1


async def test_real_celery_unavailable_broker_finishes_within_short_deadline():
    import asyncio
    import socket

    from app.documents.notifications import JobNotifier

    # Reserve a non-listening local socket, rather than assuming a port is unused.
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
        notifier = JobNotifier(f"redis://127.0.0.1:{port}/0")
        async with asyncio.timeout(3):
            await notifier.notify(uuid4())


async def test_cancel_during_db_commit_retains_committed_file(tmp_path):
    import asyncio
    from datetime import UTC, datetime

    from app.documents.schemas import DocumentReceipt
    from app.documents.service import DocumentService
    from app.documents.storage import FileStorage

    started, finish = asyncio.Event(), asyncio.Event()
    committed = []

    class Repository:
        async def create(self, identity, document_id, filename, path):
            started.set()
            await finish.wait()
            committed.append(document_id)
            return DocumentReceipt(
                document_id=document_id, status="queued", created_at=datetime.now(UTC)
            )

    async def body():
        yield b"%PDF-1.4\n"

    service = DocumentService(Repository(), FileStorage(tmp_path, minimum_free_bytes=0))
    task = asyncio.create_task(service.accept(Identity(uuid4(), uuid4()), "policy.pdf", body()))
    await started.wait()
    task.cancel()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(committed) == 1
    assert len(list(tmp_path.rglob("*.pdf"))) == 1


async def test_unknown_commit_outcome_preserves_file_and_returns_safe_503(tmp_path):
    from app.documents.repository import DocumentCommitOutcomeUnknown
    from app.documents.service import DocumentService
    from app.documents.storage import FileStorage

    class UnknownRepository:
        async def create(self, *args):
            raise DocumentCommitOutcomeUnknown()

    async def body():
        yield b"%PDF-1.4\n"

    service = DocumentService(UnknownRepository(), FileStorage(tmp_path, minimum_free_bytes=0))
    with pytest.raises(AppError) as error:
        await service.accept(Identity(uuid4(), uuid4()), "policy.pdf", body())
    assert error.value.status == 503
    assert len(list(tmp_path.rglob("*.pdf"))) == 1
