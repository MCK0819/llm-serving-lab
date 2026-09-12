import asyncio
import base64
import json
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import psycopg
import pytest
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError, StatementError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.errors import AppError
from app.documents.models import Chunk, Document, Job
from app.documents.repository import DocumentCommitOutcomeUnknown, DocumentRepository
from app.users.types import Identity

pytestmark = pytest.mark.integration


def _inject_one_commit_ack_failure(
    monkeypatch: pytest.MonkeyPatch,
    sessions: async_sessionmaker[AsyncSession],
    *,
    commit_first: bool,
) -> None:
    engine = sessions.kw["bind"]
    dialect = engine.sync_engine.dialect
    original_commit = dialect.do_commit
    fired = False

    def fail_once(connection: object) -> None:
        nonlocal fired
        if not fired:
            fired = True
            if commit_first:
                original_commit(connection)
            raise psycopg.OperationalError("simulated commit acknowledgement loss")
        original_commit(connection)

    monkeypatch.setattr(dialect, "do_commit", fail_once)


@pytest.fixture
async def document_database() -> AsyncIterator[
    tuple[
        async_sessionmaker[AsyncSession],
        list[UUID],
        list[UUID],
    ]
]:
    url = os.environ.get("LLM_LAB_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Use the isolated Docker Compose PostgreSQL test environment")
    engine = create_async_engine(url, hide_parameters=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    organizations = [uuid4(), uuid4()]
    users = [uuid4(), uuid4(), uuid4()]
    try:
        async with engine.begin() as connection:
            for organization_id in organizations:
                await connection.execute(
                    text("INSERT INTO organizations (id, name) VALUES (:id, 'Document test org')"),
                    {"id": organization_id},
                )
            for user_id, organization_id in zip(
                users,
                [organizations[0], organizations[0], organizations[1]],
                strict=True,
            ):
                await connection.execute(
                    text(
                        "INSERT INTO users (id, organization_id, name) "
                        "VALUES (:id, :organization_id, 'Document test user')"
                    ),
                    {"id": user_id, "organization_id": organization_id},
                )
        yield sessions, organizations, users
    finally:
        async with engine.begin() as connection:
            for organization_id in organizations:
                await connection.execute(
                    text("DELETE FROM organizations WHERE id = :id"),
                    {"id": organization_id},
                )
        await engine.dispose()


async def test_create_persists_document_and_one_recoverable_job_atomically(
    document_database: tuple,
) -> None:
    sessions, organizations, users = document_database
    repository = DocumentRepository(sessions)
    identity = Identity(users[0], organizations[0])
    document_id = uuid4()

    receipt = await repository.create(
        identity,
        document_id,
        "휴가 규정.pdf",
        f"{organizations[0]}/{document_id}.pdf",
    )

    assert receipt.document_id == document_id
    assert receipt.status == "queued"
    assert receipt.created_at.tzinfo is not None
    assert "request_id" not in receipt.model_dump()
    async with sessions() as session:
        document = await session.get(Document, document_id)
        jobs = (
            (await session.execute(select(Job).where(Job.document_id == document_id)))
            .scalars()
            .all()
        )
    assert document is not None
    assert document.organization_id == organizations[0]
    assert document.uploader_id == users[0]
    assert document.storage_path == f"{organizations[0]}/{document_id}.pdf"
    assert document.deleted_at is None
    assert len(jobs) == 1
    assert jobs[0].attempt_count == 0
    assert jobs[0].attempt_id is None
    assert jobs[0].lease_until is None


async def test_create_recovers_receipt_when_commit_succeeded_but_acknowledgement_was_lost(
    document_database: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions, organizations, users = document_database
    repository = DocumentRepository(sessions)
    identity = Identity(users[0], organizations[0])
    document_id = uuid4()
    _inject_one_commit_ack_failure(monkeypatch, sessions, commit_first=True)

    receipt = await repository.create(
        identity,
        document_id,
        "committed.pdf",
        "generated/committed.pdf",
    )

    assert receipt.document_id == document_id
    async with sessions() as session:
        assert await session.get(Document, document_id) is not None
        assert await session.get(Job, document_id) is not None


async def test_create_reraises_sqlalchemy_failure_when_transaction_is_definitively_absent(
    document_database: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions, organizations, users = document_database
    repository = DocumentRepository(sessions)
    identity = Identity(users[0], organizations[0])
    document_id = uuid4()
    _inject_one_commit_ack_failure(monkeypatch, sessions, commit_first=False)

    with pytest.raises(SQLAlchemyError):
        await repository.create(
            identity,
            document_id,
            "rolled-back.pdf",
            "generated/rolled-back.pdf",
        )

    async with sessions() as session:
        assert await session.get(Document, document_id) is None


async def test_create_signals_unknown_outcome_when_commit_cannot_be_verified(
    document_database: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions, organizations, users = document_database
    repository = DocumentRepository(sessions)
    identity = Identity(users[0], organizations[0])
    document_id = uuid4()
    _inject_one_commit_ack_failure(monkeypatch, sessions, commit_first=True)

    async def verification_unavailable(*args: object, **kwargs: object) -> None:
        raise SQLAlchemyError("simulated verification outage")

    monkeypatch.setattr(repository, "_find_committed", verification_unavailable)

    with pytest.raises(DocumentCommitOutcomeUnknown) as error:
        await repository.create(
            identity,
            document_id,
            "unknown.pdf",
            "generated/unknown.pdf",
        )

    assert error.value.status == 503
    assert error.value.code == "documents_unavailable"
    assert "simulated" not in str(error.value)
    async with sessions() as session:
        assert await session.get(Document, document_id) is not None


async def test_commit_reconciliation_waits_for_admission_transaction_to_finish(
    document_database: tuple,
) -> None:
    sessions, organizations, users = document_database
    repository = DocumentRepository(sessions)
    identity = Identity(users[0], organizations[0])
    blocker = sessions()
    await blocker.begin()
    await blocker.execute(text("SELECT pg_advisory_xact_lock(741001)"))
    reconciliation = asyncio.create_task(
        repository._find_committed(
            identity,
            uuid4(),
            "pending.pdf",
            "generated/pending.pdf",
        )
    )
    try:
        await asyncio.sleep(0.05)
        assert not reconciliation.done()
    finally:
        await blocker.rollback()
        await blocker.close()
    assert await asyncio.wait_for(reconciliation, timeout=1) is None


async def test_reads_are_shared_within_org_and_hidden_across_orgs(
    document_database: tuple,
) -> None:
    sessions, organizations, users = document_database
    repository = DocumentRepository(sessions)
    owner = Identity(users[0], organizations[0])
    colleague = Identity(users[1], organizations[0])
    outsider = Identity(users[2], organizations[1])
    document_id = uuid4()
    await repository.create(owner, document_id, "shared.pdf", "generated/shared.pdf")

    assert (await repository.get(colleague, document_id)).document_id == document_id
    assert [item.document_id for item in (await repository.list(colleague, 20, None)).items] == [
        document_id
    ]
    for operation in (
        repository.get(outsider, document_id),
        repository.get(owner, uuid4()),
    ):
        with pytest.raises(AppError) as error:
            await operation
        assert (error.value.status, error.value.code) == (404, "not_found")
    assert (await repository.list(outsider, 20, None)).items == []


async def test_only_uploader_deletes_and_tombstone_is_idempotent_but_invisible(
    document_database: tuple,
) -> None:
    sessions, organizations, users = document_database
    repository = DocumentRepository(sessions)
    owner = Identity(users[0], organizations[0])
    colleague = Identity(users[1], organizations[0])
    outsider = Identity(users[2], organizations[1])
    document_id = uuid4()
    await repository.create(owner, document_id, "private.pdf", "generated/private.pdf")

    with pytest.raises(AppError) as forbidden:
        await repository.delete(colleague, document_id)
    assert (forbidden.value.status, forbidden.value.code) == (403, "forbidden")
    with pytest.raises(AppError) as hidden:
        await repository.delete(outsider, document_id)
    assert (hidden.value.status, hidden.value.code) == (404, "not_found")

    await repository.delete(owner, document_id)
    await repository.delete(owner, document_id)
    with pytest.raises(AppError) as missing:
        await repository.get(owner, document_id)
    assert missing.value.status == 404
    assert (await repository.list(owner, 20, None)).items == []
    async with sessions() as session:
        document = await session.get(Document, document_id)
    assert document is not None
    assert document.status == "deleted"
    assert document.deleted_at is not None


async def test_detail_exposes_only_the_stored_public_failure_shape(
    document_database: tuple,
) -> None:
    sessions, organizations, users = document_database
    repository = DocumentRepository(sessions)
    identity = Identity(users[0], organizations[0])
    document_id = uuid4()
    await repository.create(identity, document_id, "broken.pdf", "generated/broken.pdf")
    async with sessions.begin() as session:
        await session.execute(
            update(Document)
            .where(Document.id == document_id)
            .values(
                status="failed",
                failure_code="invalid_pdf",
                failure_message="PDF 파일을 읽을 수 없습니다.",
            )
        )

    detail = await repository.get(identity, document_id)

    assert detail.failure is not None
    assert detail.failure.model_dump() == {
        "code": "invalid_pdf",
        "message": "PDF 파일을 읽을 수 없습니다.",
    }
    assert "storage_path" not in detail.model_dump()
    assert "request_id" not in detail.model_dump()


async def test_tuple_cursor_is_deterministic_across_timestamp_ties_and_deletion(
    document_database: tuple,
) -> None:
    sessions, organizations, users = document_database
    repository = DocumentRepository(sessions)
    identity = Identity(users[0], organizations[0])
    document_ids = [UUID(int=value) for value in (101, 102, 103)]
    for document_id in document_ids:
        await repository.create(
            identity, document_id, f"{document_id}.pdf", f"generated/{document_id}.pdf"
        )
    tied_time = datetime(2026, 9, 10, 12, 30, 45, 123456, tzinfo=UTC)
    async with sessions.begin() as session:
        await session.execute(
            update(Document)
            .where(Document.id.in_(document_ids))
            .values(created_at=tied_time, updated_at=tied_time)
        )

    first = await repository.list(identity, 2, None)

    assert [item.document_id for item in first.items] == [document_ids[2], document_ids[1]]
    assert first.next_cursor is not None
    await repository.delete(identity, document_ids[1])
    second = await repository.list(identity, 2, first.next_cursor)
    assert [item.document_id for item in second.items] == [document_ids[0]]
    assert second.next_cursor is None


def _cursor(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(encoded).rstrip(b"=").decode()


@pytest.mark.parametrize(
    "cursor",
    [
        "!",
        "x" * 513,
        _cursor(
            {
                "created_at": "2026-09-10T12:30:45.123456+00:00",
                "document_id": "00000000-0000-0000-0000-000000000001",
            }
        ),
        _cursor(
            {
                "created_at": "2026-09-10T12:30:45.123456Z",
                "document_id": "00000000-0000-0000-0000-00000000000A",
            }
        ),
        _cursor(
            {
                "created_at": "2026-09-10T12:30:45.123456Z",
                "document_id": "00000000-0000-0000-0000-000000000001",
                "organization_id": "00000000-0000-0000-0000-000000000001",
            }
        ),
    ],
)
async def test_cursor_rejects_malformed_noncanonical_or_non_utc_values(
    document_database: tuple, cursor: str
) -> None:
    sessions, organizations, users = document_database
    repository = DocumentRepository(sessions)

    with pytest.raises(AppError) as error:
        await repository.list(Identity(users[0], organizations[0]), 20, cursor)

    assert (error.value.status, error.value.code) == (422, "invalid_cursor")


async def test_cursor_rejects_noncanonical_base64_padding(document_database: tuple) -> None:
    sessions, organizations, users = document_database
    repository = DocumentRepository(sessions)
    identity = Identity(users[0], organizations[0])
    for value in (201, 202):
        document_id = UUID(int=value)
        await repository.create(identity, document_id, f"{value}.pdf", f"generated/{value}.pdf")
    cursor = (await repository.list(identity, 1, None)).next_cursor
    assert cursor is not None

    with pytest.raises(AppError) as error:
        await repository.list(identity, 1, cursor + "=")

    assert (error.value.status, error.value.code) == (422, "invalid_cursor")


@pytest.mark.parametrize("limit", [0, 101])
async def test_repository_enforces_page_bounds(document_database: tuple, limit: int) -> None:
    sessions, organizations, users = document_database
    with pytest.raises(AppError) as error:
        await DocumentRepository(sessions).list(Identity(users[0], organizations[0]), limit, None)
    assert error.value.status == 422


async def test_concurrent_org_admission_never_exceeds_queued_processing_limit(
    document_database: tuple,
) -> None:
    sessions, organizations, users = document_database
    repository = DocumentRepository(sessions, organization_job_limit=2, global_job_limit=100)
    identity = Identity(users[0], organizations[0])
    existing_id = uuid4()
    await repository.create(identity, existing_id, "existing.pdf", "generated/existing.pdf")
    async with sessions.begin() as session:
        await session.execute(
            update(Document).where(Document.id == existing_id).values(status="processing")
        )

    results = await asyncio.gather(
        *(
            repository.create(
                identity, document_id, f"{document_id}.pdf", f"generated/{document_id}.pdf"
            )
            for document_id in (uuid4(), uuid4())
        ),
        return_exceptions=True,
    )

    assert sum(not isinstance(result, Exception) for result in results) == 1
    rejected = [result for result in results if isinstance(result, AppError)]
    assert len(rejected) == 1
    assert (rejected[0].status, rejected[0].code) == (503, "document_capacity_exceeded")
    async with sessions() as session:
        unfinished = await session.scalar(
            select(func.count())
            .select_from(Document)
            .where(
                Document.organization_id == organizations[0],
                Document.status.in_(("queued", "processing")),
            )
        )
    assert unfinished == 2


async def test_concurrent_global_admission_never_exceeds_limit(
    document_database: tuple,
) -> None:
    sessions, organizations, users = document_database
    repository = DocumentRepository(sessions, organization_job_limit=10, global_job_limit=1)
    identities = [Identity(users[0], organizations[0]), Identity(users[2], organizations[1])]

    results = await asyncio.gather(
        *(
            repository.create(
                identity, document_id, f"{document_id}.pdf", f"generated/{document_id}.pdf"
            )
            for identity, document_id in zip(identities, (uuid4(), uuid4()), strict=True)
        ),
        return_exceptions=True,
    )

    assert sum(not isinstance(result, Exception) for result in results) == 1
    assert sum(isinstance(result, AppError) and result.status == 503 for result in results) == 1


async def test_composite_foreign_keys_reject_cross_tenant_rows(
    document_database: tuple,
) -> None:
    sessions, organizations, users = document_database
    crossed_document = Document(
        id=uuid4(),
        organization_id=organizations[0],
        uploader_id=users[2],
        filename="crossed.pdf",
        storage_path="generated/crossed.pdf",
        status="queued",
    )
    with pytest.raises(IntegrityError):
        async with sessions.begin() as session:
            session.add(crossed_document)

    repository = DocumentRepository(sessions)
    valid_id = uuid4()
    await repository.create(
        Identity(users[0], organizations[0]),
        valid_id,
        "valid.pdf",
        "generated/valid.pdf",
    )
    async with sessions.begin() as session:
        await session.execute(delete(Job).where(Job.document_id == valid_id))
    with pytest.raises(IntegrityError):
        async with sessions.begin() as session:
            session.add(Job(document_id=valid_id, organization_id=organizations[1]))
    with pytest.raises(IntegrityError):
        async with sessions.begin() as session:
            session.add(
                Chunk(
                    document_id=valid_id,
                    organization_id=organizations[1],
                    ordinal=0,
                    page=1,
                    text="다른 조직 청크",
                    embedding=[0.0] * 384,
                    embedding_revision="test-revision",
                )
            )


async def test_vector_dimension_is_fixed_at_384(document_database: tuple) -> None:
    sessions, organizations, users = document_database
    repository = DocumentRepository(sessions)
    document_id = uuid4()
    await repository.create(
        Identity(users[0], organizations[0]),
        document_id,
        "vector.pdf",
        "generated/vector.pdf",
    )

    with pytest.raises((IntegrityError, StatementError)):
        async with sessions.begin() as session:
            session.add(
                Chunk(
                    document_id=document_id,
                    organization_id=organizations[0],
                    ordinal=0,
                    page=1,
                    text="차원 오류",
                    embedding=[0.0] * 383,
                    embedding_revision="test-revision",
                )
            )
