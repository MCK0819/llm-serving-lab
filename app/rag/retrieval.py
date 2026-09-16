from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import and_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import AppError
from app.documents.models import Chunk, Document

_MAX_RESULTS = 5


@dataclass(frozen=True, slots=True)
class SourceChunk:
    document_id: UUID
    ordinal: int
    filename: str
    page: int
    text: str


class Retriever:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    async def search(
        self,
        organization_id: UUID,
        vector: list[float],
        revision: str,
        limit: int = 5,
    ) -> list[SourceChunk]:
        statement = (
            select(
                Chunk.document_id,
                Chunk.ordinal,
                Document.filename,
                Chunk.page,
                Chunk.text,
            )
            .join(
                Document,
                and_(
                    Document.id == Chunk.document_id,
                    Document.organization_id == Chunk.organization_id,
                ),
            )
            .where(
                Document.organization_id == organization_id,
                Document.status == "ready",
                Document.deleted_at.is_(None),
                Chunk.embedding_revision == revision,
            )
            .order_by(
                Chunk.embedding.cosine_distance(vector),
                Document.id,
                Chunk.ordinal,
            )
            .limit(min(limit, _MAX_RESULTS))
        )
        try:
            async with self.sessions() as session:
                rows = (await session.execute(statement)).all()
        except SQLAlchemyError:
            raise AppError(
                503,
                "retrieval_unavailable",
                "문서 검색 저장소를 사용할 수 없습니다.",
            ) from None
        return [SourceChunk(*row) for row in rows]
