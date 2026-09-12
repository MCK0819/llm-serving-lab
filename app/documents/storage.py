import asyncio
import os
import shutil
from collections.abc import AsyncIterator, Callable
from functools import partial
from pathlib import Path
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

    async def save(
        self, organization_id: UUID, document_id: UUID, body: AsyncIterator[bytes]
    ) -> Path:
        path = self.root / str(organization_id) / f"{document_id}.pdf"
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
