import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest


def database():
    engine = MagicMock()
    connection = AsyncMock()
    engine.connect.return_value.__aenter__ = AsyncMock(return_value=connection)
    engine.connect.return_value.__aexit__ = AsyncMock(return_value=False)
    return engine, connection


async def test_readiness_needs_only_database_writable_storage_and_no_drain(tmp_path):
    from app.core.health import HealthService

    engine, connection = database()
    draining = [False]
    health = HealthService(engine, tmp_path, lambda: draining[0])
    assert await health.ready()
    connection.execute.assert_awaited_once()
    assert list(tmp_path.iterdir()) == []
    draining[0] = True
    assert not await health.ready()
    assert connection.execute.await_count == 1


async def test_readiness_fails_closed_on_missing_db_or_dependency_errors(tmp_path):
    from app.core.health import HealthService

    assert not await HealthService(None, tmp_path, lambda: False).ready()
    engine, connection = database()
    connection.execute.side_effect = RuntimeError("PRIVATE_DATABASE_DETAILS")
    assert not await HealthService(engine, tmp_path, lambda: False).ready()
    engine, _ = database()
    blocked = tmp_path / "file"
    blocked.write_text("not a directory")
    assert not await HealthService(engine, blocked, lambda: False).ready()


async def test_cancelled_probe_does_not_spawn_duplicate_dependency_work(tmp_path):
    from app.core.health import HealthService

    entered, release = asyncio.Event(), asyncio.Event()
    engine, connection = database()

    async def blocked(query):
        entered.set()
        await release.wait()

    connection.execute.side_effect = blocked
    health = HealthService(engine, tmp_path, lambda: False)
    first = asyncio.create_task(health.ready())
    await entered.wait()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    second = asyncio.create_task(health.ready())
    await asyncio.sleep(0)
    assert connection.execute.await_count == 1
    release.set()
    assert await second
