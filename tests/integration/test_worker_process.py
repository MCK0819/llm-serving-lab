"""Real Redis, PostgreSQL, Celery prefork, and crash-recovery integration."""

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy import text

from app.core.settings import Settings
from app.main import create_app
from app.users.service import AuthService
from tests.integration.test_auth import accounts  # noqa: F401

pytestmark = pytest.mark.integration


def _worker_environment(upload_root: Path) -> dict[str, str]:
    return {
        **os.environ,
        "LLM_LAB_DATABASE_URL": os.environ["LLM_LAB_TEST_DATABASE_URL"],
        "LLM_LAB_BROKER_URL": os.environ["LLM_LAB_TEST_BROKER_URL"],
        "LLM_LAB_EMBEDDING_URL": os.environ["LLM_LAB_TEST_TEI_URL"],
        "LLM_LAB_WORKER_TOKENIZER_PATH": os.environ["LLM_LAB_TEST_E5_TOKENIZER"],
        "LLM_LAB_UPLOAD_ROOT": str(upload_root),
        "LLM_LAB_DOCUMENT_MINIMUM_FREE_BYTES": "0",
    }


def _start_owned_process(
    mode: str, environment: dict[str, str], log_path: Path
) -> subprocess.Popen:
    log = log_path.open("wb")
    process = subprocess.Popen(
        [sys.executable, "-m", "app.documents.worker", mode],
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        env=environment,
        start_new_session=True,
    )
    log.close()
    return process


def _stop_owned_process(process: subprocess.Popen, *, force: bool = False) -> None:
    if process.poll() is not None:
        return
    os.killpg(process.pid, signal.SIGKILL if force else signal.SIGTERM)
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=10)


def _process_log(process: subprocess.Popen, log_path: Path) -> str:
    if process.poll() is None:
        return ""
    return log_path.read_text(encoding="utf-8", errors="replace")


async def _wait_until(description, operation, *, processes=(), timeout=45):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for process, log_path in processes:
            if process.poll() is not None:
                pytest.fail(
                    f"{description}: process exited with {process.returncode}\n"
                    f"{_process_log(process, log_path)}"
                )
        value = await operation()
        if value:
            return value
        await asyncio.sleep(0.1)
    logs = "\n".join(_process_log(process, path) for process, path in processes)
    pytest.fail(f"timed out waiting for {description}\n{logs}")


