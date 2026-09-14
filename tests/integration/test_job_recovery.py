import asyncio
import os
from collections.abc import AsyncIterator
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.documents.chunking import Chunk as ParsedChunk
from app.documents.jobs import JobRepository, Lease
from app.documents.models import Chunk, Document, Job

pytestmark = pytest.mark.integration


@pytest.fixture
async def job_database() -> AsyncIterator[tuple[async_sessionmaker[AsyncSession], UUID, UUID]]:
    url = os.environ.get("LLM_LAB_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Use the isolated Docker Compose PostgreSQL test environment")
    engine = create_async_engine(url, hide_parameters=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    organization_id = uuid4()
    user_id = uuid4()
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text("INSERT INTO organizations (id, name) VALUES (:id, 'Job test org')"),
                {"id": organization_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO users (id, organization_id, name) "
                    "VALUES (:id, :organization_id, 'Job test user')"
                ),
                {"id": user_id, "organization_id": organization_id},
            )
        yield sessions, organization_id, user_id
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM organizations WHERE id = :id"), {"id": organization_id}
            )
        await engine.dispose()


@pytest.fixture
async def jobs(job_database: tuple) -> JobRepository:
    sessions, _, _ = job_database
    return JobRepository(sessions)


@pytest.fixture
async def queued_document(job_database: tuple) -> UUID:
    sessions, organization_id, user_id = job_database
    document_id = uuid4()
    async with sessions.begin() as session:
        session.add(
            Document(
                id=document_id,
                organization_id=organization_id,
                uploader_id=user_id,
                filename="queued.pdf",
                storage_path=f"trusted/{document_id}.pdf",
                status="queued",
            )
        )
        session.add(Job(document_id=document_id, organization_id=organization_id))
    return document_id


@pytest.fixture
def expire_lease(job_database: tuple):
    sessions, _, _ = job_database

    async def expire(lease: Lease, *, seconds: int = 60) -> None:
        async with sessions.begin() as session:
            await session.execute(
                update(Job)
                .where(Job.document_id == lease.document_id)
                .values(
                    lease_until=text(f"clock_timestamp() - interval '{seconds} seconds'"),
                    attempt_started_at=text("clock_timestamp() - interval '301 seconds'"),
                )
            )

    return expire


async def test_claim_uses_database_authority_and_only_one_duplicate_claim_wins(
    jobs: JobRepository, queued_document: UUID, job_database: tuple
) -> None:
    sessions, organization_id, _ = job_database

    first, duplicate = await asyncio.gather(
        jobs.claim(queued_document), jobs.claim(queued_document)
    )

    leases = [lease for lease in (first, duplicate) if lease is not None]
    assert len(leases) == 1
    assert leases[0].document_id == queued_document
    assert leases[0].organization_id == organization_id
    assert leases[0].attempt_count == 1
    assert leases[0].storage_path == f"trusted/{queued_document}.pdf"
    async with sessions() as session:
        job = await session.get(Job, queued_document)
        document = await session.get(Document, queued_document)
    assert job is not None and job.attempt_count == 1
    assert job.attempt_started_at is not None
    assert document is not None and document.status == "processing"


async def test_old_attempt_cannot_publish_after_expiry_and_reclaim(
    jobs: JobRepository,
    queued_document: UUID,
    expire_lease,
    job_database: tuple,
) -> None:
    sessions, _, _ = job_database
    first = await jobs.claim(queued_document)
    assert first is not None
    await expire_lease(first)

    second = await jobs.claim(queued_document)

    assert second is not None
    assert second.attempt_id != first.attempt_id
    assert second.attempt_count == 2
    assert await jobs.publish(first, [], [], "test-revision") is False
    async with sessions() as session:
        assert (
            await session.scalar(select(Chunk.id).where(Chunk.document_id == queued_document))
            is None
        )
        document = await session.get(Document, queued_document)
    assert document is not None and document.status == "processing"


