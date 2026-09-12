"""Tenant-scoped documents, recoverable jobs, and pgvector chunks."""

import pgvector.sqlalchemy
import sqlalchemy as sa
from alembic import op

revision = "0002_documents_jobs_chunks"
down_revision = "0001_users"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_unique_constraint("uq_users_id_organization_id", "users", ["id", "organization_id"])
    op.create_table(
        "documents",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "organization_id",
            sa.Uuid(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("uploader_id", sa.Uuid(), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("storage_path", sa.String(1024), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="queued"),
        sa.Column("failure_code", sa.String(100)),
        sa.Column("failure_message", sa.String(500)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("id", "organization_id", name="uq_documents_id_organization_id"),
        sa.ForeignKeyConstraint(
            ["uploader_id", "organization_id"],
            ["users.id", "users.organization_id"],
            name="fk_documents_uploader_organization",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'processing', 'ready', 'failed', 'deleted')",
            name="ck_documents_status",
        ),
        sa.CheckConstraint(
            "(status = 'deleted') = (deleted_at IS NOT NULL)",
            name="ck_documents_deleted_state",
        ),
        sa.CheckConstraint(
            "status <> 'failed' OR (failure_code IS NOT NULL AND failure_message IS NOT NULL)",
            name="ck_documents_failure_shape",
        ),
    )
    op.create_index(
        "ix_documents_organization_created_id",
        "documents",
        ["organization_id", "created_at", "id"],
    )
    op.create_table(
        "jobs",
        sa.Column("document_id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_id", sa.Uuid()),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["document_id", "organization_id"],
            ["documents.id", "documents.organization_id"],
            name="fk_jobs_document_organization",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("attempt_count >= 0", name="ck_jobs_attempt_count"),
    )
    op.create_index("ix_jobs_recovery", "jobs", ["next_attempt_at", "lease_until"])
    op.create_table(
        "chunks",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("page", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("embedding", pgvector.sqlalchemy.Vector(dim=384), nullable=False),
        sa.Column("embedding_revision", sa.String(200), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["document_id", "organization_id"],
            ["documents.id", "documents.organization_id"],
            name="fk_chunks_document_organization",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "document_id",
            "ordinal",
            "embedding_revision",
            name="uq_chunks_document_ordinal_revision",
        ),
        sa.CheckConstraint("ordinal >= 0", name="ck_chunks_ordinal"),
        sa.CheckConstraint("page >= 1", name="ck_chunks_page"),
    )
    op.create_index("ix_chunks_organization_document", "chunks", ["organization_id", "document_id"])


def downgrade() -> None:
    op.drop_table("chunks")
    op.drop_table("jobs")
    op.drop_table("documents")
    op.drop_constraint("uq_users_id_organization_id", "users", type_="unique")
