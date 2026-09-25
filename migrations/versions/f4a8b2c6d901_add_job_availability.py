"""Add explicit job availability checks.

Revision ID: f4a8b2c6d901
Revises: c3d7e9f1a2b4
Create Date: 2026-07-22
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f4a8b2c6d901"
down_revision: str | Sequence[str] | None = "c3d7e9f1a2b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "job_postings",
        sa.Column(
            "availability_status",
            sa.String(length=30),
            server_default="unverified",
            nullable=False,
        ),
    )
    op.add_column(
        "job_postings",
        sa.Column("availability_checked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "job_postings",
        sa.Column("availability_check_method", sa.String(length=50), nullable=True),
    )
    op.add_column(
        "job_postings",
        sa.Column("availability_reason", sa.String(length=500), nullable=True),
    )
    op.add_column(
        "job_postings",
        sa.Column("availability_failures", sa.Integer(), server_default="0", nullable=False),
    )
    op.create_index(
        "ix_jobs_availability_status",
        "job_postings",
        ["availability_status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_jobs_availability_status", table_name="job_postings")
    op.drop_column("job_postings", "availability_failures")
    op.drop_column("job_postings", "availability_reason")
    op.drop_column("job_postings", "availability_check_method")
    op.drop_column("job_postings", "availability_checked_at")
    op.drop_column("job_postings", "availability_status")