async def test_renew_only_extends_a_current_active_attempt_and_caps_absolute_runtime(
    jobs: JobRepository,
    queued_document: UUID,
    expire_lease,
    job_database: tuple,
) -> None:
    sessions, _, _ = job_database
    lease = await jobs.claim(queued_document)
    assert lease is not None
    stale = Lease(
        lease.document_id,
        lease.organization_id,
        uuid4(),
        lease.attempt_count,
        lease.storage_path,
    )

    assert await jobs.renew(stale) is False
    assert await jobs.renew(lease) is True
    async with sessions.begin() as session:
        await session.execute(
            update(Job)
            .where(Job.document_id == lease.document_id)
            .values(attempt_started_at=text("clock_timestamp() - interval '299 seconds'"))
        )
    assert await jobs.renew(lease) is True
    async with sessions() as session:
        remaining = await session.scalar(
            select(Job.lease_until - text("clock_timestamp()")).where(
                Job.document_id == lease.document_id
            )
        )
    assert remaining is not None and remaining < timedelta(seconds=2)
    await expire_lease(lease, seconds=1)
    assert await jobs.renew(lease) is False


async def test_publish_inserts_one_chunk_set_and_duplicate_completion_is_rejected(
    jobs: JobRepository, queued_document: UUID, job_database: tuple
) -> None:
    sessions, organization_id, _ = job_database
    lease = await jobs.claim(queued_document)
    assert lease is not None
    parsed = [
        ParsedChunk(ordinal=0, page=1, text="첫 문단"),
        ParsedChunk(ordinal=1, page=2, text="둘째 문단"),
    ]
    vectors = [[1.0] + [0.0] * 383, [0.0, 1.0] + [0.0] * 382]

    assert await jobs.publish(lease, parsed, vectors, "e5-test") is True
    assert await jobs.publish(lease, parsed, vectors, "e5-test") is False
    async with sessions() as session:
        document = await session.get(Document, queued_document)
        chunks = (
            (
                await session.execute(
                    select(Chunk)
                    .where(Chunk.document_id == queued_document)
                    .order_by(Chunk.ordinal)
                )
            )
            .scalars()
            .all()
        )
    assert document is not None and document.status == "ready"
    assert [(chunk.organization_id, chunk.ordinal, chunk.page, chunk.text) for chunk in chunks] == [
        (organization_id, 0, 1, "첫 문단"),
        (organization_id, 1, 2, "둘째 문단"),
    ]
    assert all(chunk.embedding_revision == "e5-test" for chunk in chunks)


async def test_deleted_document_cannot_be_revived_or_gain_chunks(
    jobs: JobRepository, queued_document: UUID, job_database: tuple
) -> None:
    sessions, _, _ = job_database
    lease = await jobs.claim(queued_document)
    assert lease is not None
    async with sessions.begin() as session:
        await session.execute(
            update(Document)
            .where(Document.id == queued_document)
            .values(
                status="deleted",
                deleted_at=text("clock_timestamp()"),
                updated_at=text("clock_timestamp()"),
            )
        )

    published = await jobs.publish(
        lease,
        [ParsedChunk(ordinal=0, page=1, text="삭제 뒤 결과")],
        [[1.0] + [0.0] * 383],
        "e5-test",
    )

    assert published is False
    async with sessions() as session:
        document = await session.get(Document, queued_document)
        chunks = await session.scalar(select(Chunk.id).where(Chunk.document_id == queued_document))
    assert document is not None and document.status == "deleted"
    assert chunks is None


async def test_retryable_failure_waits_for_backoff_and_recovery_lists_it_when_due(
    jobs: JobRepository, queued_document: UUID, job_database: tuple
) -> None:
    sessions, _, _ = job_database
    first = await jobs.claim(queued_document)
    assert first is not None

    await jobs.fail(first, "embedding_unavailable", retryable=True)

    assert await jobs.claim(queued_document) is None
    assert queued_document not in await jobs.recover_due()
    async with sessions.begin() as session:
        await session.execute(
            update(Job)
            .where(Job.document_id == queued_document)
            .values(next_attempt_at=text("clock_timestamp() - interval '1 second'"))
        )
    assert queued_document in await jobs.recover_due()
    second = await jobs.claim(queued_document)
    assert second is not None and second.attempt_count == 2


async def test_crash_expiry_waits_ten_then_thirty_seconds_before_retry(
    jobs: JobRepository, queued_document: UUID, job_database: tuple
) -> None:
    sessions, _, _ = job_database
    first = await jobs.claim(queued_document)
    assert first is not None
    async with sessions.begin() as session:
        await session.execute(
            update(Job)
            .where(Job.document_id == queued_document)
            .values(lease_until=text("clock_timestamp() - interval '1 second'"))
        )
    assert await jobs.claim(queued_document) is None
    assert queued_document not in await jobs.recover_due()
    async with sessions.begin() as session:
        await session.execute(
            update(Job)
            .where(Job.document_id == queued_document)
            .values(lease_until=text("clock_timestamp() - interval '11 seconds'"))
        )
    assert queued_document in await jobs.recover_due()
    second = await jobs.claim(queued_document)
    assert second is not None and second.attempt_count == 2
    async with sessions.begin() as session:
        await session.execute(
            update(Job)
            .where(Job.document_id == queued_document)
            .values(lease_until=text("clock_timestamp() - interval '11 seconds'"))
        )
    assert await jobs.claim(queued_document) is None
    async with sessions.begin() as session:
        await session.execute(
            update(Job)
            .where(Job.document_id == queued_document)
            .values(lease_until=text("clock_timestamp() - interval '31 seconds'"))
        )
    third = await jobs.claim(queued_document)
    assert third is not None and third.attempt_count == 3


