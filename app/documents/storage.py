import asyncio
import errno
import importlib
import os
import shutil
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path
from typing import BinaryIO
from uuid import UUID

import anyio

from app.core.errors import AppError


async def disk_operation[T](operation: Callable[[], T]) -> T:
    # A cancelled request must join its disk operation before removing its files.
    task = asyncio.create_task(anyio.to_thread.run_sync(operation))
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    if cancelled:
        if not task.cancelled():
            task.exception()
        raise asyncio.CancelledError
    return task.result()


class FileStorage:
    def __init__(
        self,
        root: Path,
        *,
        maximum_bytes: int = 20 * 1024**2,
        minimum_free_bytes: int = 1024**3,
    ) -> None:
        self.root = root
        self.maximum_bytes = maximum_bytes
        self.minimum_free_bytes = minimum_free_bytes

    def _upload_marker(self, organization_id: UUID, document_id: UUID) -> Path:
        return self.root / str(organization_id) / f"{document_id}.upload"

    def document_path(self, organization_id: UUID, document_id: UUID) -> Path:
        return self.root / str(organization_id) / f"{document_id}.pdf"

    @asynccontextmanager
    async def track_upload(self, organization_id: UUID, document_id: UUID) -> AsyncIterator[None]:
        marker = self._upload_marker(organization_id, document_id)
        try:
            lock = await self._acquire_upload_lock(marker, True)
        except OSError:
            raise AppError(
                503, "storage_unavailable", "문서 저장 공간을 사용할 수 없습니다."
            ) from None
        assert lock is not None
        try:
            yield
        finally:
            await disk_operation(partial(self._release_lock, lock))

    @asynccontextmanager
    async def try_track_upload(
        self, organization_id: UUID, document_id: UUID
    ) -> AsyncIterator[bool]:
        marker = self._upload_marker(organization_id, document_id)
        lock = await self._acquire_upload_lock(marker, False)
        try:
            yield lock is not None
        finally:
            if lock is not None:
                await disk_operation(partial(self._release_lock, lock))

    async def save(
        self, organization_id: UUID, document_id: UUID, body: AsyncIterator[bytes]
    ) -> Path:
        path = self.document_path(organization_id, document_id)
        temporary = path.with_suffix(".part")
        complete = False
        try:
            await disk_operation(lambda: path.parent.mkdir(parents=True, exist_ok=True))
            await disk_operation(lambda: self._create(temporary))
            size = 0
            prefix = b""
            async for chunk in body:
                size += len(chunk)
                if size > self.maximum_bytes:
                    raise AppError(413, "file_too_large", "PDF 파일 크기 제한을 초과했습니다.")
                prefix = (prefix + chunk[:5])[:5]
                if len(prefix) == 5 and prefix != b"%PDF-":
                    raise AppError(415, "unsupported_file", "PDF 파일을 업로드해 주세요.")
                await disk_operation(partial(self._append, temporary, chunk))
            if prefix != b"%PDF-":
                raise AppError(415, "unsupported_file", "PDF 파일을 업로드해 주세요.")
            await disk_operation(lambda: self._finalize(temporary, path))
            complete = True
            return path
        except OSError:
            raise AppError(
                503, "storage_unavailable", "문서 저장 공간을 사용할 수 없습니다."
            ) from None
        finally:
            if not complete:
                await disk_operation(partial(self._discard, temporary, path))

    @staticmethod
    def _discard(temporary: Path, path: Path) -> None:
        # Join both removals as one operation, even if cancellation repeats during cleanup.
        try:
            temporary.unlink(missing_ok=True)
        finally:
            path.unlink(missing_ok=True)

    def _create(self, path: Path) -> None:
        self._check_space(path.parent, 0)
        with path.open("xb"):
            pass

    def _check_space(self, directory: Path, incoming: int) -> None:
        if shutil.disk_usage(directory).free < self.minimum_free_bytes + incoming:
            raise AppError(503, "storage_unavailable", "문서 저장 공간이 부족합니다.")

    def _append(self, path: Path, chunk: bytes) -> None:
        self._check_space(path.parent, len(chunk))
        with path.open("ab") as file:
            file.write(chunk)

    @staticmethod
    def _finalize(temporary: Path, path: Path) -> None:
        with temporary.open("ab") as file:
            file.flush()
            os.fsync(file.fileno())
        temporary.replace(path)

    async def remove(self, path: Path) -> None:
        await disk_operation(lambda: path.unlink(missing_ok=True))

    async def _acquire_upload_lock(self, path: Path, blocking: bool) -> BinaryIO | None:
        # An acquisition can finish after cancellation. Keep its result so that the
        # descriptor is explicitly released before propagating cancellation.
        task = asyncio.create_task(
            anyio.to_thread.run_sync(partial(self._acquire_lock, path, blocking))
        )
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
        lock = task.result()
        if cancelled:
            if lock is not None:
                await disk_operation(partial(self._release_lock, lock))
            raise asyncio.CancelledError
        return lock

    @staticmethod
    def _acquire_lock(path: Path, blocking: bool) -> BinaryIO | None:
        path.parent.mkdir(parents=True, exist_ok=True)
        file = path.open("a+b")
        try:
            file.seek(0, os.SEEK_END)
            if file.tell() == 0:
                file.write(b"\0")
                file.flush()
                os.fsync(file.fileno())
            file.seek(0)
            if os.name == "nt":
                msvcrt = importlib.import_module("msvcrt")

                mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
                try:
                    msvcrt.locking(file.fileno(), mode, 1)
                except OSError as error:
                    if not blocking and error.errno in {
                        errno.EACCES,
                        errno.EAGAIN,
                        errno.EDEADLK,
                    }:
                        file.close()
                        return None
                    raise
            else:
                fcntl = importlib.import_module("fcntl")
                flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
                try:
                    fcntl.flock(file.fileno(), flags)
                except BlockingIOError:
                    file.close()
                    return None
            os.utime(path, None)
            return file
        except BaseException:
            file.close()
            raise

    @staticmethod
    def _release_lock(file: BinaryIO) -> None:
        try:
            file.seek(0)
            if os.name == "nt":
                msvcrt = importlib.import_module("msvcrt")

                msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl = importlib.import_module("fcntl")
                fcntl.flock(file.fileno(), fcntl.LOCK_UN)
        finally:
            file.close()
