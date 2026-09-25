"""Persistent domain model for GF-Jobradar."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.ext.mutable import MutableDict, MutableList
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base
from .domain.role_terms import (
    DEFAULT_ADDITIONAL_ROLE_TERMS,
    DEFAULT_PRIMARY_ROLE_TERMS,
)


def utcnow() -> datetime:
    return datetime.now(UTC)


class ApplicationStatus(StrEnum):
    NEW = "Neu"
    SAVED = "Merkliste"
    NOT_SUITABLE = "Nicht passend"
    PLANNED = "Bewerbung geplant"
    APPLIED = "Beworben"
    INTERVIEW = "Gespräch"
    OFFER = "Angebot"
    CLOSED = "Abgeschlossen"


class ApplicationDraftStatus(StrEnum):
    REVIEWED = "reviewed"
    NEEDS_REVISION = "needs_revision"
    EDITED = "edited"


class CrawlRunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    BLOCKED = "blocked"
class JobAvailabilityStatus(StrEnum):
    UNVERIFIED = "unverified"
    ACTIVE = "active"
    INACTIVE = "inactive"
    CHECK_FAILED = "check_failed"




class Source(Base):
    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(40), default="api", nullable=False)
    base_url: Mapped[str | None] = mapped_column(String(1000))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_official: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    free_api: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="unknown", nullable=False)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata", MutableDict.as_mutable(JSON), default=dict, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    jobs: Mapped[list[JobPosting]] = relationship(back_populates="source")
    crawl_runs: Mapped[list[CrawlRun]] = relationship(back_populates="source")


class JobPosting(Base):
    __tablename__ = "job_postings"
    __table_args__ = (
        UniqueConstraint("source_id", "external_id", name="uq_job_source_external"),
        Index("ix_jobs_active_published", "is_active", "published_at"),
        Index("ix_jobs_canonical_key", "canonical_key"),
        Index("ix_jobs_state_city", "state", "city"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(
        ForeignKey("sources.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    external_id: Mapped[str] = mapped_column(String(300), nullable=False)
    canonical_url: Mapped[str | None] = mapped_column(String(2000))
    canonical_key: Mapped[str | None] = mapped_column(String(128))
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    employer: Mapped[str | None] = mapped_column(String(500), index=True)
    description_text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    location_text: Mapped[str | None] = mapped_column(String(1000))
    postcode: Mapped[str | None] = mapped_column(String(20))
    city: Mapped[str | None] = mapped_column(String(200))
    state: Mapped[str | None] = mapped_column(String(200), index=True)
    country: Mapped[str] = mapped_column(String(2), default="DE", nullable=False)
    remote_type: Mapped[str | None] = mapped_column(String(40), index=True)
    employment_type: Mapped[str | None] = mapped_column(String(100), index=True)
    contract_type: Mapped[str | None] = mapped_column(String(100), index=True)
    language: Mapped[str | None] = mapped_column(String(20))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    availability_status: Mapped[str] = mapped_column(
        String(30), default=JobAvailabilityStatus.UNVERIFIED.value, nullable=False, index=True
    )
    availability_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    availability_check_method: Mapped[str | None] = mapped_column(String(50))
    availability_reason: Mapped[str | None] = mapped_column(String(500))
    availability_failures: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    content_hash: Mapped[str | None] = mapped_column(String(128), index=True)
    structured_data: Mapped[dict[str, Any]] = mapped_column(
        MutableDict.as_mutable(JSON), default=dict, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    source: Mapped[Source] = relationship(back_populates="jobs")
    snapshots: Mapped[list[JobSnapshot]] = relationship(
        back_populates="job", cascade="all, delete-orphan", order_by="JobSnapshot.fetched_at"
    )
    ai_summary: Mapped[JobSummary | None] = relationship(
        back_populates="job", cascade="all, delete-orphan", uselist=False
    )
    scores: Mapped[list[JobScore]] = relationship(
        back_populates="job", cascade="all, delete-orphan", order_by="JobScore.created_at"
    )
    feedback_events: Mapped[list[FeedbackEvent]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )
    application: Mapped[ApplicationRecord | None] = relationship(
        back_populates="job", cascade="all, delete-orphan", uselist=False
    )
    application_drafts: Mapped[list[ApplicationDraft]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
        order_by="ApplicationDraft.revision",
    )

    @property
    def latest_score(self) -> JobScore | None:
        return self.scores[-1] if self.scores else None

    @property
    def application_status(self) -> str:
        return self.application.status if self.application else ApplicationStatus.NEW.value


class JobSnapshot(Base):
    __tablename__ = "job_snapshots"
    __table_args__ = (UniqueConstraint("job_id", "content_hash", name="uq_snapshot_job_hash"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(
        ForeignKey("job_postings.id", ondelete="CASCADE"), nullable=False, index=True
    )
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    content_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    cleaned_text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    raw_html_compressed: Mapped[bytes | None] = mapped_column(LargeBinary)
    structured_data: Mapped[dict[str, Any]] = mapped_column(
        MutableDict.as_mutable(JSON), default=dict, nullable=False
    )
    change_kind: Mapped[str] = mapped_column(String(40), default="created", nullable=False)

    job: Mapped[JobPosting] = relationship(back_populates="snapshots")


class JobSummary(Base):
    __tablename__ = "job_summaries"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(
        ForeignKey("job_postings.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
        index=True,
    )
    content_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    profile_version: Mapped[int | None] = mapped_column(Integer)
    preference_version: Mapped[int | None] = mapped_column(Integer)
    locale: Mapped[str] = mapped_column(String(10), nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    reasoning_effort: Mapped[str] = mapped_column(String(20), nullable=False)
    overview: Mapped[str] = mapped_column(Text, nullable=False)
    key_points: Mapped[list[str]] = mapped_column(
        MutableList.as_mutable(JSON), default=list, nullable=False
    )
    missing_information: Mapped[list[str]] = mapped_column(
        MutableList.as_mutable(JSON), default=list, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    job: Mapped[JobPosting] = relationship(back_populates="ai_summary")


class CandidateProfile(Base):
    __tablename__ = "candidate_profiles"

    id: Mapped[int] = mapped_column(primary_key=True)
    full_name: Mapped[str | None] = mapped_column(String(300))
    email: Mapped[str | None] = mapped_column(String(320))
    phone: Mapped[str | None] = mapped_column(String(100))
    home_location: Mapped[str | None] = mapped_column(String(500))
    home_latitude: Mapped[float | None] = mapped_column(Float)
    home_longitude: Mapped[float | None] = mapped_column(Float)
    cv_filename: Mapped[str | None] = mapped_column(String(500))
    cv_text: Mapped[str | None] = mapped_column(Text)
    structured_data: Mapped[dict[str, Any]] = mapped_column(
        MutableDict.as_mutable(JSON), default=dict, nullable=False
    )
    is_confirmed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    preferences: Mapped[PreferenceProfile | None] = relationship(
        back_populates="candidate", cascade="all, delete-orphan", uselist=False
    )
    addresses: Mapped[list[CandidateAddress]] = relationship(
        back_populates="candidate",
        cascade="all, delete-orphan",
        order_by="CandidateAddress.position",
    )
    scores: Mapped[list[JobScore]] = relationship(back_populates="candidate")

    @property
    def address_values(self) -> list[str]:
        """Return ordered commute origins with a legacy-field fallback."""

        if self.addresses:
            return [address.address_text for address in self.addresses]
        if self.home_location and self.home_location.strip():
            return [self.home_location.strip()]
        return []


class CandidateAddress(Base):
    __tablename__ = "candidate_addresses"
    __table_args__ = (
        UniqueConstraint(
            "candidate_profile_id",
            "address_text",
            name="uq_candidate_address_text",
        ),
        Index(
            "ix_candidate_addresses_candidate_position",
            "candidate_profile_id",
            "position",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    candidate_profile_id: Mapped[int] = mapped_column(
        ForeignKey("candidate_profiles.id", ondelete="CASCADE"), nullable=False
    )
    address_text: Mapped[str] = mapped_column(String(500), nullable=False)
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    candidate: Mapped[CandidateProfile] = relationship(back_populates="addresses")


class PreferenceProfile(Base):
    __tablename__ = "preference_profiles"

    id: Mapped[int] = mapped_column(primary_key=True)
    candidate_profile_id: Mapped[int | None] = mapped_column(
        ForeignKey("candidate_profiles.id", ondelete="CASCADE"),
        unique=True,
        index=True,
    )
    employment_types: Mapped[list[str]] = mapped_column(
        MutableList.as_mutable(JSON), default=lambda: ["Vollzeit"], nullable=False
    )
    contract_types: Mapped[list[str]] = mapped_column(
        MutableList.as_mutable(JSON),
        default=lambda: ["unbefristet", "Direktanstellung"],
        nullable=False,
    )
    preferred_states: Mapped[list[str]] = mapped_column(
        MutableList.as_mutable(JSON),
        default=lambda: ["Baden-Württemberg"],
        nullable=False,
    )
    include_germany_remote: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    commute_penalty_minutes: Mapped[int] = mapped_column(Integer, default=60, nullable=False)
    max_commute_minutes: Mapped[int] = mapped_column(Integer, default=75, nullable=False)
    onsite_weight: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    hybrid_weight: Mapped[float] = mapped_column(Float, default=0.55, nullable=False)
    remote_weight: Mapped[float] = mapped_column(Float, default=0.2, nullable=False)
    min_salary: Mapped[int | None] = mapped_column(Integer)
    target_salary: Mapped[int | None] = mapped_column(Integer)
    languages: Mapped[list[str]] = mapped_column(
        MutableList.as_mutable(JSON), default=lambda: ["de", "en"], nullable=False
    )
    primary_role_terms: Mapped[list[str]] = mapped_column(
        MutableList.as_mutable(JSON),
        default=lambda: list(DEFAULT_PRIMARY_ROLE_TERMS),
        nullable=False,
    )
    additional_role_terms: Mapped[list[str]] = mapped_column(
        MutableList.as_mutable(JSON),
        default=lambda: list(DEFAULT_ADDITIONAL_ROLE_TERMS),
        nullable=False,
    )
    extra_preferences: Mapped[dict[str, Any]] = mapped_column(
        MutableDict.as_mutable(JSON), default=dict, nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    candidate: Mapped[CandidateProfile | None] = relationship(back_populates="preferences")


class JobScore(Base):
    __tablename__ = "job_scores"
    __table_args__ = (
        Index("ix_scores_job_created", "job_id", "created_at"),
        Index("ix_scores_relevance_fit", "relevance_score", "candidate_fit_score"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(
        ForeignKey("job_postings.id", ondelete="CASCADE"), nullable=False, index=True
    )
    candidate_profile_id: Mapped[int | None] = mapped_column(
        ForeignKey("candidate_profiles.id", ondelete="SET NULL"), index=True
    )
    profile_version: Mapped[int | None] = mapped_column(Integer)
    preference_version: Mapped[int | None] = mapped_column(Integer)
    relevance_score: Mapped[float] = mapped_column(Float, nullable=False)
    candidate_fit_score: Mapped[float | None] = mapped_column(Float)
    relevance_reasons: Mapped[list[dict[str, Any]]] = mapped_column(
        MutableList.as_mutable(JSON), default=list, nullable=False
    )
    fit_reasons: Mapped[list[dict[str, Any]]] = mapped_column(
        MutableList.as_mutable(JSON), default=list, nullable=False
    )
    scoring_version: Mapped[str] = mapped_column(String(50), default="local-v1", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    job: Mapped[JobPosting] = relationship(back_populates="scores")
    candidate: Mapped[CandidateProfile | None] = relationship(back_populates="scores")


class FeedbackEvent(Base):
    __tablename__ = "feedback_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(
        ForeignKey("job_postings.id", ondelete="CASCADE"), nullable=False, index=True
    )
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    value: Mapped[str | None] = mapped_column(String(500))
    weight_delta: Mapped[dict[str, Any]] = mapped_column(
        MutableDict.as_mutable(JSON), default=dict, nullable=False
    )
    explanation: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    job: Mapped[JobPosting] = relationship(back_populates="feedback_events")


class ApplicationDraft(Base):
    __tablename__ = "application_drafts"
    __table_args__ = (
        UniqueConstraint("job_id", "revision", name="uq_application_draft_job_revision"),
        Index("ix_application_drafts_job_updated", "job_id", "updated_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(
        ForeignKey("job_postings.id", ondelete="CASCADE"), nullable=False, index=True
    )
    profile_version: Mapped[int] = mapped_column(Integer, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(40), default=ApplicationDraftStatus.NEEDS_REVISION.value, nullable=False, index=True
    )
    language: Mapped[str] = mapped_column(String(10), default="de", nullable=False)
    cv_draft: Mapped[str] = mapped_column(Text, default="", nullable=False)
    cover_letter: Mapped[str] = mapped_column(Text, default="", nullable=False)
    evidence_map: Mapped[list[dict[str, Any]]] = mapped_column(
        MutableList.as_mutable(JSON), default=list, nullable=False
    )
    questions: Mapped[list[str]] = mapped_column(
        MutableList.as_mutable(JSON), default=list, nullable=False
    )
    review: Mapped[dict[str, Any]] = mapped_column(
        MutableDict.as_mutable(JSON), default=dict, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    job: Mapped[JobPosting] = relationship(back_populates="application_drafts")


class ApplicationRecord(Base):
    __tablename__ = "application_records"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(
        ForeignKey("job_postings.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
        index=True,
    )
    status: Mapped[str] = mapped_column(
        String(80), default=ApplicationStatus.NEW.value, nullable=False, index=True
    )
    notes: Mapped[str | None] = mapped_column(Text)
    documents: Mapped[list[dict[str, Any]]] = mapped_column(
        MutableList.as_mutable(JSON), default=list, nullable=False
    )
    planned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    interview_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    offer_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    job: Mapped[JobPosting] = relationship(back_populates="application")


class CrawlRun(Base):
    __tablename__ = "crawl_runs"
    __table_args__ = (Index("ix_crawl_runs_status_created", "status", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int | None] = mapped_column(
        ForeignKey("sources.id", ondelete="SET NULL"), index=True
    )
    run_type: Mapped[str] = mapped_column(String(50), default="scheduled", nullable=False)
    status: Mapped[str] = mapped_column(
        String(40), default=CrawlRunStatus.QUEUED.value, nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    discovered_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    fetched_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    updated_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    skipped_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict[str, Any]] = mapped_column(
        MutableDict.as_mutable(JSON), default=dict, nullable=False
    )
    lock_reason: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    source: Mapped[Source | None] = relationship(back_populates="crawl_runs")


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    value_json: Mapped[Any] = mapped_column("value", JSON, nullable=False)
    description: Mapped[str | None] = mapped_column(String(1000))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )
