from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from sqlalchemy import create_engine, inspect, text

from jobradar.migrations import upgrade_database


def test_initial_migration_builds_versioned_schema_and_is_idempotent() -> None:
    work_dir = Path("work")
    work_dir.mkdir(exist_ok=True)
    database_path = work_dir / f"migration-test-{uuid4().hex}.db"
    database_url = f"sqlite:///{database_path.as_posix()}"

    upgrade_database(database_url=database_url)
    upgrade_database(database_url=database_url)

    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        job_columns = {column["name"] for column in inspector.get_columns("job_postings")}
        with engine.connect() as connection:
            revision = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
    finally:
        engine.dispose()
        database_path.unlink(missing_ok=True)

    assert {
        "alembic_version",
        "sources",
        "job_postings",
        "job_snapshots",
        "job_summaries",
        "application_drafts",
        "candidate_profiles",
        "candidate_addresses",
        "preference_profiles",
        "job_scores",
        "feedback_events",
        "application_records",
        "crawl_runs",
        "app_settings",
    } <= tables
    assert {
        "availability_status",
        "availability_checked_at",
        "availability_check_method",
        "availability_reason",
        "availability_failures",
    } <= job_columns
    assert revision == "a9d4e7c2b615"


def test_statistics_source_is_removed_from_existing_databases() -> None:
    work_dir = Path("work")
    work_dir.mkdir(exist_ok=True)
    database_path = work_dir / f"statistics-removal-{uuid4().hex}.db"
    database_url = f"sqlite:///{database_path.resolve().as_posix()}"

    try:
        upgrade_database("e7a4c2d9b610", database_url=database_url)
        engine = create_engine(database_url)
        try:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        INSERT INTO sources (
                            name, slug, kind, enabled, is_official, free_api, status,
                            metadata, created_at, updated_at
                        )
                        VALUES (
                            'BA Statistics', 'ba_statistics_jobs', 'statistics', 0, 1, 1,
                            'prepared', '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                        )
                        """
                    )
                )
        finally:
            engine.dispose()

        upgrade_database(database_url=database_url)

        engine = create_engine(database_url)
        try:
            with engine.connect() as connection:
                source_count = connection.execute(
                    text("SELECT count(*) FROM sources WHERE slug = 'ba_statistics_jobs'")
                ).scalar_one()
        finally:
            engine.dispose()
    finally:
        database_path.unlink(missing_ok=True)

    assert source_count == 0


def test_candidate_address_migration_backfills_the_legacy_home_location() -> None:
    work_dir = Path("work")
    work_dir.mkdir(exist_ok=True)
    database_path = work_dir / f"migration-backfill-{uuid4().hex}.db"
    database_url = f"sqlite:///{database_path.resolve().as_posix()}"

    try:
        upgrade_database("b404659b8ee6", database_url=database_url)
        engine = create_engine(database_url)
        try:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        INSERT INTO candidate_profiles (
                            full_name, home_location, home_latitude, home_longitude,
                            structured_data, is_confirmed, version, created_at, updated_at
                        ) VALUES (
                            :full_name, :home_location, :home_latitude, :home_longitude,
                            :structured_data, :is_confirmed, :version, :created_at, :updated_at
                        )
                        """
                    ),
                    {
                        "full_name": "Ada Beispiel",
                        "home_location": "  Stuttgart  ",
                        "home_latitude": 48.7758,
                        "home_longitude": 9.1829,
                        "structured_data": "{}",
                        "is_confirmed": False,
                        "version": 1,
                        "created_at": "2026-07-17 10:00:00",
                        "updated_at": "2026-07-17 10:00:00",
                    },
                )
        finally:
            engine.dispose()

        upgrade_database(database_url=database_url)
        engine = create_engine(database_url)
        try:
            with engine.connect() as connection:
                address = connection.execute(
                    text(
                        """
                        SELECT address_text, position, latitude, longitude
                        FROM candidate_addresses
                        """
                    )
                ).mappings().one()
                revision = connection.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalar_one()
        finally:
            engine.dispose()

        assert dict(address) == {
            "address_text": "Stuttgart",
            "position": 0,
            "latitude": 48.7758,
            "longitude": 9.1829,
        }
        assert revision == "a9d4e7c2b615"
    finally:
        database_path.unlink(missing_ok=True)


def test_role_taxonomy_migration_preserves_custom_role_terms() -> None:
    work_dir = Path("work")
    work_dir.mkdir(exist_ok=True)
    database_path = work_dir / f"role-taxonomy-migration-{uuid4().hex}.db"
    database_url = f"sqlite:///{database_path.resolve().as_posix()}"

    try:
        upgrade_database("f4a8b2c6d901", database_url=database_url)
        engine = create_engine(database_url)
        try:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        INSERT INTO preference_profiles (
                            candidate_profile_id, employment_types, contract_types,
                            preferred_states, include_germany_remote, commute_penalty_minutes,
                            max_commute_minutes, onsite_weight, hybrid_weight, remote_weight,
                            min_salary, target_salary, languages, role_terms, extra_preferences,
                            version, created_at, updated_at
                        ) VALUES (
                            NULL, :employment_types, :contract_types,
                            :preferred_states, 1, 60,
                            75, 1.0, 0.55, 0.2,
                            NULL, NULL, :languages, :role_terms, :extra_preferences,
                            1, :created_at, :updated_at
                        )
                        """
                    ),
                    {
                        "employment_types": '["Vollzeit"]',
                        "contract_types": '["unbefristet"]',
                        "preferred_states": '["Baden-Württemberg"]',
                        "languages": '["de", "en"]',
                        "role_terms": json.dumps(
                            [
                                "Chief of Staff",
                                "Custom Executive Role",
                                "Assistenz der Geschäftsführung",
                            ]
                        ),
                        "extra_preferences": "{}",
                        "created_at": "2026-07-22 10:00:00",
                        "updated_at": "2026-07-22 10:00:00",
                    },
                )
        finally:
            engine.dispose()

        upgrade_database(database_url=database_url)
        engine = create_engine(database_url)
        try:
            with engine.connect() as connection:
                row = connection.execute(
                    text(
                        """
                        SELECT primary_role_terms, additional_role_terms
                        FROM preference_profiles
                        """
                    )
                ).mappings().one()
                rescore_request = connection.execute(
                    text(
                        """
                        SELECT value FROM app_settings
                        WHERE key = 'scoring.rescore_request'
                        """
                    )
                ).scalar_one()
            columns = {
                column["name"]
                for column in inspect(engine).get_columns("preference_profiles")
            }
        finally:
            engine.dispose()

        assert json.loads(row["primary_role_terms"]) == [
            "Assistenz der Geschäftsführung",
            "Executive Assistant",
        ]
        additional = json.loads(row["additional_role_terms"])
        assert "Chief of Staff" in additional
        assert "Custom Executive Role" in additional
        assert "Assistenz der Geschäftsführung" not in additional
        assert "role_terms" not in columns
        assert json.loads(rescore_request)["reason"] == "role_taxonomy_migrated"
        assert json.loads(rescore_request)["status"] == "pending"
    finally:
        database_path.unlink(missing_ok=True)
