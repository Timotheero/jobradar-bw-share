"""FastAPI routes for the backend core.

There is intentionally no endpoint that starts crawling.  The first deployment
can be inspected, configured, and connected to OAuth while the global source
access lock remains closed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from .config import Settings
from .config import get_settings as get_runtime_settings
from .db import get_db
from .i18n import locale_from_request, translate_text
from .models import ApplicationStatus, CrawlRun, CrawlRunStatus, JobPosting, Source
from .schemas import (
    ApplicationRead,
    CandidateProfileRead,
    CandidateProfileUpdate,
    CrawlRunRead,
    HealthResponse,
    JobDetail,
    JobListItem,
    JobListResponse,
    JobStatusUpdate,
    PreferenceProfileRead,
    PreferenceProfileUpdate,
    SettingEntry,
    SettingsRead,
    SettingsUpdate,
    SourceActivity,
    SourceRead,
    SourceUpdate,
)
from .services import jobs as job_service
from .services import profile as profile_service
from .services import settings as setting_service
from .services.availability import HTTPJobAvailabilityChecker, verify_jobs_for_application
from .services.crawl_reporting import (
    firecrawl_runtime_status,
    firecrawl_target_reports,
)
from .services.sync import source_environment_enabled

router = APIRouter()
DatabaseSession = Annotated[Session, Depends(get_db)]


def _as_utc(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _source_active(source_enabled: bool, source_slug: str, settings: Settings) -> bool:
    return source_enabled and source_environment_enabled(source_slug, settings)


@router.get("/health", response_model=HealthResponse, tags=["system"])
def health(session: DatabaseSession) -> HealthResponse:
    added_since = datetime.now(UTC) - timedelta(days=1)
    try:
        session.execute(text("SELECT 1"))
        effective_api_access = setting_service.is_crawling_enabled(session)
        effective_firecrawl_access = setting_service.is_firecrawl_enabled(session)
        source_rows = session.execute(
            select(
                Source.id,
                Source.slug,
                Source.kind,
                Source.enabled,
                Source.last_success_at,
                func.count(JobPosting.id),
                func.count(JobPosting.id).filter(JobPosting.created_at >= added_since),
            )
            .outerjoin(JobPosting, JobPosting.source_id == Source.id)
            .where(Source.kind.in_(("api", "statistics", "crawler")))
            .group_by(
                Source.id,
                Source.slug,
                Source.kind,
                Source.enabled,
                Source.last_success_at,
            )
        ).all()
        running_source_ids = set(
            session.scalars(
                select(CrawlRun.source_id).where(
                    CrawlRun.status == CrawlRunStatus.RUNNING.value,
                    CrawlRun.source_id.is_not(None),
                )
            )
        )
        crawler_sources = session.scalars(
            select(Source).where(Source.kind == "crawler").order_by(Source.id)
        ).all()
        crawler_reports = tuple(
            report
            for source in crawler_sources
            for report in firecrawl_target_reports(session, source)
        )
        crawler_runtime_statuses = tuple(
            firecrawl_runtime_status(
                session,
                source,
                enabled=effective_firecrawl_access,
            )
            for source in crawler_sources
        )
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database unavailable",
        ) from exc

    runtime_settings = get_runtime_settings()
    api_active = False
    api_job_count = 0
    api_added_last_day = 0
    api_running = False
    api_last_fetched_at = None
    crawler_state = max(
        (runtime.state for runtime in crawler_runtime_statuses),
        key={"paused": 0, "pending": 1, "ready": 2, "limited": 3, "error": 4, "running": 5}.get,
        default="paused",
    )
    for (
        source_id,
        source_slug,
        source_kind,
        source_enabled,
        last_success_at,
        job_count,
        added_last_day,
    ) in source_rows:
        fetched_at = _as_utc(last_success_at)
        if source_kind == "crawler":
            continue
        active = effective_api_access and _source_active(
            source_enabled, source_slug, runtime_settings
        )
        api_active = api_active or active
        api_running = api_running or source_id in running_source_ids
        api_job_count += job_count
        api_added_last_day += added_last_day
        if fetched_at is not None and (
            api_last_fetched_at is None or fetched_at > api_last_fetched_at
        ):
            api_last_fetched_at = fetched_at

    def crawler_activity(target_kind: str) -> SourceActivity:
        reports = tuple(
            report for report in crawler_reports if report.target_kind == target_kind
        )
        state = crawler_state if reports else "paused"
        crawled_values = [
            _as_utc(report.last_crawled_at)
            for report in reports
            if report.last_crawled_at is not None
        ]
        return SourceActivity(
            active=state in {"ready", "running"},
            running=state == "running",
            state=state,
            job_count=sum(report.stored_job_count for report in reports),
            added_last_day=sum(report.added_last_day for report in reports),
            last_fetched_at=max(crawled_values, default=None),
        )

    return HealthResponse(
        api_access_enabled=effective_api_access,
        firecrawl_enabled=effective_firecrawl_access,
        api_calls=SourceActivity(
            active=api_active,
            running=api_running,
            state="running" if api_running else "ready" if api_active else "paused",
            job_count=api_job_count,
            added_last_day=api_added_last_day,
            last_fetched_at=api_last_fetched_at,
        ),
        job_portal_crawling=crawler_activity("job_portal"),
        company_site_crawling=crawler_activity("company_site"),
    )


def _job_list_item(item: job_service.JobSearchItem) -> JobListItem:
    job, score = item.job, item.score
    return JobListItem(
        id=job.id,
        title=job.title,
        employer=job.employer,
        location_text=job.location_text,
        city=job.city,
        state=job.state,
        remote_type=job.remote_type,
        employment_type=job.employment_type,
        contract_type=job.contract_type,
        published_at=job.published_at,
        last_seen_at=job.last_seen_at,
        is_active=job.is_active,
        availability_status=job.availability_status,
        availability_checked_at=job.availability_checked_at,
        availability_reason=job.availability_reason,
        source=SourceRead.model_validate(job.source),
        relevance_score=score.relevance_score if score else None,
        candidate_fit_score=score.candidate_fit_score if score else None,
        application_status=job.application_status,
    )


@router.get("/jobs", response_model=JobListResponse, tags=["jobs"])
def jobs_list(
    session: DatabaseSession,
    q: str | None = Query(default=None, max_length=500),
    active_only: bool = True,
    all_jobs: bool = False,
    source_id: int | None = None,
    employment_type: str | None = None,
    remote_type: str | None = None,
    state_name: str | None = Query(default=None, alias="state"),
    application_status: str | None = Query(default=None, alias="status"),
    min_relevance: float | None = Query(default=None, ge=0, le=100),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> JobListResponse:
    # The normal view is curated; all_jobs=true exposes the complete inventory.
    effective_minimum = min_relevance
    if effective_minimum is None and not all_jobs:
        effective_minimum = float(
            setting_service.get_setting(session, "scoring.main_view_minimum", 30)
        )
    result = job_service.list_jobs(
        session,
        query=q,
        active_only=active_only,
        source_id=source_id,
        employment_type=employment_type,
        remote_type=remote_type,
        state=state_name,
        status=application_status,
        min_relevance=effective_minimum,
        limit=limit,
        offset=offset,
    )
    return JobListResponse(
        items=[_job_list_item(item) for item in result.items],
        total=result.total,
        limit=result.limit,
        offset=result.offset,
    )


@router.get("/jobs/{job_id}", response_model=JobDetail, tags=["jobs"])
def job_detail(job_id: int, session: DatabaseSession) -> JobDetail:
    job = job_service.get_job(session, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return JobDetail.model_validate(job, from_attributes=True)


@router.patch("/jobs/{job_id}/status", response_model=ApplicationRead, tags=["jobs"])
def job_status(
    job_id: int,
    payload: JobStatusUpdate,
    request: Request,
    session: DatabaseSession,
) -> ApplicationRead:
    if payload.status is ApplicationStatus.PLANNED:
        job = session.get(JobPosting, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
        checker = getattr(request.app.state, "availability_checker", None)
        owned_checker = checker is None
        checker = checker or HTTPJobAvailabilityChecker()
        try:
            availability = verify_jobs_for_application(
                session,
                (job,),
                checker,
                get_runtime_settings(),
                allow_unverified=payload.confirm_unverified,
            )
        finally:
            if owned_checker:
                checker.close()
        if availability.inactive:
            session.commit()
            raise HTTPException(status_code=409, detail="Job is no longer available")
        if availability.unverified and not payload.confirm_unverified:
            session.commit()
            raise HTTPException(
                status_code=409,
                detail="Job availability could not be confirmed; explicit confirmation is required",
            )
    try:
        record = job_service.update_application_status(
            session, job_id, payload.status, notes=payload.notes
        )
    except job_service.JobNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Job not found") from exc
    return ApplicationRead.model_validate(record)


@router.get("/profile", response_model=CandidateProfileRead, tags=["profile"])
def profile_get(session: DatabaseSession) -> CandidateProfileRead:
    profile = profile_service.get_or_create_candidate_profile(session)
    return CandidateProfileRead.model_validate(profile)


@router.put("/profile", response_model=CandidateProfileRead, tags=["profile"])
def profile_put(payload: CandidateProfileUpdate, session: DatabaseSession) -> CandidateProfileRead:
    profile = profile_service.update_candidate_profile(session, payload.model_dump(mode="python"))
    return CandidateProfileRead.model_validate(profile)


@router.get("/preferences", response_model=PreferenceProfileRead, tags=["profile"])
def preferences_get(session: DatabaseSession) -> PreferenceProfileRead:
    preferences = profile_service.get_or_create_preference_profile(session)
    return PreferenceProfileRead.model_validate(preferences)


@router.put("/preferences", response_model=PreferenceProfileRead, tags=["profile"])
def preferences_put(
    payload: PreferenceProfileUpdate, session: DatabaseSession
) -> PreferenceProfileRead:
    preferences = profile_service.update_preference_profile(
        session, payload.model_dump(mode="python")
    )
    return PreferenceProfileRead.model_validate(preferences)


@router.get("/sources", response_model=list[SourceRead], tags=["sources"])
def sources_list(session: DatabaseSession) -> list[SourceRead]:
    rows = session.scalars(select(Source).order_by(Source.name)).all()
    return [SourceRead.model_validate(row) for row in rows]


@router.patch("/sources/{source_id}", response_model=SourceRead, tags=["sources"])
def source_update(source_id: int, payload: SourceUpdate, session: DatabaseSession) -> SourceRead:
    source = session.get(Source, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    if payload.enabled is not None:
        source.enabled = payload.enabled
    session.commit()
    session.refresh(source)
    return SourceRead.model_validate(source)


def _settings_response(session: Session) -> SettingsRead:
    rows = setting_service.list_settings(session)
    entries = [
        SettingEntry(
            key=row.key,
            value=row.value_json,
            description=row.description,
            source="database",
        )
        for row in rows
    ]
    entries.append(
        SettingEntry(
            key="deployment.source_access_allowed",
            value=get_runtime_settings().crawling_enabled,
            description="Read-only deployment lock from CRAWLING_ENABLED.",
            source="environment",
        )
    )
    entries.append(
        SettingEntry(
            key="deployment.firecrawl_allowed",
            value=get_runtime_settings().firecrawl_enabled,
            description="Read-only deployment lock from FIRECRAWL_ENABLED.",
            source="environment",
        )
    )
    return SettingsRead(
        api_access_enabled=setting_service.is_crawling_enabled(session),
        firecrawl_enabled=setting_service.is_firecrawl_enabled(session),
        entries=entries,
    )


@router.get("/settings", response_model=SettingsRead, tags=["settings"])
def settings_get(session: DatabaseSession) -> SettingsRead:
    return _settings_response(session)


@router.put("/settings", response_model=SettingsRead, tags=["settings"])
def settings_put(
    payload: SettingsUpdate, request: Request, session: DatabaseSession
) -> SettingsRead:
    if payload.api_access_enabled is not None:
        try:
            setting_service.set_crawling_enabled(session, payload.api_access_enabled)
        except setting_service.CrawlingLockedError as exc:
            raise HTTPException(
                status_code=409,
                detail=translate_text(str(exc), locale_from_request(request)),
            ) from exc
    if payload.firecrawl_enabled is not None:
        try:
            setting_service.set_firecrawl_enabled(session, payload.firecrawl_enabled)
        except setting_service.CrawlingLockedError as exc:
            raise HTTPException(
                status_code=409,
                detail=translate_text(str(exc), locale_from_request(request)),
            ) from exc
    for key, value in payload.values.items():
        if key.startswith("system.") or key.startswith("deployment."):
            raise HTTPException(
                status_code=422,
                detail=f"Reserved setting key: {key}",
            )
        setting_service.set_setting(session, key, value)
    return _settings_response(session)


@router.get("/crawl-runs", response_model=list[CrawlRunRead], tags=["sources"])
def crawl_runs_list(
    session: DatabaseSession,
    source_id: int | None = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> list[CrawlRunRead]:
    statement = select(CrawlRun).order_by(CrawlRun.created_at.desc()).limit(limit)
    if source_id is not None:
        statement = statement.where(CrawlRun.source_id == source_id)
    rows = session.scalars(statement).all()
    return [CrawlRunRead.model_validate(row) for row in rows]
