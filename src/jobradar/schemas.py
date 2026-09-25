"""Validated API contracts.

The contracts expose the two scores separately and retain each scoring reason;
clients never need to reverse-engineer a combined or opaque ranking value.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .domain.role_terms import DEFAULT_ADDITIONAL_ROLE_TERMS, DEFAULT_PRIMARY_ROLE_TERMS
from .models import ApplicationStatus


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class SourceActivity(BaseModel):
    active: bool
    running: bool
    state: str
    job_count: int
    added_last_day: int
    last_fetched_at: datetime | None


class HealthResponse(BaseModel):
    status: str = "ok"
    database: str = "ok"
    api_access_enabled: bool
    firecrawl_enabled: bool
    api_calls: SourceActivity
    job_portal_crawling: SourceActivity
    company_site_crawling: SourceActivity


class SourceRead(ORMModel):
    id: int
    name: str
    slug: str
    kind: str
    base_url: str | None
    enabled: bool
    is_official: bool
    free_api: bool
    status: str
    last_success_at: datetime | None
    last_error: str | None
    metadata_json: dict[str, Any]


class SourceUpdate(BaseModel):
    enabled: bool | None = None


class ScoreReasonRead(BaseModel):
    code: str
    label: str
    effect: str
    points: float
    possible_points: float
    evidence: list[str] = Field(default_factory=list)
    explanation: str = ""


class JobScoreRead(ORMModel):
    id: int
    relevance_score: float
    candidate_fit_score: float | None
    relevance_reasons: list[dict[str, Any]]
    fit_reasons: list[dict[str, Any]]
    scoring_version: str
    profile_version: int | None
    preference_version: int | None
    created_at: datetime


class ApplicationRead(ORMModel):
    id: int
    status: str
    notes: str | None
    documents: list[dict[str, Any]]
    planned_at: datetime | None
    applied_at: datetime | None
    interview_at: datetime | None
    offer_at: datetime | None
    closed_at: datetime | None
    updated_at: datetime


class JobSnapshotRead(ORMModel):
    id: int
    fetched_at: datetime
    content_hash: str
    cleaned_text: str
    structured_data: dict[str, Any]
    change_kind: str


class JobListItem(BaseModel):
    id: int
    title: str
    employer: str | None
    location_text: str | None
    city: str | None
    state: str | None
    remote_type: str | None
    employment_type: str | None
    contract_type: str | None
    published_at: datetime | None
    last_seen_at: datetime
    is_active: bool
    availability_status: str
    availability_checked_at: datetime | None
    availability_reason: str | None
    source: SourceRead
    relevance_score: float | None
    candidate_fit_score: float | None
    application_status: str


class JobListResponse(BaseModel):
    items: list[JobListItem]
    total: int
    limit: int
    offset: int


class JobDetail(BaseModel):
    id: int
    external_id: str
    canonical_url: str | None
    canonical_key: str | None
    title: str
    employer: str | None
    description_text: str
    location_text: str | None
    postcode: str | None
    city: str | None
    state: str | None
    country: str
    remote_type: str | None
    employment_type: str | None
    contract_type: str | None
    language: str | None
    published_at: datetime | None
    expires_at: datetime | None
    first_seen_at: datetime
    last_seen_at: datetime
    is_active: bool
    availability_status: str
    availability_checked_at: datetime | None
    availability_check_method: str | None
    availability_reason: str | None
    structured_data: dict[str, Any]
    source: SourceRead
    latest_score: JobScoreRead | None
    application: ApplicationRead | None
    snapshots: list[JobSnapshotRead]


class JobStatusUpdate(BaseModel):
    status: ApplicationStatus
    notes: str | None = Field(default=None, max_length=20_000)
    confirm_unverified: bool = False


class CandidateProfileBase(BaseModel):
    full_name: str | None = Field(default=None, max_length=300)
    email: str | None = Field(default=None, max_length=320)
    phone: str | None = Field(default=None, max_length=100)
    home_location: str | None = Field(default=None, max_length=500)
    home_latitude: float | None = Field(default=None, ge=-90, le=90)
    home_longitude: float | None = Field(default=None, ge=-180, le=180)
    cv_filename: str | None = Field(default=None, max_length=500)
    cv_text: str | None = None
    structured_data: dict[str, Any] = Field(default_factory=dict)
    is_confirmed: bool = False


class CandidateProfileUpdate(CandidateProfileBase):
    pass


class CandidateProfileRead(CandidateProfileBase, ORMModel):
    id: int
    version: int
    created_at: datetime
    updated_at: datetime



class PreferenceProfileBase(BaseModel):
    employment_types: list[str] = Field(default_factory=lambda: ["Vollzeit"])
    contract_types: list[str] = Field(default_factory=lambda: ["unbefristet", "Direktanstellung"])
    preferred_states: list[str] = Field(default_factory=lambda: ["Baden-Württemberg"])
    include_germany_remote: bool = True
    commute_penalty_minutes: int = Field(default=60, ge=0, le=600)
    max_commute_minutes: int = Field(default=75, ge=0, le=600)
    onsite_weight: float = Field(default=1.0, ge=0, le=1)
    hybrid_weight: float = Field(default=0.55, ge=0, le=1)
    remote_weight: float = Field(default=0.2, ge=0, le=1)
    min_salary: int | None = Field(default=None, ge=0)
    target_salary: int | None = Field(default=None, ge=0)
    languages: list[str] = Field(default_factory=lambda: ["de", "en"])
    primary_role_terms: list[str] = Field(default_factory=lambda: list(DEFAULT_PRIMARY_ROLE_TERMS))
    additional_role_terms: list[str] = Field(
        default_factory=lambda: list(DEFAULT_ADDITIONAL_ROLE_TERMS)
    )
    extra_preferences: dict[str, Any] = Field(default_factory=dict)

    @field_validator(
        "employment_types",
        "contract_types",
        "preferred_states",
        "languages",
        "primary_role_terms",
        "additional_role_terms",
    )
    @classmethod
    def _clean_lists(cls, values: list[str]) -> list[str]:
        result: list[str] = []
        for value in values:
            clean = value.strip()
            if clean and clean not in result:
                result.append(clean)
        return result

    @model_validator(mode="after")
    def _validate_ranges(self) -> PreferenceProfileBase:
        if self.max_commute_minutes < self.commute_penalty_minutes:
            raise ValueError("max_commute_minutes must be at least commute_penalty_minutes")
        if (
            self.target_salary is not None
            and self.min_salary is not None
            and self.target_salary < self.min_salary
        ):
            raise ValueError("target_salary must be at least min_salary")
        if not self.primary_role_terms:
            raise ValueError("primary_role_terms must contain at least one term")
        if (
            len(self.primary_role_terms) > 10
            or len(self.additional_role_terms) > 50
            or any(
                len(term) > 100
                for term in (*self.primary_role_terms, *self.additional_role_terms)
            )
        ):
            raise ValueError("role term limits exceeded")
        primary_keys = {term.casefold() for term in self.primary_role_terms}
        self.additional_role_terms = [
            term
            for term in self.additional_role_terms
            if term.casefold() not in primary_keys
        ]
        return self


class PreferenceProfileUpdate(PreferenceProfileBase):
    pass


class PreferenceProfileRead(PreferenceProfileBase, ORMModel):
    id: int
    candidate_profile_id: int | None
    version: int
    created_at: datetime
    updated_at: datetime


class SettingEntry(BaseModel):
    key: str
    value: Any
    description: str | None = None
    source: str = "database"


class SettingsRead(BaseModel):
    api_access_enabled: bool
    firecrawl_enabled: bool
    entries: list[SettingEntry]


class SettingsUpdate(BaseModel):
    api_access_enabled: bool | None = None
    firecrawl_enabled: bool | None = None
    values: dict[str, Any] = Field(default_factory=dict)

    @field_validator("values")
    @classmethod
    def _forbid_global_lock_alias(cls, values: dict[str, Any]) -> dict[str, Any]:
        dedicated_fields = {"api_access_enabled", "firecrawl_enabled"}
        if dedicated_fields.intersection(values):
            raise ValueError("Use the dedicated permission fields")
        return values


class CrawlRunRead(ORMModel):
    id: int
    source_id: int | None
    run_type: str
    status: str
    started_at: datetime | None
    finished_at: datetime | None
    discovered_count: int
    fetched_count: int
    created_count: int
    updated_count: int
    skipped_count: int
    error_count: int
    error_message: str | None
    details: dict[str, Any]
    lock_reason: str | None
    created_at: datetime
