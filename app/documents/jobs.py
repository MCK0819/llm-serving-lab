from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.documents.chunking import Chunk as ParsedChunk
from app.documents.models import Chunk, Document, Job

LEASE_SECONDS = 120
ABSOLUTE_ATTEMPT_SECONDS = 300
MAX_ATTEMPTS = 3

_RETRY_DELAYS = {1: 10, 2: 30}
_PUBLIC_FAILURES = {
    "document_too_large": "문서 처리 한도를 초과했습니다.",
    "encrypted_pdf": "암호화된 PDF는 처리할 수 없습니다.",
    "invalid_document_text": "문서 텍스트를 처리할 수 없습니다.",
    "invalid_pdf": "PDF를 처리할 수 없습니다.",
    "pdf_no_text": "PDF에서 텍스트를 찾을 수 없습니다.",
    "pdf_page_limit": "PDF 페이지 수 제한을 초과했습니다.",
    "pdf_text_limit": "PDF 텍스트 크기 제한을 초과했습니다.",
    "processing_timeout": "문서 처리 시간이 초과되었습니다.",
    "worker_unavailable": "문서 처리 서버를 사용할 수 없습니다.",
}
_GENERIC_FAILURE = ("processing_failed", "문서 처리에 실패했습니다.")


@dataclass(frozen=True, slots=True)
class Lease:
    document_id: UUID
    organization_id: UUID
    attempt_id: UUID
    attempt_count: int
    storage_path: str


class JobRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    async def claim(self, document_id: UUID) -> Lease | None:
        async with self.sessions.begin() as session:
            document = await self._lock_document(session, document_id)
            if document is None or document.deleted_at is not None:
                return None
            if document.status not in ("queued", "processing"):
                return None
            job = await self._lock_job(session, document_id)
            if job is None:
                return None
            now = await self._now(session)
            if not self._claim_is_due(document, job, now):
                return None
            if job.attempt_count >= MAX_ATTEMPTS:
                self._mark_failed(document, job, now, *_GENERIC_FAILURE)
                return None

            attempt_id = uuid4()
            attempt_count = job.attempt_count + 1
            if document.status == "queued":
                due = Job.next_attempt_at <= now
            else:
                delay = _RETRY_DELAYS.get(job.attempt_count, 0)
                due = Job.lease_until <= now - timedelta(seconds=delay)
            claimed_count = await session.scalar(
                update(Job)
                .where(
                    Job.document_id == document_id,
                    Job.attempt_count == job.attempt_count,
                    due,
                )
                .values(
                    attempt_id=attempt_id,
                    attempt_count=attempt_count,
                    attempt_started_at=now,
                    lease_until=now + timedelta(seconds=LEASE_SECONDS),
                    updated_at=now,
                )
                .returning(Job.attempt_count)
            )
            if claimed_count != attempt_count:
                return None
            document.status = "processing"
            document.failure_code = None
            document.failure_message = None
            document.updated_at = now
            return Lease(
                document_id=document.id,
                organization_id=document.organization_id,
                attempt_id=attempt_id,
                attempt_count=attempt_count,
                storage_path=document.storage_path,
            )

    async def renew(self, lease: Lease) -> bool:
        async with self.sessions.begin() as session:
            document = await self._lock_document(session, lease.document_id)
            if not self._document_is_processing(document, lease):
                return False
            job = await self._lock_job(session, lease.document_id)
            now = await self._now(session)
            if not self._lease_is_active(job, lease, now):
                return False
            assert job is not None and job.attempt_started_at is not None
            absolute_end = job.attempt_started_at + timedelta(seconds=ABSOLUTE_ATTEMPT_SECONDS)
            if absolute_end <= now:
                return False
            job.lease_until = min(now + timedelta(seconds=LEASE_SECONDS), absolute_end)
            job.updated_at = now
            return True

    async def publish(
        self,
        lease: Lease,
        chunks: list[ParsedChunk],
        vectors: list[list[float]],
        revision: str,
    ) -> bool:
        if len(chunks) != len(vectors):
            raise ValueError("chunks and vectors must have the same length")
        async with self.sessions.begin() as session:
            document = await self._lock_document(session, lease.document_id)
            if not self._document_is_processing(document, lease):
                return False
            job = await self._lock_job(session, lease.document_id)
            now = await self._now(session)
            if not self._lease_is_active(job, lease, now):
                return False
            assert document is not None and job is not None
            session.add_all(
                [
                    Chunk(
                        document_id=lease.document_id,
                        organization_id=lease.organization_id,
                        ordinal=chunk.ordinal,
                        page=chunk.page,
                        text=chunk.text,
                        embedding=vector,
                        embedding_revision=revision,
                    )
                    for chunk, vector in zip(chunks, vectors, strict=True)
                ]
            )
            document.status = "ready"
            document.failure_code = None
            document.failure_message = None
            document.updated_at = now
            self._clear_attempt(job, now)
            return True

    async def fail(self, lease: Lease, code: str, retryable: bool) -> None:
        async with self.sessions.begin() as session:
            document = await self._lock_document(session, lease.document_id)
            if not self._document_is_processing(document, lease):
                return
            job = await self._lock_job(session, lease.document_id)
            now = await self._now(session)
            if not self._lease_is_active(job, lease, now):
                return
            assert document is not None and job is not None
            if retryable and job.attempt_count < MAX_ATTEMPTS:
                delay = _RETRY_DELAYS[job.attempt_count]
                document.status = "queued"
                document.failure_code = None
                document.failure_message = None
                document.updated_at = now
                self._clear_attempt(job, now)
                job.next_attempt_at = now + timedelta(seconds=delay)
                return
            if retryable:
                failure_code, failure_message = _GENERIC_FAILURE
            else:
                failure_code, failure_message = self._public_failure(code)
            self._mark_failed(document, job, now, failure_code, failure_message)

    async def recover_due(self) -> list[UUID]:
        due: list[UUID] = []
        async with self.sessions.begin() as session:
            documents = (
                (
                    await session.execute(
                        select(Document)
                        .where(Document.status.in_(("queued", "processing")))
                        .order_by(Document.id)
                        .with_for_update(skip_locked=True)
                    )
                )
                .scalars()
                .all()
            )
            now = await self._now(session)
            for document in documents:
                if document.deleted_at is not None:
                    continue
                job = await self._lock_job(session, document.id)
                if job is None or not self._claim_is_due(document, job, now):
                    continue
                if job.attempt_count >= MAX_ATTEMPTS:
                    self._mark_failed(document, job, now, *_GENERIC_FAILURE)
                else:
                    due.append(document.id)
        return due

    @staticmethod
    async def _lock_document(session: AsyncSession, document_id: UUID) -> Document | None:
        return (
            await session.execute(
                select(Document).where(Document.id == document_id).with_for_update()
            )
        ).scalar_one_or_none()

    @staticmethod
    async def _lock_job(session: AsyncSession, document_id: UUID) -> Job | None:
        return (
            await session.execute(
                select(Job).where(Job.document_id == document_id).with_for_update()
            )
        ).scalar_one_or_none()

    @staticmethod
    async def _now(session: AsyncSession) -> datetime:
        now = await session.scalar(select(func.clock_timestamp()))
        assert isinstance(now, datetime)
        return now

    @staticmethod
    def _document_is_processing(document: Document | None, lease: Lease) -> bool:
        return bool(
            document is not None
            and document.organization_id == lease.organization_id
            and document.status == "processing"
            and document.deleted_at is None
        )

    @staticmethod
    def _lease_is_active(job: Job | None, lease: Lease, now: datetime) -> bool:
        return bool(
            job is not None
            and job.organization_id == lease.organization_id
            and job.attempt_id == lease.attempt_id
            and job.attempt_count == lease.attempt_count
            and job.attempt_started_at is not None
            and job.lease_until is not None
            and job.attempt_started_at + timedelta(seconds=ABSOLUTE_ATTEMPT_SECONDS) > now
            and job.lease_until > now
        )

    @staticmethod
    def _claim_is_due(document: Document, job: Job, now: datetime) -> bool:
        if document.status == "queued":
            return job.next_attempt_at <= now
        if document.status != "processing" or job.lease_until is None:
            return False
        delay = _RETRY_DELAYS.get(job.attempt_count, 0)
        return job.lease_until + timedelta(seconds=delay) <= now

    @staticmethod
    def _clear_attempt(job: Job, now: datetime) -> None:
        job.attempt_id = None
        job.attempt_started_at = None
        job.lease_until = None
        job.updated_at = now

    @classmethod
    def _mark_failed(
        cls,
        document: Document,
        job: Job,
        now: datetime,
        code: str,
        message: str,
    ) -> None:
        document.status = "failed"
        document.failure_code = code
        document.failure_message = message
        document.updated_at = now
        cls._clear_attempt(job, now)

    @staticmethod
    def _public_failure(code: str) -> tuple[str, str]:
        message = _PUBLIC_FAILURES.get(code)
        return (code, message) if message is not None else _GENERIC_FAILURE
