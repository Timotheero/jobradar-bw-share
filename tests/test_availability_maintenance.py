from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session, sessionmaker

from jobradar.config import Settings
from jobradar.db import Base, build_engine
from jobradar.models import (
    ApplicationRecord,
    ApplicationStatus,
    JobAvailabilityStatus,
    JobPosting,
    JobSnapshot,
    Source,
)
from jobradar.services.availability import AvailabilityCheck
from jobradar.services.maintenance import run_job_maintenance
from jobradar.services.settings import CRAWLING_SETTING_KEY, set_setting


@pytest.fixture
def session() -> Generator[Session, None, None]:
    engine = build_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as database_session:
        yield database_session
    engine.dispose()


class FakeChecker:
    def __init__(self, results: dict[int, AvailabilityCheck]) -> None:
        self.results = results
        self.checked: list[int] = []

    def check(self, job: JobPosting) -> AvailabilityCheck:
        self.checked.append(job.id)
        return self.results[job.id]

    def close(self) -> None:
        pass


def _job(
    source: Source,
    external_id: str,
    *,
    now: datetime,
    active: bool = True,
) -> JobPosting:
    return JobPosting(
        source_id=source.id,
        external_id=external_id,
        canonical_url=f"https://jobs.example.org/{external_id}",
        title=f"Executive Assistant {external_id}",
        description_text="Unterstützung der Geschäftsführung",
        last_seen_at=now - timedelta(days=31),
        is_active=active,
    )


def test_maintenance_prioritizes_saved_jobs_and_clears_only_old_raw_html(
    session: Session,
) -> None:
    now = datetime(2026, 7, 22, 12, tzinfo=UTC)
    source = Source(name="Test", slug="test-api", kind="api", enabled=True)
    session.add(source)
    session.flush()
    saved = _job(source, "saved", now=now)
    ordinary = _job(source, "ordinary", now=now)
    session.add_all([saved, ordinary])
    session.flush()
    session.add(
        ApplicationRecord(job_id=saved.id, status=ApplicationStatus.SAVED.value, documents=[])
    )
    old_snapshot = JobSnapshot(
        job_id=saved.id,
        fetched_at=now - timedelta(days=91),
        content_hash="old",
        cleaned_text="kept",
        raw_html_compressed=b"old raw html",
        structured_data={"kept": True},
    )
    recent_snapshot = JobSnapshot(
        job_id=ordinary.id,
        fetched_at=now - timedelta(days=89),
        content_hash="recent",
        cleaned_text="kept",
        raw_html_compressed=b"recent raw html",
        structured_data={"kept": True},
    )
    session.add_all([old_snapshot, recent_snapshot])
    set_setting(session, CRAWLING_SETTING_KEY, True)
    session.commit()

    checker = FakeChecker(
        {
            saved.id: AvailabilityCheck(
                JobAvailabilityStatus.INACTIVE,
                "test",
                "confirmed_closed",
            ),
            ordinary.id: AvailabilityCheck(
                JobAvailabilityStatus.ACTIVE,
                "test",
                "confirmed_active",
            ),
        }
    )
    result = run_job_maintenance(
        session,
        Settings(
            crawling_enabled=True,
            job_inactive_after_days=30,
            raw_html_retention_days=90,
        ),
        checker,
        now=now,
        batch_size=1,
    )

    assert checker.checked == [saved.id]
    assert result.jobs_checked == 1
    assert result.jobs_inactivated == 1
    assert result.raw_snapshots_cleaned == 1
    assert saved.is_active is False
    assert saved.availability_status == JobAvailabilityStatus.INACTIVE.value
    assert old_snapshot.raw_html_compressed is None
    assert old_snapshot.cleaned_text == "kept"
    assert old_snapshot.structured_data == {"kept": True}
    assert recent_snapshot.raw_html_compressed == b"recent raw html"


def test_failed_check_does_not_deactivate_stale_job(session: Session) -> None:
    now = datetime(2026, 7, 22, 12, tzinfo=UTC)
    source = Source(name="Test", slug="test-api", kind="api", enabled=True)
    session.add(source)
    session.flush()
    job = _job(source, "temporary-failure", now=now)
    session.add(job)
    set_setting(session, CRAWLING_SETTING_KEY, True)
    session.commit()

    result = run_job_maintenance(
        session,
        Settings(crawling_enabled=True),
        FakeChecker(
            {
                job.id: AvailabilityCheck(
                    JobAvailabilityStatus.CHECK_FAILED,
                    "test",
                    "temporary_failure",
                )
            }
        ),
        now=now,
    )

    assert result.checks_failed == 1
    assert job.is_active is True
    assert job.availability_status == JobAvailabilityStatus.CHECK_FAILED.value
    assert job.availability_failures == 1
