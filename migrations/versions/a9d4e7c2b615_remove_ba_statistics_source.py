"""Remove the unused BA aggregate statistics source.

Revision ID: a9d4e7c2b615
Revises: e7a4c2d9b610
Create Date: 2026-07-24
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "a9d4e7c2b615"
down_revision: str | Sequence[str] | None = "e7a4c2d9b610"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SOURCE_SLUG = "ba_statistics_jobs"


def upgrade() -> None:
    connection = op.get_bind()
    source_id = connection.execute(
        sa.text("SELECT id FROM sources WHERE slug = :slug"), {"slug": _SOURCE_SLUG}
    ).scalar_one_or_none()
    if source_id is None:
        return

    connection.execute(
        sa.text("UPDATE crawl_runs SET source_id = NULL WHERE source_id = :source_id"),
        {"source_id": source_id},
    )
    connection.execute(
        sa.text("DELETE FROM sources WHERE id = :source_id"), {"source_id": source_id}
    )


def downgrade() -> None:
    connection = op.get_bind()
    exists = connection.execute(
        sa.text("SELECT 1 FROM sources WHERE slug = :slug"), {"slug": _SOURCE_SLUG}
    ).scalar_one_or_none()
    if exists is not None:
        return

    sources = sa.table(
        "sources",
        sa.column("name", sa.String()),
        sa.column("slug", sa.String()),
        sa.column("kind", sa.String()),
        sa.column("base_url", sa.String()),
        sa.column("enabled", sa.Boolean()),
        sa.column("is_official", sa.Boolean()),
        sa.column("free_api", sa.Boolean()),
        sa.column("status", sa.String()),
        sa.column("metadata", sa.JSON()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    now = datetime.now(UTC)
    op.bulk_insert(
        sources,
        [
            {
                "name": "Statistik der Bundesagentur für Arbeit – gemeldete Arbeitsstellen",
                "slug": _SOURCE_SLUG,
                "kind": "statistics",
                "base_url": "https://statistik-dr.arbeitsagentur.de/bifrontend/bids-api",
                "enabled": False,
                "is_official": True,
                "free_api": True,
                "status": "prepared",
                "metadata": {
                    "acquisition": "official_api",
                    "experimental": False,
                    "default_enabled": False,
                    "coverage": "Aggregierte BA-Arbeitsmarktstatistik, keine Stellenanzeigen",
                },
                "created_at": now,
                "updated_at": now,
            }
        ],
    )
