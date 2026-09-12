import asyncio
import logging
from collections.abc import AsyncIterator
from uuid import UUID, uuid4

from sqlalchemy.exc import SQLAlchemyError

from app.core.errors import AppError
from app.documents.notifications import JobNotifier
from app.documents.repository import DocumentCommitOutcomeUnknown, DocumentRepository
from app.documents.schemas import DocumentDetail, DocumentPage, DocumentReceipt
from app.documents.storage import FileStorage
from app.users.types import Identity


def database_unavailable() -> AppError:
    return AppError(503, "documents_unavailable", "문서 저장소를 사용할 수 없습니다.")


class DocumentService:
    def __init__(
        self,
        repository: DocumentRepository,
        storage: FileStorage,
        notifier: JobNotifier | None = None,
    ) -> None:
        self.repository = repository
        self.storage = storage
        self.notifier = notifier

    async def accept(
        self, identity: Identity, filename: str, body: AsyncIterator[bytes]
    ) -> DocumentReceipt:
        filename = filename.strip()
        if (
            not filename
            or len(filename) > 255
            or any(ord(char) < 32 or ord(char) == 127 for char in filename)
        ):
            raise AppError(422, "invalid_filename", "문서 이름을 확인해 주세요.")
        document_id = uuid4()
        path = await self.storage.save(identity.organization_id, document_id, body)
        # Once a transaction starts, settle it before deciding whether its file is disposable.
        task = asyncio.create_task(
            self.repository.create(identity, document_id, filename, str(path))
        )
        cancelled = False
        try:
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    cancelled = True
            receipt = task.result()
        except DocumentCommitOutcomeUnknown:
            # Reconciliation could not establish whether COMMIT succeeded. Keep its PDF.
            raise database_unavailable() from None
        except (SQLAlchemyError, AppError) as exc:
            await self.storage.remove(path)
            if isinstance(exc, SQLAlchemyError):
                raise database_unavailable() from None
            raise
        if cancelled:
            raise asyncio.CancelledError
        if self.notifier is not None:
            try:
                await self.notifier.notify(document_id)
            except Exception:
                # The job is already committed; never report a failed upload for a lost wake-up.
                logging.getLogger(__name__).warning("document_notification_unavailable")
        return receipt

    async def list(self, identity: Identity, limit: int, cursor: str | None) -> DocumentPage:
        try:
            return await self.repository.list(identity, limit, cursor)
        except SQLAlchemyError:
            raise database_unavailable() from None

    async def get(self, identity: Identity, document_id: UUID) -> DocumentDetail:
        try:
            return await self.repository.get(identity, document_id)
        except SQLAlchemyError:
            raise database_unavailable() from None

    async def delete(self, identity: Identity, document_id: UUID) -> None:
        try:
            await self.repository.delete(identity, document_id)
        except SQLAlchemyError:
            raise database_unavailable() from None