async def test_third_expired_attempt_is_failed_safely_without_another_claim(
    jobs: JobRepository,
    queued_document: UUID,
    expire_lease,
    job_database: tuple,
) -> None:
    sessions, _, _ = job_database
    lease = await jobs.claim(queued_document)
    assert lease is not None
    for expected_count in (2, 3):
        await expire_lease(lease)
        lease = await jobs.claim(queued_document)
        assert lease is not None and lease.attempt_count == expected_count
    async with sessions.begin() as session:
        await session.execute(
            update(Job)
            .where(Job.document_id == queued_document)
            .values(lease_until=text("clock_timestamp() - interval '1 second'"))
        )

    assert queued_document not in await jobs.recover_due()
    assert await jobs.claim(queued_document) is None
    async with sessions() as session:
        document = await session.get(Document, queued_document)
        job = await session.get(Job, queued_document)
    assert document is not None
    assert (document.status, document.failure_code, document.failure_message) == (
        "failed",
        "processing_failed",
        "문서 처리에 실패했습니다.",
    )
    assert job is not None and job.attempt_count == 3


async def test_permanent_failure_uses_allowlisted_public_message_and_unknown_code_is_hidden(
    jobs: JobRepository, queued_document: UUID, job_database: tuple
) -> None:
    sessions, organization_id, user_id = job_database
    invalid_lease = await jobs.claim(queued_document)
    assert invalid_lease is not None
    await jobs.fail(invalid_lease, "invalid_pdf", retryable=False)
    async with sessions() as session:
        invalid_document = await session.get(Document, queued_document)
    assert invalid_document is not None
    assert (invalid_document.failure_code, invalid_document.failure_message) == (
        "invalid_pdf",
        "PDF를 처리할 수 없습니다.",
    )

    unknown_id = uuid4()
    async with sessions.begin() as session:
        session.add(
            Document(
                id=unknown_id,
                organization_id=organization_id,
                uploader_id=user_id,
                filename="unknown.pdf",
                storage_path="trusted/unknown.pdf",
                status="queued",
            )
        )
        session.add(Job(document_id=unknown_id, organization_id=organization_id))
    unknown_lease = await jobs.claim(unknown_id)
    assert unknown_lease is not None
    await jobs.fail(unknown_lease, "C:\\private\\secret.pdf: bearer-token", retryable=False)
    async with sessions() as session:
        unknown_document = await session.get(Document, unknown_id)
    assert unknown_document is not None
    assert (unknown_document.failure_code, unknown_document.failure_message) == (
        "processing_failed",
        "문서 처리에 실패했습니다.",
    )
    assert "secret" not in unknown_document.failure_message


async def test_recover_due_lists_queued_missed_notifications_only_when_due(
    jobs: JobRepository, queued_document: UUID, job_database: tuple
) -> None:
    sessions, _, _ = job_database
    assert queued_document in await jobs.recover_due()
    async with sessions.begin() as session:
        await session.execute(
            update(Job)
            .where(Job.document_id == queued_document)
            .values(next_attempt_at=text("clock_timestamp() + interval '30 seconds'"))
        )
    assert queued_document not in await jobs.recover_due()


async def test_publish_rejects_mismatched_chunk_and_vector_counts_before_writing(
    jobs: JobRepository, queued_document: UUID, job_database: tuple
) -> None:
    sessions, _, _ = job_database
    lease = await jobs.claim(queued_document)
    assert lease is not None
    with pytest.raises(ValueError, match="same length"):
        await jobs.publish(
            lease,
            [ParsedChunk(ordinal=0, page=1, text="청크")],
            [],
            "e5-test",
        )
    async with sessions() as session:
        assert (
            await session.scalar(select(Chunk.id).where(Chunk.document_id == queued_document))
            is None
        )
