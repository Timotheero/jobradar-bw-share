"""Add editable primary and additional role search terms.

Revision ID: e7a4c2d9b610
Revises: f4a8b2c6d901
Create Date: 2026-07-22
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "e7a4c2d9b610"
down_revision: str | Sequence[str] | None = "f4a8b2c6d901"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PRIMARY_TERMS = (
    "Assistenz der Geschäftsführung",
    "Executive Assistant",
)
_ADDITIONAL_TERMS = (
    "Assistenz",
    "Assistant",
    "Geschäftsführung",
    "Geschäftsleitung",
    "Referent",
    "Office Manager",
    "Chief of Staff",
    "CEO Office",
    "Projektkoordination",
    "Stabsmitarbeiter",
    "Vorstandsassistenz",
    "Executive Office",
    "Management Coordinator",
    "Referent des Vorstands",
)


def _unique_terms(*groups: Sequence[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for value in group:
            term = str(value).strip()
            key = term.casefold()
            if term and key not in seen:
                result.append(term)
                seen.add(key)
    return result


def upgrade() -> None:
    op.add_column(
        "preference_profiles",
        sa.Column("primary_role_terms", sa.JSON(), nullable=True),
    )
    op.add_column(
        "preference_profiles",
        sa.Column("additional_role_terms", sa.JSON(), nullable=True),
    )

    preferences = sa.table(
        "preference_profiles",
        sa.column("id", sa.Integer()),
        sa.column("role_terms", sa.JSON()),
        sa.column("primary_role_terms", sa.JSON()),
        sa.column("additional_role_terms", sa.JSON()),
    )
    connection = op.get_bind()
    primary_keys = {term.casefold() for term in _PRIMARY_TERMS}
    rows = list(
        connection.execute(
            sa.select(preferences.c.id, preferences.c.role_terms)
        ).mappings()
    )
    for row in rows:
        existing = row["role_terms"] if isinstance(row["role_terms"], list) else []
        additional = [
            term
            for term in _unique_terms(_ADDITIONAL_TERMS, existing)
            if term.casefold() not in primary_keys
        ]
        connection.execute(
            preferences.update()
            .where(preferences.c.id == row["id"])
            .values(
                primary_role_terms=list(_PRIMARY_TERMS),
                additional_role_terms=additional,
            )
        )

    with op.batch_alter_table("preference_profiles") as batch_op:
        batch_op.alter_column("primary_role_terms", nullable=False)
        batch_op.alter_column("additional_role_terms", nullable=False)
        batch_op.drop_column("role_terms")
    if rows:
        app_settings = sa.table(
            "app_settings",
            sa.column("key", sa.String()),
            sa.column("value", sa.JSON()),
            sa.column("description", sa.String()),
            sa.column("updated_at", sa.DateTime(timezone=True)),
        )
        key = "scoring.rescore_request"
        payload = {
            "requested_at": datetime.now(UTC).isoformat(),
            "reason": "role_taxonomy_migrated",
            "profile_version": None,
            "preference_version": None,
            "status": "pending",
        }
        existing_setting = connection.scalar(
            sa.select(app_settings.c.key).where(app_settings.c.key == key)
        )
        if existing_setting is None:
            connection.execute(
                app_settings.insert().values(
                    key=key,
                    value=payload,
                    description="Local rescore request after editable role taxonomy migration.",
                    updated_at=datetime.now(UTC),
                )
            )
        else:
            connection.execute(
                app_settings.update()
                .where(app_settings.c.key == key)
                .values(
                    value=payload,
                    description="Local rescore request after editable role taxonomy migration.",
                    updated_at=datetime.now(UTC),
                )
            )


def downgrade() -> None:
    op.add_column(
        "preference_profiles",
        sa.Column("role_terms", sa.JSON(), nullable=True),
    )
    preferences = sa.table(
        "preference_profiles",
        sa.column("id", sa.Integer()),
        sa.column("role_terms", sa.JSON()),
        sa.column("primary_role_terms", sa.JSON()),
        sa.column("additional_role_terms", sa.JSON()),
    )
    connection = op.get_bind()
    rows = connection.execute(
        sa.select(
            preferences.c.id,
            preferences.c.primary_role_terms,
            preferences.c.additional_role_terms,
        )
    ).mappings()
    for row in rows:
        primary = row["primary_role_terms"] if isinstance(row["primary_role_terms"], list) else []
        additional = (
            row["additional_role_terms"]
            if isinstance(row["additional_role_terms"], list)
            else []
        )
        connection.execute(
            preferences.update()
            .where(preferences.c.id == row["id"])
            .values(role_terms=_unique_terms(primary, additional))
        )

    with op.batch_alter_table("preference_profiles") as batch_op:
        batch_op.alter_column("role_terms", nullable=False)
        batch_op.drop_column("additional_role_terms")
        batch_op.drop_column("primary_role_terms")
