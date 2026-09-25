"""Read-only reporting for configured Firecrawl targets."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import CrawlRun, CrawlRunStatus, JobPosting, Source

CRAWLER_RUNNING_STALE_AFTER = timedelta(hours=3)


@dataclass(frozen=True, slots=True)
class TargetCrawlError:
    document_url: str | None
    message: str


@dataclass(frozen=True, slots=True)
class TargetCrawlReport:
    label: str
    url: str
    target_kind: str
    last_crawled_at: datetime | None
    found_count: int
    stored_job_count: int
    added_last_day: int
    status: str | None
    documents_seen: int
    pages_rejected: int
    duplicates_skipped: int
    query_filtered: int
    page_error_count: int
    errors: tuple[TargetCrawlError, ...]


@dataclass(frozen=True, slots=True)
class CrawlerRuntimeStatus:
    state: str
    configured: bool
    last_run_at: datetime | None
    error_message: str | None


def firecrawl_runtime_status(
    session: Session,
    source: Source | None,
    *,
    enabled: bool,
    now: datetime | None = None,
) -> CrawlerRuntimeStatus:
    """Report operational state from the latest run, not permission switches."""

    configured = bool(source and _configured_targets(source.metadata_json))
    if source is None or not source.enabled or not enabled:
        return CrawlerRuntimeStatus("paused", configured, None, None)
    if not configured:
        return CrawlerRuntimeStatus("pending", False, None, None)

    latest = session.scalar(
        select(CrawlRun)
        .where(CrawlRun.source_id == source.id)
        .order_by(CrawlRun.id.desc())
        .limit(1)
    )
    if latest is None:
        return CrawlerRuntimeStatus("pending", True, None, None)

    last_run_at = latest.finished_at or latest.started_at or latest.created_at
    if latest.status == CrawlRunStatus.RUNNING.value:
        checked_at = now or datetime.now(UTC)
        started_at = latest.started_at
        if started_at is not None and started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=UTC)
        if started_at is None or started_at < checked_at - CRAWLER_RUNNING_STALE_AFTER:
            return CrawlerRuntimeStatus(
                "error",
                True,
                last_run_at,
                "Crawler-Lauf wurde seit mehr als drei Stunden nicht abgeschlossen.",
            )
        return CrawlerRuntimeStatus("running", True, last_run_at, None)
    if latest.status == CrawlRunStatus.SUCCEEDED.value:
        return CrawlerRuntimeStatus("ready", True, last_run_at, None)
    if latest.status == CrawlRunStatus.PARTIAL.value:
        return CrawlerRuntimeStatus("limited", True, last_run_at, latest.error_message)
    if latest.status == CrawlRunStatus.FAILED.value:
        return CrawlerRuntimeStatus("error", True, last_run_at, latest.error_message)
    return CrawlerRuntimeStatus("pending", True, last_run_at, latest.lock_reason)


def firecrawl_target_reports(
    session: Session,
    source: Source | None,
) -> tuple[TargetCrawlReport, ...]:
    """Return the latest completed result and current job count for every target."""

    if source is None:
        return ()
    targets = _configured_targets(source.metadata_json)
    if not targets:
        return ()

    added_since = datetime.now(UTC) - timedelta(days=1)
    metadata_expression = JobPosting.structured_data["source_metadata"]
    label_expression = metadata_expression["firecrawl_target_label"].as_string()
    url_expression = metadata_expression["firecrawl_target_url"].as_string()
    stored_by_label: dict[str, int] = {}
    stored_by_url: dict[str, int] = {}
    added_by_label: dict[str, int] = {}
    added_by_url: dict[str, int] = {}
    for label, url, count, added_count in session.execute(
        select(
            label_expression,
            url_expression,
            func.count(JobPosting.id),
            func.count(JobPosting.id).filter(JobPosting.created_at >= added_since),
        )
        .where(JobPosting.source_id == source.id)
        .group_by(label_expression, url_expression)
    ).all():
        if label:
            stored_by_label[str(label)] = stored_by_label.get(str(label), 0) + int(count)
            added_by_label[str(label)] = added_by_label.get(str(label), 0) + int(
                added_count
            )
        if url:
            stored_by_url[str(url)] = stored_by_url.get(str(url), 0) + int(count)
            added_by_url[str(url)] = added_by_url.get(str(url), 0) + int(added_count)
    latest_results: dict[
        str,
        tuple[datetime | None, Mapping[str, Any], tuple[TargetCrawlError, ...]],
    ] = {}
    runs = session.scalars(
        select(CrawlRun)
        .where(CrawlRun.source_id == source.id, CrawlRun.finished_at.is_not(None))
        .order_by(CrawlRun.id.desc())
        .limit(100)
    )
    configured_urls = {target["url"] for target in targets}
    for run in runs:
        connector = _mapping((run.details or {}).get("connector"))
        page_errors_by_target: dict[str, list[TargetCrawlError]] = {}
        for raw_error in _sequence(connector.get("page_errors")):
            error = _mapping(raw_error)
            target_url = _text(error.get("target"))
            message = _text(error.get("error"))
            if not target_url or not message:
                continue
            page_errors_by_target.setdefault(target_url, []).append(
                TargetCrawlError(
                    document_url=_text(error.get("document_url")),
                    message=message,
                )
            )
        for result in _sequence(connector.get("target_results")):
            item = _mapping(result)
            url = _text(item.get("target"))
            if not url or url not in configured_urls or url in latest_results:
                continue
            latest_results[url] = (
                run.finished_at or run.started_at,
                item,
                tuple(page_errors_by_target.get(url, ())),
            )
        if (
            not _sequence(connector.get("target_results"))
            and _non_negative_int(connector.get("targets_started")) >= len(targets)
        ):
            target_errors = tuple(
                _mapping(value) for value in _sequence(connector.get("target_errors"))
            )
            errors_by_url = {
                url: item
                for item in target_errors
                if (url := _text(item.get("target"))) is not None
            }
            errors_by_label = {
                label: item
                for item in target_errors
                if (label := _text(item.get("label"))) is not None
            }
            for target in targets:
                if target["url"] in latest_results:
                    continue
                failure = errors_by_url.get(target["url"]) or errors_by_label.get(
                    target["label"]
                )
                result: dict[str, Any] = {
                    "status": "failed" if failure else "completed"
                }
                if failure and (message := _text(failure.get("error"))):
                    result["error"] = message
                latest_results[target["url"]] = (
                    run.finished_at or run.started_at,
                    result,
                    (),
                )
        if len(latest_results) == len(configured_urls):
            break

    reports = []
    for target in targets:
        latest = latest_results.get(target["url"])
        crawled_at, result, page_errors = (
            latest if latest is not None else (None, {}, ())
        )
        stored_job_count = stored_by_label.get(
            target["label"], stored_by_url.get(target["url"], 0)
        )
        found_count = (
            _non_negative_int(result.get("records_emitted"))
            if "records_emitted" in result
            else stored_job_count
        )
        errors = list(page_errors)
        if message := _text(result.get("error")):
            errors.insert(0, TargetCrawlError(document_url=None, message=message))
        status = _text(result.get("status"))
        if errors and status == "completed":
            status = "partial"
        elif errors and status in {"empty", "insufficient"}:
            status = "failed"
        reports.append(
            TargetCrawlReport(
                label=target["label"],
                url=target["url"],
                target_kind=target["target_kind"],
                last_crawled_at=crawled_at,
                found_count=found_count,
                stored_job_count=stored_job_count,
                added_last_day=added_by_label.get(
                    target["label"], added_by_url.get(target["url"], 0)
                ),
                status=status,
                documents_seen=_non_negative_int(result.get("documents_seen")),
                pages_rejected=_non_negative_int(result.get("pages_rejected")),
                duplicates_skipped=_non_negative_int(result.get("duplicates_skipped")),
                query_filtered=_non_negative_int(result.get("query_filtered")),
                page_error_count=max(
                    _non_negative_int(result.get("page_error_count")),
                    len(page_errors),
                ),
                errors=tuple(errors),
            )
        )
    return tuple(reports)


def _configured_targets(metadata: Mapping[str, Any] | None) -> tuple[dict[str, str], ...]:
    root = _mapping(metadata)
    firecrawl = _mapping(root.get("firecrawl"))
    result = []
    for value in _sequence(firecrawl.get("targets")):
        item = {"url": value} if isinstance(value, str) else _mapping(value)
        url = _text(item.get("url"))
        if not url:
            continue
        label = _text(item.get("label")) or (urlsplit(url).hostname or url)
        target_kind = _text(item.get("target_kind") or item.get("category"))
        if target_kind not in {"job_portal", "company_site"}:
            target_kind = "company_site"
        result.append({"label": label, "url": url, "target_kind": target_kind})
    return tuple(result)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> tuple[Any, ...]:
    return tuple(value) if isinstance(value, (list, tuple)) else ()


def _text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _non_negative_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0
