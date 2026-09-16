import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.errors import AppError
from app.documents.models import Chunk, Document
from app.rag.retrieval import Retriever, SourceChunk

pytestmark = pytest.mark.integration


def _vector(x: float, y: float = 0.0) -> list[float]:
    return [x, y, *([0.0] * 382)]


@pytest.fixture
async def retrieval_database() -> AsyncIterator[
    tuple[async_sessionmaker[AsyncSession], AsyncEngine, tuple[UUID, UUID], tuple[UUID, UUID]]
]:
    url = os.environ.get("LLM_LAB_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Use the isolated Docker Compose PostgreSQL test environment")
    engine = create_async_engine(
        url,
        hide_parameters=True,
        pool_size=1,
        max_overflow=0,
        pool_timeout=1,
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    organizations = (uuid4(), uuid4())
    users = (uuid4(), uuid4())
    try:
        async with engine.begin() as connection:
            for organization_id in organizations:
                await connection.execute(
                    text("INSERT INTO organizations (id, name) VALUES (:id, 'Retrieval test org')"),
                    {"id": organization_id},
                )
            for user_id, organization_id in zip(users, organizations, strict=True):
                await connection.execute(
                    text(
                        "INSERT INTO users (id, organization_id, name) "
                        "VALUES (:id, :organization_id, 'Retrieval test user')"
                    ),
                    {"id": user_id, "organization_id": organization_id},
                )
        yield sessions, engine, organizations, users
    finally:
        async with engine.begin() as connection:
            for organization_id in organizations:
                await connection.execute(
                    text("DELETE FROM organizations WHERE id = :id"),
                    {"id": organization_id},
                )
        await engine.dispose()


async def _insert_document(
    sessions: async_sessionmaker[AsyncSession],
    *,
    document_id: UUID,
    organization_id: UUID,
    uploader_id: UUID,
    filename: str,
    status: str,
    chunks: list[tuple[int, str, list[float], str]],
) -> None:
    deleted_at = datetime.now(UTC) if status == "deleted" else None
    failure_code = "processing_failed" if status == "failed" else None
    failure_message = "문서 처리에 실패했습니다." if status == "failed" else None
    async with sessions.begin() as session:
        session.add(
            Document(
                id=document_id,
                organization_id=organization_id,
                uploader_id=uploader_id,
                filename=filename,
                storage_path=f"trusted/{document_id}.pdf",
                status=status,
                deleted_at=deleted_at,
                failure_code=failure_code,
                failure_message=failure_message,
            )
        )
        await session.flush()
        session.add_all(
            [
                Chunk(
                    document_id=document_id,
                    organization_id=organization_id,
                    ordinal=ordinal,
                    page=ordinal + 1,
                    text=chunk_text,
                    embedding=embedding,
                    embedding_revision=revision,
                )
                for ordinal, chunk_text, embedding, revision in chunks
            ]
        )


async def test_search_filters_tenant_state_deletion_and_revision_before_top_five(
    retrieval_database: tuple,
) -> None:
    sessions, engine, organizations, users = retrieval_database
    allowed_id = UUID(int=501)
    await _insert_document(
        sessions,
        document_id=allowed_id,
        organization_id=organizations[0],
        uploader_id=users[0],
        filename="허용.pdf",
        status="ready",
        chunks=[
            (0, "allowed-0", _vector(1.0, 0.0), "e5-r1"),
            (1, "allowed-1", _vector(5.0, 1.0), "e5-r1"),
            (2, "allowed-2", _vector(4.0, 1.0), "e5-r1"),
            (3, "allowed-3", _vector(3.0, 1.0), "e5-r1"),
            (4, "allowed-4", _vector(2.0, 1.0), "e5-r1"),
            (5, "allowed-5", _vector(1.0, 1.0), "e5-r1"),
        ],
    )
    excluded = [
        (organizations[1], users[1], "ready", "other-org-sentinel", "e5-r1"),
        (organizations[0], users[0], "queued", "queued-sentinel", "e5-r1"),
        (organizations[0], users[0], "processing", "processing-sentinel", "e5-r1"),
        (organizations[0], users[0], "failed", "failed-sentinel", "e5-r1"),
        (organizations[0], users[0], "deleted", "deleted-sentinel", "e5-r1"),
        (organizations[0], users[0], "ready", "wrong-revision-sentinel", "e5-r0"),
    ]
    for organization_id, user_id, status, sentinel, revision in excluded:
        await _insert_document(
            sessions,
            document_id=uuid4(),
            organization_id=organization_id,
            uploader_id=user_id,
            filename=f"{sentinel}.pdf",
            status=status,
            chunks=[(0, sentinel, _vector(1.0, 0.0), revision)],
        )

    results = await Retriever(sessions).search(organizations[0], _vector(1.0, 0.0), "e5-r1")

    assert results == [
        SourceChunk(allowed_id, 0, "허용.pdf", 1, "allowed-0"),
        SourceChunk(allowed_id, 1, "허용.pdf", 2, "allowed-1"),
        SourceChunk(allowed_id, 2, "허용.pdf", 3, "allowed-2"),
        SourceChunk(allowed_id, 3, "허용.pdf", 4, "allowed-3"),
        SourceChunk(allowed_id, 4, "허용.pdf", 5, "allowed-4"),
    ]
    assert engine.sync_engine.pool.checkedout() == 0


async def test_equal_cosine_distances_are_ordered_by_document_then_ordinal(
    retrieval_database: tuple,
) -> None:
    sessions, _, organizations, users = retrieval_database
    earlier_id = UUID(int=10)
    later_id = UUID(int=20)
    await _insert_document(
        sessions,
        document_id=later_id,
        organization_id=organizations[0],
        uploader_id=users[0],
        filename="later.pdf",
        status="ready",
        chunks=[
            (2, "later-2", _vector(1.0), "e5-r1"),
            (0, "later-0", _vector(1.0), "e5-r1"),
            (1, "later-1", _vector(1.0), "e5-r1"),
        ],
    )
    await _insert_document(
        sessions,
        document_id=earlier_id,
        organization_id=organizations[0],
        uploader_id=users[0],
        filename="earlier.pdf",
        status="ready",
        chunks=[(0, "earlier-0", _vector(1.0), "e5-r1")],
    )

    results = await Retriever(sessions).search(organizations[0], _vector(1.0), "e5-r1")

    assert [(chunk.document_id, chunk.ordinal) for chunk in results] == [
        (earlier_id, 0),
        (later_id, 0),
        (later_id, 1),
        (later_id, 2),
    ]


async def test_search_never_returns_more_than_top_five(retrieval_database: tuple) -> None:
    sessions, _, organizations, users = retrieval_database
    document_id = UUID(int=30)
    await _insert_document(
        sessions,
        document_id=document_id,
        organization_id=organizations[0],
        uploader_id=users[0],
        filename="top-five.pdf",
        status="ready",
        chunks=[(ordinal, f"chunk-{ordinal}", _vector(1.0), "e5-r1") for ordinal in range(6)],
    )

    results = await Retriever(sessions).search(organizations[0], _vector(1.0), "e5-r1", limit=10)

    assert [chunk.ordinal for chunk in results] == [0, 1, 2, 3, 4]


async def test_database_failure_becomes_a_safe_targeted_503() -> None:
    engine = create_async_engine(
        "postgresql+psycopg://retrieval:secret-marker@127.0.0.1:1/retrieval",
        hide_parameters=True,
        connect_args={"connect_timeout": 1},
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        with pytest.raises(AppError) as error:
            await Retriever(sessions).search(uuid4(), _vector(1.0), "e5-r1")
    finally:
        await engine.dispose()

    assert (error.value.status, error.value.code) == (503, "retrieval_unavailable")
    assert error.value.message == "문서 검색 저장소를 사용할 수 없습니다."
    assert "secret-marker" not in str(error.value)
