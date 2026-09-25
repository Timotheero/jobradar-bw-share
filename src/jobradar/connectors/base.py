"""Shared contracts and normalized raw records for source connectors.

The records in this module deliberately retain the provider payload in ``raw``.
They normalize only the small set of fields needed by the ingestion pipeline;
source-specific interpretation belongs in later processing stages.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class SourceInfo:
    """Static provenance and risk metadata for a connector."""

    key: str
    name: str
    acquisition: str
    official: bool
    default_enabled: bool = True
    experimental: bool = False
    documentation_url: str | None = None
    terms_url: str | None = None
    notes: str | None = None


@dataclass(frozen=True, slots=True)
class PaginationOptions:
    """Provider-independent pagination guardrails."""

    page_size: int = 50
    max_pages: int | None = None
    start_page: int = 1

    def __post_init__(self) -> None:
        if self.page_size < 1:
            raise ValueError("page_size must be at least 1")
        if self.max_pages is not None and self.max_pages < 1:
            raise ValueError("max_pages must be at least 1 when set")
        if self.start_page < 1:
            raise ValueError("start_page must be at least 1")


@dataclass(frozen=True, slots=True)
class JobQuery:
    """A deliberately small query shared by all job-board connectors."""

    text: str | None = None
    location: str | None = None
    radius_km: int | None = None
    published_since_days: int | None = None
    pagination: PaginationOptions = field(default_factory=PaginationOptions)
    filters: Mapping[str, str | int | bool] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.radius_km is not None and self.radius_km < 0:
            raise ValueError("radius_km must be non-negative")
        if self.published_since_days is not None and not 0 <= self.published_since_days <= 100:
            raise ValueError("published_since_days must be between 0 and 100")


@dataclass(frozen=True, slots=True)
class RawJobRecord:
    """Normalized envelope around an unmodified provider job payload."""

    source: str
    source_job_id: str
    title: str
    company: str | None = None
    locations: tuple[str, ...] = ()
    description: str | None = None
    description_format: str | None = None
    canonical_url: str | None = None
    apply_url: str | None = None
    published_at: datetime | None = None
    updated_at: datetime | None = None
    employment_types: tuple[str, ...] = ()
    remote: bool | None = None
    salary: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    raw: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.source.strip():
            raise ValueError("source must not be empty")
        if not self.source_job_id.strip():
            raise ValueError("source_job_id must not be empty")
        if not self.title.strip():
            raise ValueError("title must not be empty")




@runtime_checkable
class JobConnector(Protocol):
    """Protocol implemented by connectors that yield individual jobs."""

    source_info: SourceInfo

    def iter_jobs(self, query: JobQuery | None = None) -> Iterator[RawJobRecord]: ...




def parse_datetime(value: Any) -> datetime | None:
    """Parse common ISO strings and Unix seconds/milliseconds as UTC."""

    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime(value.year, value.month, value.day)
    elif isinstance(value, (int, float)):
        seconds = float(value) / 1000 if abs(float(value)) >= 100_000_000_000 else float(value)
        return datetime.fromtimestamp(seconds, tz=UTC)
    elif isinstance(value, str):
        candidate = value.strip()
        if not candidate:
            return None
        if candidate.isdigit():
            return parse_datetime(int(candidate))
        if candidate.endswith("Z"):
            candidate = f"{candidate[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            return None
    else:
        return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def string_tuple(value: Any) -> tuple[str, ...]:
    """Coerce a provider scalar/list into a compact tuple of unique strings."""

    if value is None:
        return ()
    values = value if isinstance(value, (list, tuple, set)) else (value,)
    result: list[str] = []
    for item in values:
        if isinstance(item, Mapping):
            item = item.get("name") or item.get("location") or item.get("text")
        if item is None:
            continue
        text = str(item).strip()
        if text and text not in result:
            result.append(text)
    return tuple(result)


def local_query_match(record: RawJobRecord, query: JobQuery | None) -> bool:
    """Apply text/location filters for providers without server-side search."""

    if query is None:
        return True
    if query.text:
        needle = query.text.casefold()
        haystack = " ".join(
            part for part in (record.title, record.company or "", record.description or "") if part
        ).casefold()
        if needle not in haystack:
            return False
    if query.location:
        needle = query.location.casefold()
        if not any(needle in location.casefold() for location in record.locations):
            return False
    return True
