"""Add versioned application drafts.

Revision ID: c3d7e9f1a2b4
Revises: c6e4a1b7d920
Create Date: 2026-07-22
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c3d7e9f1a2b4"
down_revision: str | Sequence[str] | None = "c6e4a1b7d920"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "application_drafts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("job_id", sa.Integer(), nullable=False),
        sa.Column("profile_version", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("language", sa.String(length=10), nullable=False),
        sa.Column("cv_draft", sa.Text(), nullable=False),
        sa.Column("cover_letter", sa.Text(), nullable=False),
        sa.Column("evidence_map", sa.JSON(), nullable=False),
        sa.Column("questions", sa.JSON(), nullable=False),
        sa.Column("review", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["job_postings.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "job_id",
            "revision",
            name="uq_application_draft_job_revision",
        ),
    )
    op.create_index(
        "ix_application_drafts_job_id",
        "application_drafts",
        ["job_id"],
        unique=False,
    )
    op.create_index(
        "ix_application_drafts_status",
        "application_drafts",
        ["status"],
        unique=False,
    )
    op.create_index(
        "ix_application_drafts_job_updated",
        "application_drafts",
        ["job_id", "updated_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_application_drafts_job_updated", table_name="application_drafts")
    op.drop_index("ix_application_drafts_status", table_name="application_drafts")
    op.drop_index("ix_application_drafts_job_id", table_name="application_drafts")
    op.drop_table("application_drafts")
