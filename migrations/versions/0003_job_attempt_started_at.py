"""Bound each document job attempt by its absolute start time."""

import sqlalchemy as sa
from alembic import op

revision = "0003_job_attempt_started_at"
down_revision = "0002_documents_jobs_chunks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("attempt_started_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    op.drop_column("jobs", "attempt_started_at")
