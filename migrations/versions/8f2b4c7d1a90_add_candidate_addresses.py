"""Add ordered candidate commute addresses.

Revision ID: 8f2b4c7d1a90
Revises: b404659b8ee6
Create Date: 2026-07-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "8f2b4c7d1a90"
down_revision: str | Sequence[str] | None = "b404659b8ee6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "candidate_addresses",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("candidate_profile_id", sa.Integer(), nullable=False),
        sa.Column("address_text", sa.String(length=500), nullable=False),
        sa.Column("position", sa.Integer(), server_default="0", nullable=False),
        sa.Column("latitude", sa.Float(), nullable=True),
        sa.Column("longitude", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["candidate_profile_id"],
            ["candidate_profiles.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "candidate_profile_id",
            "address_text",
            name="uq_candidate_address_text",
        ),
    )
    op.create_index(
        "ix_candidate_addresses_candidate_position",
        "candidate_addresses",
        ["candidate_profile_id", "position"],
        unique=False,
    )

    candidate_profiles = sa.table(
        "candidate_profiles",
        sa.column("id", sa.Integer()),
        sa.column("home_location", sa.String(length=500)),
        sa.column("home_latitude", sa.Float()),
        sa.column("home_longitude", sa.Float()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    candidate_addresses = sa.table(
        "candidate_addresses",
        sa.column("candidate_profile_id", sa.Integer()),
        sa.column("address_text", sa.String(length=500)),
        sa.column("position", sa.Integer()),
        sa.column("latitude", sa.Float()),
        sa.column("longitude", sa.Float()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    connection = op.get_bind()
    legacy_profiles = connection.execute(
        sa.select(
            candidate_profiles.c.id,
            candidate_profiles.c.home_location,
            candidate_profiles.c.home_latitude,
            candidate_profiles.c.home_longitude,
            candidate_profiles.c.created_at,
            candidate_profiles.c.updated_at,
        ).where(
            candidate_profiles.c.home_location.is_not(None),
            sa.func.trim(candidate_profiles.c.home_location) != "",
        )
    ).mappings()
    for profile in legacy_profiles:
        connection.execute(
            candidate_addresses.insert().values(
                candidate_profile_id=profile["id"],
                address_text=profile["home_location"].strip(),
                position=0,
                latitude=profile["home_latitude"],
                longitude=profile["home_longitude"],
                created_at=profile["created_at"],
                updated_at=profile["updated_at"],
            )
        )


def downgrade() -> None:
    op.drop_index(
        "ix_candidate_addresses_candidate_position",
        table_name="candidate_addresses",
    )
    op.drop_table("candidate_addresses")
