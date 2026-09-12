import base64
import binascii
import json
import re
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

from sqlalchemy import and_, func, or_, select, text, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import AppError
from app.documents.models import Document, Job
from app.documents.schemas import (
    DocumentDetail,
    DocumentFailure,
    DocumentItem,
    DocumentPage,
    DocumentReceipt,
    DocumentStatus,
)
from app.users.types import Identity

_ADMISSION_LOCK = 741001
_MAX_CURSOR_LENGTH = 512
_CURSOR_PATTERN = re.compile(r"[A-Za-z0-9_-]+")


def _invalid_cursor() -> AppError:
    return AppError(422, "invalid_cursor", "커서를 확인해 주세요.")


class DocumentCommitOutcomeUnknown(RuntimeError):
    status = 503
    code = "documents_unavailable"
    message = "문서 저장 결과를 확인할 수 없습니다."

    def __init__(self) -> None:
        super().__init__(self.message)


def _timestamp_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _encode_cursor(created_at: datetime, document_id: UUID) -> str:
    payload = {
        "created_at": _timestamp_text(created_at),
        "document_id": str(document_id),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("ascii")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    if not cursor or len(cursor) > _MAX_CURSOR_LENGTH or _CURSOR_PATTERN.fullmatch(cursor) is None:
        raise _invalid_cursor()
    try:
        padding = "=" * (-len(cursor) % 4)
        raw = base64.b64decode(cursor + padding, altchars=b"-_", validate=True)
        if base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii") != cursor:
            raise ValueError
        payload = json.loads(raw)
        if not isinstance(payload, dict) or set(payload) != {"created_at", "document_id"}:
            raise ValueError
        if not isinstance(payload["created_at"], str) or not isinstance(
            payload["document_id"], str
        ):
            raise ValueError
        created_text = payload["created_at"]
        if not created_text.endswith("Z"):
            raise ValueError
        created_at = datetime.fromisoformat(created_text[:-1] + "+00:00")
        if _timestamp_text(created_at) != created_text:
            raise ValueError
        document_id = UUID(payload["document_id"])
        if str(document_id) != payload["document_id"]:
            raise ValueError
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("ascii")
        if raw != canonical:
            raise ValueError
    except ValueError, TypeError, UnicodeError, json.JSONDecodeError, binascii.Error:
        raise _invalid_cursor() from None
    return created_at, document_id


class DocumentRepository:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        organization_job_limit: int = 10,
        global_job_limit: int = 100,
    ) -> None:
        if organization_job_limit < 1 or global_job_limit < 1:
            raise ValueError("Document job limits must be positive")
        self.sessions = sessions
        self.organization_job_limit = organization_job_limit
        self.global_job_limit = global_job_limit

    async def create(
        self,
        identity: Identity,
        document_id: UUID,
        filename: str,
        storage_path: str,
    ) -> DocumentReceipt:
        try:
            async with self.sessions.begin() as session:
                await session.execute(
                    text("SELECT pg_advisory_xact_lock(:key)"), {"key": _ADMISSION_LOCK}
                )
                unfinished = Document.status.in_(("queued", "processing"))
                global_count = await session.scalar(
                    select(func.count()).select_from(Document).where(unfinished)
                )
                organization_count = await session.scalar(
                    select(func.count())
                    .select_from(Document)
                    .where(unfinished, Document.organization_id == identity.organization_id)
                )
                if (
                    global_count is None
                    or organization_count is None
                    or global_count >= self.global_job_limit
                    or organization_count >= self.organization_job_limit
                ):
                    raise AppError(
                        503,
                        "document_capacity_exceeded",
                        "문서 처리 대기열이 가득 찼습니다. 잠시 후 다시 시도해 주세요.",
                    )
                document = Document(
                    id=document_id,
                    organization_id=identity.organization_id,
                    uploader_id=identity.user_id,
                    filename=filename,
                    storage_path=storage_path,
                    status="queued",
                )
                session.add(document)
                session.add(Job(document_id=document_id, organization_id=identity.organization_id))
                await session.flush()
                receipt = DocumentReceipt(
                    document_id=document.id,
                    status="queued",
                    created_at=document.created_at,
                )
            return receipt
        except SQLAlchemyError:
            try:
                committed = await self._find_committed(
                    identity, document_id, filename, storage_path
                )
            except SQLAlchemyError:
                raise DocumentCommitOutcomeUnknown() from None
            if committed is not None:
                return committed
            raise

    async def _find_committed(
        self,
        identity: Identity,
        document_id: UUID,
        filename: str,
        storage_path: str,
    ) -> DocumentReceipt | None:
        async with self.sessions.begin() as session:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(:key)"), {"key": _ADMISSION_LOCK}
            )
            document = (
                await session.execute(
                    select(Document)
                    .join(
                        Job,
                        and_(
                            Job.document_id == Document.id,
                            Job.organization_id == Document.organization_id,
                        ),
                    )
                    .where(
                        Document.id == document_id,
                        Document.organization_id == identity.organization_id,
                        Document.uploader_id == identity.user_id,
                        Document.filename == filename,
                        Document.storage_path == storage_path,
                    )
                )
            ).scalar_one_or_none()
        if document is None:
            return None
        return DocumentReceipt(
            document_id=document.id,
            status="queued",
            created_at=document.created_at,
        )

    async def list(self, identity: Identity, limit: int, cursor: str | None) -> DocumentPage:
        if limit < 1 or limit > 100:
            raise AppError(422, "invalid_request", "목록 크기를 확인해 주세요.")
        position = _decode_cursor(cursor) if cursor is not None else None
        statement = select(Document).where(
            Document.organization_id == identity.organization_id,
            Document.deleted_at.is_(None),
        )
        if position is not None:
            statement = statement.where(
                or_(
                    Document.created_at < position[0],
                    and_(Document.created_at == position[0], Document.id < position[1]),
                )
            )
        statement = statement.order_by(Document.created_at.desc(), Document.id.desc()).limit(
            limit + 1
        )
        async with self.sessions() as session:
            documents = (await session.execute(statement)).scalars().all()
        has_more = len(documents) > limit
        visible = documents[:limit]
        next_cursor = (
            _encode_cursor(visible[-1].created_at, visible[-1].id) if has_more and visible else None
        )
        return DocumentPage(
            items=[self._item(document) for document in visible], next_cursor=next_cursor
        )

    async def get(self, identity: Identity, document_id: UUID) -> DocumentDetail:
        async with self.sessions() as session:
            document = (
                await session.execute(
                    select(Document).where(
                        Document.id == document_id,
                        Document.organization_id == identity.organization_id,
                        Document.deleted_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
        if document is None:
            raise AppError(404, "not_found", "문서를 찾을 수 없습니다.")
        failure = (
            DocumentFailure(code=document.failure_code, message=document.failure_message)
            if document.status == "failed"
            and document.failure_code is not None
            and document.failure_message is not None
            else None
        )
        return DocumentDetail(**self._item(document).model_dump(), failure=failure)

    async def delete(self, identity: Identity, document_id: UUID) -> None:
        async with self.sessions.begin() as session:
            document = (
                await session.execute(
                    select(Document)
                    .where(
                        Document.id == document_id,
                        Document.organization_id == identity.organization_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if document is None:
                raise AppError(404, "not_found", "문서를 찾을 수 없습니다.")
            if document.uploader_id != identity.user_id:
                raise AppError(403, "forbidden", "문서를 삭제할 권한이 없습니다.")
            if document.deleted_at is not None:
                return
            await session.execute(
                select(Job).where(Job.document_id == document_id).with_for_update()
            )
            await session.execute(
                update(Document)
                .where(Document.id == document_id)
                .values(status="deleted", deleted_at=func.clock_timestamp(), updated_at=func.now())
            )

    @staticmethod
    def _item(document: Document) -> DocumentItem:
        return DocumentItem(
            document_id=document.id,
            filename=document.filename,
            status=cast(DocumentStatus, document.status),
            created_at=document.created_at,
            updated_at=document.updated_at,
        )
