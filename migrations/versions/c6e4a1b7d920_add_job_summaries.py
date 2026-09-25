"""Add cached automatic job summaries.

Revision ID: c6e4a1b7d920
Revises: 8f2b4c7d1a90
Create Date: 2026-07-22
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c6e4a1b7d920"
down_revision: str | Sequence[str] | None = "8f2b4c7d1a90"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "job_summaries",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("job_id", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(length=128), nullable=False),
        sa.Column("profile_version", sa.Integer(), nullable=True),
        sa.Column("preference_version", sa.Integer(), nullable=True),
        sa.Column("locale", sa.String(length=10), nullable=False),
        sa.Column("model", sa.String(length=100), nullable=False),
        sa.Column("reasoning_effort", sa.String(length=20), nullable=False),
        sa.Column("overview", sa.Text(), nullable=False),
        sa.Column("key_points", sa.JSON(), nullable=False),
        sa.Column("missing_information", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["job_postings.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id"),
    )
    op.create_index("ix_job_summaries_job_id", "job_summaries", ["job_id"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_job_summaries_job_id", table_name="job_summaries")
    op.drop_table("job_summaries")