async def test_prefork_worker_recovers_a_claim_after_its_process_is_killed(
    accounts,  # noqa: F811
    tmp_path,
    record_property,
):
    repository, _, users = accounts
    database_url = os.environ.get("LLM_LAB_TEST_DATABASE_URL")
    broker_url = os.environ.get("LLM_LAB_TEST_BROKER_URL")
    tei_url = os.environ.get("LLM_LAB_TEST_TEI_URL")
    tokenizer_path = os.environ.get("LLM_LAB_TEST_E5_TOKENIZER")
    if not all((database_url, broker_url, tei_url, tokenizer_path)):
        pytest.skip("Run with the isolated worker-process Compose environment")

    environment = _worker_environment(tmp_path)
    first_log = tmp_path / "worker-first.log"
    second_log = tmp_path / "worker-second.log"
    recovery_log = tmp_path / "recovery.log"
    first = _start_owned_process("worker", environment, first_log)
    second = recovery = None
    started = time.monotonic()

    async with httpx.AsyncClient(base_url=tei_url, trust_env=False, timeout=5) as tei:
        reset = await tei.post("/test/reset")
        reset.raise_for_status()
        try:
            key = await AuthService(repository).issue_key(users[0])
            settings = Settings(
                database_url=database_url,
                broker_url=broker_url,
                embedding_url=tei_url,
                worker_tokenizer_path=tokenizer_path,
                upload_root=tmp_path,
                document_minimum_free_bytes=0,
            )
            app = create_app(settings)
            fixture = Path(__file__).parents[1] / "fixtures/pdfs/authored_korean.pdf"
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test"
                ) as api:
                    accepted = await api.post(
                        "/documents",
                        files={"file": ("policy.pdf", fixture.read_bytes(), "application/pdf")},
                        headers={"Authorization": f"Bearer {key}"},
                    )
            assert accepted.status_code == 202, accepted.text
            document_id = accepted.json()["document_id"]

            async def first_attempt_claimed():
                state = (await tei.get("/test/state")).json()
                async with repository.sessions() as session:
                    row = (
                        await session.execute(
                            text(
                                "SELECT d.status, j.attempt_count "
                                "FROM documents d JOIN jobs j ON j.document_id = d.id "
                                "WHERE d.id = :document_id"
                            ),
                            {"document_id": document_id},
                        )
                    ).one()
                return state["requests"] >= 1 and row == ("processing", 1)

            await _wait_until(
                "the first worker to claim and reach TEI",
                first_attempt_claimed,
                processes=((first, first_log),),
            )
            first_claim_ms = round((time.monotonic() - started) * 1000)
            _stop_owned_process(first, force=True)

            redis = Redis.from_url(broker_url)
            try:
                await redis.flushdb()
            finally:
                await redis.aclose()
            async with repository.sessions.begin() as session:
                await session.execute(
                    text(
                        "UPDATE jobs SET "
                        "lease_until = clock_timestamp() - interval '10 minutes', "
                        "next_attempt_at = clock_timestamp() - interval '10 minutes' "
                        "WHERE document_id = :document_id"
                    ),
                    {"document_id": document_id},
                )

            recovery = _start_owned_process("recover", environment, recovery_log)

            async def recovered_message_is_queued():
                client = Redis.from_url(broker_url)
                try:
                    return await client.llen("celery") == 1
                finally:
                    await client.aclose()

            await _wait_until(
                "the recovery scan to republish the expired DB job",
                recovered_message_is_queued,
                processes=((recovery, recovery_log),),
            )
            recovery_publish_ms = round((time.monotonic() - started) * 1000)
            second = _start_owned_process("worker", environment, second_log)

            async def second_attempt_claimed():
                state = (await tei.get("/test/state")).json()
                async with repository.sessions() as session:
                    row = (
                        await session.execute(
                            text(
                                "SELECT d.status, j.attempt_count "
                                "FROM documents d JOIN jobs j ON j.document_id = d.id "
                                "WHERE d.id = :document_id"
                            ),
                            {"document_id": document_id},
                        )
                    ).one()
                return state["requests"] >= 2 and row == ("processing", 2)

            await _wait_until(
                "the second worker to claim the recovered message",
                second_attempt_claimed,
                processes=((second, second_log), (recovery, recovery_log)),
            )
            recovery_dispatch_ms = round((time.monotonic() - started) * 1000)
            released = await tei.post("/test/release")
            released.raise_for_status()

            async def published_once():
                async with repository.sessions() as session:
                    return (
                        await session.execute(
                            text(
                                "SELECT d.status, j.attempt_count, count(c.id) "
                                "FROM documents d JOIN jobs j ON j.document_id = d.id "
                                "LEFT JOIN chunks c ON c.document_id = d.id "
                                "WHERE d.id = :document_id "
                                "GROUP BY d.status, j.attempt_count"
                            ),
                            {"document_id": document_id},
                        )
                    ).one()

            final = await _wait_until(
                "the recovered attempt to publish",
                lambda: _ready_result(published_once),
                processes=((second, second_log), (recovery, recovery_log)),
            )
            assert final == ("ready", 2, 1)
            completed_ms = round((time.monotonic() - started) * 1000)
            timings = {
                "first_claim_ms": first_claim_ms,
                "recovery_publish_ms": recovery_publish_ms,
                "recovery_dispatch_ms": recovery_dispatch_ms,
                "completed_ms": completed_ms,
            }
            record_property("worker_recovery_timings_ms", json.dumps(timings, sort_keys=True))
            print("worker_recovery_timings_ms=" + json.dumps(timings, sort_keys=True))
        finally:
            _stop_owned_process(first, force=True)
            if second is not None:
                _stop_owned_process(second)
            if recovery is not None:
                _stop_owned_process(recovery)


