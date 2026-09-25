"""Connector for Deutsche Bahn's public server-rendered job search."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin

import httpx

from .base import JobQuery, RawJobRecord, SourceInfo
from .http import HTTPTransport, RetryPolicy, TimeoutOptions


class DeutscheBahnConnector:
    BASE_URL = "https://db.jobs"
    SEARCH_PATH = "/service/search/de-de/5441588"
    MAX_PAGE_SIZE = 50
    MAX_PAGES = 20

    source_info = SourceInfo(
        key="deutsche_bahn",
        name="Deutsche Bahn Jobs",
        acquisition="public_server_rendered_search",
        official=True,
        documentation_url="https://db.jobs/de-de",
    )

    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        timeout: TimeoutOptions | None = None,
        retry: RetryPolicy | None = None,
    ) -> None:
        self._http = HTTPTransport(
            self.BASE_URL,
            client=client,
            timeout=timeout,
            retry=retry,
            default_headers={"Accept": "text/html"},
        )

    def close(self) -> None:
        self._http.close()

    def iter_jobs(self, query: JobQuery | None = None) -> Iterator[RawJobRecord]:
        query = query or JobQuery()
        pagination = query.pagination
        if pagination.page_size > self.MAX_PAGE_SIZE:
            raise ValueError(f"Deutsche Bahn page_size must not exceed {self.MAX_PAGE_SIZE}")
        max_pages = min(pagination.max_pages or self.MAX_PAGES, self.MAX_PAGES)
        page = pagination.start_page - 1
        seen: set[str] = set()

        for _ in range(max_pages):
            params: dict[str, Any] = {
                "qli": "true",
                "query": query.text or "",
                "location": query.location or "",
                "sort": "pubExternalDate_tdt",
                "itemsPerPage": pagination.page_size,
                "pageNum": page,
            }
            if query.radius_km is not None:
                params["radius"] = query.radius_km
            params.update(query.filters)
            response = self._http.request("GET", self.SEARCH_PATH, params=params)
            parser = _DBSearchParser()
            parser.feed(response.text)
            parser.close()
            if not parser.saw_results:
                raise ValueError("Deutsche Bahn search response has no result container")
            if not parser.jobs:
                return

            new_ids = 0
            for job in parser.jobs:
                record = _normalize_job(job)
                if record is None or record.source_job_id in seen:
                    continue
                seen.add(record.source_job_id)
                new_ids += 1
                yield record
            if not new_ids or len(parser.jobs) < pagination.page_size:
                return
            page += 1


def _normalize_job(job: Mapping[str, Any]) -> RawJobRecord | None:
    job_id = _text(job.get("id"))
    title = _text(job.get("title"))
    href = _text(job.get("href"))
    if not job_id or not title or not href:
        return None
    locations = tuple(job.get("locations") or ())
    company = _text(job.get("company"))
    canonical_url = urljoin(DeutscheBahnConnector.BASE_URL, href)
    return RawJobRecord(
        source="deutsche_bahn",
        source_job_id=job_id,
        title=title,
        company=company,
        locations=locations,
        canonical_url=canonical_url,
        apply_url=canonical_url,
        raw=dict(job),
    )


class _DBSearchParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.saw_results = False
        self.jobs: list[dict[str, Any]] = []
        self._job: dict[str, Any] | None = None
        self._depth = 0
        self._field: str | None = None
        self._field_depth = 0
        self._parts: list[str] = []
        self._fragment: list[str] = []

    def handle_starttag(self, tag: str, attrs_list: list[tuple[str, str | None]]) -> None:
        attrs = dict(attrs_list)
        classes = set((attrs.get("class") or "").split())
        if attrs.get("id") == "resultItems":
            self.saw_results = True
        if self._job is None and tag == "a" and "m-search-hit" in classes:
            self._job = {
                "id": attrs.get("data-job-id"),
                "href": attrs.get("href"),
                "unpublication_external": attrs.get("data-unpub-external-date"),
                "unpublication_internal": attrs.get("data-unpub-internal-date"),
                "locations": [],
            }
            self._depth = 1
            self._fragment = [self.get_starttag_text()]
            return
        if self._job is None:
            return
        self._depth += 1
        self._fragment.append(self.get_starttag_text())
        if "m-search-hit__title-text" in classes:
            self._begin_field("title")
        elif tag == "i" and attrs.get("aria-label") == "Arbeitsort":
            self._begin_field("location", after_current=True)
        elif tag == "i" and attrs.get("aria-label") in {"Arbeitgeber:in", "Arbeitgeber"}:
            self._begin_field("company", after_current=True)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._job is not None:
            self._fragment.append(self.get_starttag_text())

    def handle_data(self, data: str) -> None:
        if self._job is not None:
            self._fragment.append(data)
            if self._field:
                self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._job is None:
            return
        self._fragment.append(f"</{tag}>")
        if self._field and self._depth == self._field_depth:
            self._finish_field()
        self._depth -= 1
        if self._depth == 0:
            self._job["html"] = "".join(self._fragment)
            self.jobs.append(self._job)
            self._job = None
            self._fragment = []

    def _begin_field(self, field: str, *, after_current: bool = False) -> None:
        self._field = field
        self._field_depth = self._depth - 1 if after_current else self._depth
        self._parts = []

    def _finish_field(self) -> None:
        assert self._job is not None and self._field is not None
        value = " ".join("".join(self._parts).split())
        if value:
            if self._field == "location":
                self._job["locations"] = [part.strip() for part in value.split(",") if part.strip()]
            else:
                self._job[self._field] = value
        self._field = None
        self._parts = []


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
