from uuid import uuid4

import pytest


async def test_storage_saves_received_bytes_under_generated_ids(tmp_path):
    from app.documents.storage import FileStorage

    async def body():
        yield b"%PD"
        yield b"F-1.4\nexample"

    org, document = uuid4(), uuid4()
    storage = FileStorage(tmp_path, minimum_free_bytes=0)
    path = await storage.save(org, document, body())
    assert path == tmp_path / str(org) / f"{document}.pdf"
    assert path.read_bytes() == b"%PDF-1.4\nexample"
    assert not list(tmp_path.rglob("*.part"))


@pytest.mark.parametrize("payload,status", [(b"not pdf", 415), (b"%PDF-" + b"x" * 100, 413)])
async def test_storage_rejects_invalid_or_oversized_files_without_residue(
    tmp_path, payload, status
):
    from app.core.errors import AppError
    from app.documents.storage import FileStorage

    async def body():
        yield payload

    with pytest.raises(AppError) as error:
        await FileStorage(tmp_path, maximum_bytes=32, minimum_free_bytes=0).save(
            uuid4(), uuid4(), body()
        )
    assert error.value.status == status
    assert not [path for path in tmp_path.rglob("*") if path.is_file()]


async def test_storage_disk_full_is_safe_and_cleans_partial_file(tmp_path, monkeypatch):
    import errno
    from pathlib import Path

    from app.core.errors import AppError
    from app.documents.storage import FileStorage

    original = Path.open

    def fail_write(path, mode="r", *args, **kwargs):
        if mode == "ab":
            raise OSError(errno.ENOSPC, "private disk path")
        return original(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_write)

    async def body():
        yield b"%PDF-1.4\n"

    with pytest.raises(AppError) as error:
        await FileStorage(tmp_path, minimum_free_bytes=0).save(uuid4(), uuid4(), body())
    assert error.value.status == 503
    assert "private" not in str(error.value)
    assert not [path for path in tmp_path.rglob("*") if path.is_file()]


async def test_cancelled_upload_cleans_partial_file(tmp_path):
    import asyncio

    from app.documents.storage import FileStorage

    written = asyncio.Event()

    async def body():
        yield b"%PDF-1.4\n"
        written.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(
        FileStorage(tmp_path, minimum_free_bytes=0).save(uuid4(), uuid4(), body())
    )
    await written.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not [path for path in tmp_path.rglob("*") if path.is_file()]


async def test_storage_preserves_minimum_free_space(tmp_path, monkeypatch):
    import shutil
    from collections import namedtuple

    from app.core.errors import AppError
    from app.documents.storage import FileStorage

    usage = namedtuple("Usage", "total used free")
    monkeypatch.setattr(shutil, "disk_usage", lambda _: usage(100, 99, 1))

    async def body():
        yield b"%PDF-1.4\n"

    with pytest.raises(AppError) as error:
        await FileStorage(tmp_path, minimum_free_bytes=10).save(uuid4(), uuid4(), body())
    assert error.value.status == 503
    assert not [path for path in tmp_path.rglob("*") if path.is_file()]


async def test_repeated_cancellation_finishes_cleanup_after_atomic_rename(tmp_path, monkeypatch):
    import asyncio
    import threading
    from pathlib import Path

    from app.documents.storage import FileStorage

    renamed, finalize_release = threading.Event(), threading.Event()
    cleaning, cleanup_release = threading.Event(), threading.Event()
    original_finalize, original_unlink = FileStorage._finalize, Path.unlink

    def finalize(temporary, path):
        original_finalize(temporary, path)
        renamed.set()
        finalize_release.wait(timeout=5)

    def unlink(path, *args, **kwargs):
        if path.suffix == ".part":
            cleaning.set()
            cleanup_release.wait(timeout=5)
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(FileStorage, "_finalize", staticmethod(finalize))
    monkeypatch.setattr(Path, "unlink", unlink)

    async def body():
        yield b"%PDF-1.4\n"

    task = asyncio.create_task(
        FileStorage(tmp_path, minimum_free_bytes=0).save(uuid4(), uuid4(), body())
    )
    try:
        assert await asyncio.to_thread(renamed.wait, 2)
        task.cancel()
        finalize_release.set()
        assert await asyncio.to_thread(cleaning.wait, 2)
        task.cancel()
        cleanup_release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not [path for path in tmp_path.rglob("*") if path.is_file()]
    finally:
        finalize_release.set()
        cleanup_release.set()