async def _ready_result(operation):
    row = await operation()
    return row if row == ("ready", 2, 1) else None


async def test_broker_outage_keeps_the_job_for_recovery_after_redis_returns(
    accounts,  # noqa: F811
    tmp_path,
):
    repository, _, users = accounts
    database_url = os.environ.get("LLM_LAB_TEST_DATABASE_URL")
    broker_url = os.environ.get("LLM_LAB_TEST_BROKER_URL")
    tei_url = os.environ.get("LLM_LAB_TEST_TEI_URL")
    tokenizer_path = os.environ.get("LLM_LAB_TEST_E5_TOKENIZER")
    if not all((database_url, broker_url, tei_url, tokenizer_path)):
        pytest.skip("Run with the isolated worker-process Compose environment")

    environment = _worker_environment(tmp_path)
    worker_log = tmp_path / "worker-broker-recovery.log"
    recovery_log = tmp_path / "recovery-broker-recovery.log"
    worker = recovery = None
    restored_broker = Redis.from_url(broker_url)
    try:
        await restored_broker.flushdb()
    finally:
        await restored_broker.aclose()

    async with httpx.AsyncClient(base_url=tei_url, trust_env=False, timeout=5) as tei:
        (await tei.post("/test/reset")).raise_for_status()
        (await tei.post("/test/release")).raise_for_status()
        try:
            key = await AuthService(repository).issue_key(users[0])
            # Port 6380 is deliberately closed on the internal Redis service.
            unavailable_broker = "redis://redis:6380/0"
            app = create_app(
                Settings(
                    database_url=database_url,
                    broker_url=unavailable_broker,
                    embedding_url=tei_url,
                    worker_tokenizer_path=tokenizer_path,
                    upload_root=tmp_path,
                    document_minimum_free_bytes=0,
                )
            )
            fixture = Path(__file__).parents[1] / "fixtures/pdfs/authored_korean.pdf"
            accepted_started = time.monotonic()
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test"
                ) as api:
                    accepted = await api.post(
                        "/documents",
                        files={"file": ("policy.pdf", fixture.read_bytes(), "application/pdf")},
                        headers={"Authorization": f"Bearer {key}"},
                    )
            assert accepted.status_code == 202, accepted.text
            assert time.monotonic() - accepted_started < 3
            document_id = accepted.json()["document_id"]
            async with repository.sessions() as session:
                committed = (
                    await session.execute(
                        text(
                            "SELECT d.status, j.attempt_count "
                            "FROM documents d JOIN jobs j ON j.document_id = d.id "
                            "WHERE d.id = :document_id"
                        ),
                        {"document_id": document_id},
                    )
                ).one()
            assert committed == ("queued", 0)

            worker = _start_owned_process("worker", environment, worker_log)
            recovery = _start_owned_process("recover", environment, recovery_log)

            async def published_after_broker_returned():
                async with repository.sessions() as session:
                    row = (
                        await session.execute(
                            text(
                                "SELECT d.status, j.attempt_count, count(c.id) "
                                "FROM documents d JOIN jobs j ON j.document_id = d.id "
                                "LEFT JOIN chunks c ON c.document_id = d.id "
                                "WHERE d.id = :document_id "
                                "GROUP BY d.status, j.attempt_count"
                            ),
                            {"document_id": document_id},
                        )
                    ).one()
                return row if row == ("ready", 1, 1) else None

            final = await _wait_until(
                "recovery through the restored Redis broker",
                published_after_broker_returned,
                processes=((worker, worker_log), (recovery, recovery_log)),
            )
            assert final == ("ready", 1, 1)
        finally:
            if worker is not None:
                _stop_owned_process(worker)
            if recovery is not None:
                _stop_owned_process(recovery)
