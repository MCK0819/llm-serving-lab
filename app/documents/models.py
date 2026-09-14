from datetime import datetime
from uuid import UUID, uuid4

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.users import models as user_models  # noqa: F401


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("id", "organization_id", name="uq_documents_id_organization_id"),
        ForeignKeyConstraint(
            ["uploader_id", "organization_id"],
            ["users.id", "users.organization_id"],
            name="fk_documents_uploader_organization",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "status IN ('queued', 'processing', 'ready', 'failed', 'deleted')",
            name="ck_documents_status",
        ),
        CheckConstraint(
            "(status = 'deleted') = (deleted_at IS NOT NULL)",
            name="ck_documents_deleted_state",
        ),
        CheckConstraint(
            "status <> 'failed' OR (failure_code IS NOT NULL AND failure_message IS NOT NULL)",
            name="ck_documents_failure_shape",
        ),
        Index("ix_documents_organization_created_id", "organization_id", "created_at", "id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    uploader_id: Mapped[UUID] = mapped_column(nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    storage_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="queued")
    failure_code: Mapped[str | None] = mapped_column(String(100))
    failure_message: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["document_id", "organization_id"],
            ["documents.id", "documents.organization_id"],
            name="fk_jobs_document_organization",
            ondelete="CASCADE",
        ),
        CheckConstraint("attempt_count >= 0", name="ck_jobs_attempt_count"),
        Index("ix_jobs_recovery", "next_attempt_at", "lease_until"),
    )

    document_id: Mapped[UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[UUID] = mapped_column(nullable=False)
    attempt_id: Mapped[UUID | None] = mapped_column()
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    attempt_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Chunk(Base):
    __tablename__ = "chunks"
    __table_args__ = (
        ForeignKeyConstraint(
            ["document_id", "organization_id"],
            ["documents.id", "documents.organization_id"],
            name="fk_chunks_document_organization",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "document_id",
            "ordinal",
            "embedding_revision",
            name="uq_chunks_document_ordinal_revision",
        ),
        CheckConstraint("ordinal >= 0", name="ck_chunks_ordinal"),
        CheckConstraint("page >= 1", name="ck_chunks_page"),
        Index("ix_chunks_organization_document", "organization_id", "document_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    document_id: Mapped[UUID] = mapped_column(nullable=False)
    organization_id: Mapped[UUID] = mapped_column(nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    page: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(384), nullable=False)
    embedding_revision: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
