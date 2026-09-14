import logging
import os
import stat
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from uuid import UUID

from sqlalchemy import delete, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.documents.models import Chunk, Document, Job
from app.documents.storage import FileStorage, disk_operation

_ADMISSION_LOCK = 741001
_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CleanupResult:
    deleted_documents: int = 0
    orphaned_files: int = 0
    failures: int = 0


@dataclass(frozen=True, slots=True)
class _OrphanCandidate:
    organization_id: UUID
    document_id: UUID
    path: Path


class DocumentRecovery:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        storage: FileStorage,
        *,
        orphan_age: timedelta = timedelta(hours=1),
    ) -> None:
        if orphan_age <= timedelta(0):
            raise ValueError("Orphan age must be positive")
        self.sessions = sessions
        self.storage = storage
        self.orphan_age = orphan_age

    async def run_once(self) -> CleanupResult:
        deleted_documents = 0
        orphaned_files = 0
        failures = 0
        try:
            deleted_ids = await self._deleted_document_ids()
        except SQLAlchemyError:
            _LOGGER.warning("document_cleanup_database_unavailable")
            deleted_ids = []
            failures += 1
        for document_id in deleted_ids:
            try:
                if await self._cleanup_deleted(document_id):
                    deleted_documents += 1
            except OSError:
                _LOGGER.warning("document_cleanup_storage_unavailable")
                failures += 1
            except SQLAlchemyError:
                _LOGGER.warning("document_cleanup_database_unavailable")
                failures += 1

        cutoff = datetime.now(UTC) - self.orphan_age
        try:
            candidates = await disk_operation(lambda: self._orphan_candidates(cutoff))
        except OSError:
            _LOGGER.warning("document_reconciliation_storage_unavailable")
            return CleanupResult(deleted_documents, orphaned_files, failures + 1)
        for candidate in candidates:
            try:
                async with self.storage.try_track_upload(
                    candidate.organization_id, candidate.document_id
                ) as acquired:
                    if not acquired or not await disk_operation(
                        partial(self._is_old_regular_file, candidate.path, cutoff)
                    ):
                        continue
                    if await self._remove_unregistered(candidate):
                        orphaned_files += 1
            except OSError:
                _LOGGER.warning("document_reconciliation_storage_unavailable")
                failures += 1
            except SQLAlchemyError:
                _LOGGER.warning("document_reconciliation_database_unavailable")
                failures += 1
        return CleanupResult(deleted_documents, orphaned_files, failures)

    async def _deleted_document_ids(self) -> list[UUID]:
        async with self.sessions() as session:
            return list(
                (
                    await session.scalars(
                        select(Document.id)
                        .join(Job, Job.document_id == Document.id)
                        .where(Document.deleted_at.is_not(None))
                    )
                ).all()
            )

    async def _cleanup_deleted(self, document_id: UUID) -> bool:
        async with self.sessions.begin() as session:
            document = (
                await session.execute(
                    select(Document)
                    .where(Document.id == document_id, Document.deleted_at.is_not(None))
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if document is None:
                return False
            job = (
                await session.execute(
                    select(Job).where(Job.document_id == document_id).with_for_update()
                )
            ).scalar_one_or_none()
            if job is None:
                return False
            await self.storage.remove(
                self.storage.document_path(document.organization_id, document.id)
            )
            await session.execute(delete(Chunk).where(Chunk.document_id == document_id))
            await session.delete(job)
        return True

    async def _remove_unregistered(self, candidate: _OrphanCandidate) -> bool:
        async with self.sessions.begin() as session:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(:key)"), {"key": _ADMISSION_LOCK}
            )
            existing = await session.scalar(
                select(Document.id).where(
                    Document.id == candidate.document_id,
                    Document.organization_id == candidate.organization_id,
                )
            )
            if existing is not None:
                return False
            await self.storage.remove(candidate.path)
        return True

    def _orphan_candidates(self, cutoff: datetime) -> list[_OrphanCandidate]:
        try:
            organizations = os.scandir(self.storage.root)
        except FileNotFoundError:
            return []
        candidates: list[_OrphanCandidate] = []
        with organizations:
            for organization in organizations:
                if not organization.is_dir(follow_symlinks=False):
                    continue
                organization_id = self._canonical_uuid(organization.name)
                if organization_id is None:
                    continue
                with os.scandir(organization.path) as files:
                    for file in files:
                        if not file.is_file(follow_symlinks=False):
                            continue
                        path = Path(file.path)
                        if path.suffix not in {".pdf", ".part"}:
                            continue
                        document_id = self._canonical_uuid(path.stem)
                        if document_id is None:
                            continue
                        stat = file.stat(follow_symlinks=False)
                        if datetime.fromtimestamp(stat.st_mtime, UTC) >= cutoff:
                            continue
                        candidates.append(_OrphanCandidate(organization_id, document_id, path))
        return candidates

    @staticmethod
    def _canonical_uuid(value: str) -> UUID | None:
        try:
            parsed = UUID(value)
        except ValueError:
            return None
        return parsed if str(parsed) == value else None

    @staticmethod
    def _is_old_regular_file(path: Path, cutoff: datetime) -> bool:
        try:
            metadata = path.stat(follow_symlinks=False)
        except FileNotFoundError:
            return False
        return (
            stat.S_ISREG(metadata.st_mode)
            and datetime.fromtimestamp(metadata.st_mtime, UTC) < cutoff
        )
