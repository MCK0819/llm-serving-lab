import asyncio

import pytest

from app.core.errors import AppError


async def test_full_admission_rejects_and_releases_after_failure() -> None:
    from app.inference.admission import Admission

    gate = Admission(1, 0, 0.01)
    async with gate.slot():
        with pytest.raises(AppError) as error:
            async with gate.slot():
                pytest.fail("overload was admitted")
        assert error.value.status == 503
    async with gate.slot():
        pass


async def test_fifo_and_waiting_limit() -> None:
    from app.inference.admission import Admission

    gate = Admission(1, 2, 1)
    order = []

    async def enter(number: int) -> None:
        async with gate.slot():
            order.append(number)

    async with gate.slot():
        first = asyncio.create_task(enter(1))
        await asyncio.sleep(0)
        second = asyncio.create_task(enter(2))
        await asyncio.sleep(0)
        with pytest.raises(AppError):
            async with gate.slot():
                pass
    await asyncio.gather(first, second)
    assert order == [1, 2]


async def test_timeout_and_cancelled_waiter_do_not_leak_capacity() -> None:
    from app.inference.admission import Admission

    gate = Admission(1, 1, 0.01)

    async def enter() -> None:
        async with gate.slot():
            pass

    async with gate.slot():
        with pytest.raises(AppError) as error:
            await enter()
        assert error.value.code == "admission_timeout"
        waiter = asyncio.create_task(enter())
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
    await asyncio.wait_for(enter(), 0.1)


async def test_cancel_after_handoff_returns_reserved_slot() -> None:
    from app.inference.admission import Admission

    gate = Admission(1, 1, 1)
    acquired = asyncio.Event()

    async def enter() -> None:
        async with gate.slot():
            acquired.set()

    async with gate.slot():
        task = asyncio.create_task(enter())
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.wait_for(enter(), 0.1)
    assert acquired.is_set()
