"""Connector for public tenant-specific softgarden widget pages."""

from __future__ import annotations

import re
from collections.abc import Iterator
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from .base import JobQuery, RawJobRecord, SourceInfo, local_query_match, parse_datetime
from .http import HTTPTransport, RetryPolicy, TimeoutOptions

_TENANT_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
_LANGUAGE_PATTERN = re.compile(r"^[a-z]{2}$")
_JOB_PATH_PATTERN = re.compile(r"/(?:[^/]+/)?job/(\d+)(?:/|$)", re.IGNORECASE)


class SoftgardenConnector:
    def __init__(
        self,
        tenant: str,
        *,
        company: str | None = None,
        language: str = "de",
        client: httpx.Client | None = None,
        timeout: TimeoutOptions | None = None,
        retry: RetryPolicy | None = None,
    ) -> None:
        tenant = tenant.strip().casefold()
        language = language.strip().casefold()
        if not _TENANT_PATTERN.fullmatch(tenant):
            raise ValueError("softgarden tenant must be a valid softgarden.io subdomain")
        if not _LANGUAGE_PATTERN.fullmatch(language):
            raise ValueError("softgarden language must be a two-letter code")
        self.tenant = tenant
        self.company = company.strip() if company and company.strip() else None
        self.language = language
        self.base_url = f"https://{tenant}.softgarden.io"
        self.source_info = SourceInfo(
            key=f"softgarden:{tenant}",
            name=f"softgarden job widget ({tenant})",
            acquisition="official_public_widget",
            official=True,
            documentation_url="https://softgarden.com",
        )
        self._http = HTTPTransport(
            self.base_url,
            client=client,
            timeout=timeout,
            retry=retry,
            default_headers={"Accept": "text/html"},
        )

    def close(self) -> None:
        self._http.close()

    def iter_jobs(self, query: JobQuery | None = None) -> Iterator[RawJobRecord]:
        response = self._http.request("GET", f"/{self.language}/vacancies")
        parser = _SoftgardenParser(response.url)
        parser.feed(response.text)
        parser.close()
        if not parser.saw_collection:
            raise ValueError("softgarden response has no usable vacancy collection")
        seen: set[str] = set()
        for job in parser.jobs:
            job_id = str(job["id"])
            if job_id in seen:
                continue
            seen.add(job_id)
            title = str(job["title"])
            url = str(job["url"])
            record = RawJobRecord(
                source=self.source_info.key,
                source_job_id=job_id,
                title=title,
                company=self.company or parser.company,
                locations=tuple(job.get("locations") or ()),
                canonical_url=url,
                apply_url=url,
                published_at=parse_datetime(job.get("date")),
                raw=dict(job),
            )
            if local_query_match(record, query):
                yield record


class _SoftgardenParser(HTMLParser):
    def __init__(self, response_url: httpx.URL) -> None:
        super().__init__(convert_charrefs=True)
        self.response_url = str(response_url)
        self.saw_collection = False
        self.jobs: list[dict[str, Any]] = []
        self.company: str | None = None
        self._job: dict[str, Any] | None = None
        self._job_depth = 0
        self._field: str | None = None
        self._field_depth = 0
        self._parts: list[str] = []
        self._fragment: list[str] = []
        self._in_brand = False

    def handle_starttag(self, tag: str, attrs_list: list[tuple[str, str | None]]) -> None:
        attrs = dict(attrs_list)
        classes = set((attrs.get("class") or "").split())
        element_id = attrs.get("id") or ""
        if "outputContainer" in classes or element_id == "jobSearchCss":
            self.saw_collection = True
        if tag == "a" and "brand" in classes:
            self._in_brand = True
            self._parts = []
        if self._job is None and "matchElement" in classes and element_id.startswith("job_id_"):
            self._job = {"id": element_id.removeprefix("job_id_"), "locations": []}
            self._job_depth = 1
            self._fragment = [self.get_starttag_text()]
            return
        if self._job is None:
            return
        self._job_depth += 1
        self._fragment.append(self.get_starttag_text())
        if "date" in classes:
            self._begin_field("date")
        elif "ProjectGeoLocationCity" in classes:
            self._begin_field("location")
        if tag == "a":
            href = attrs.get("href")
            matched = _JOB_PATH_PATTERN.search(urlsplit(href or "").path)
            if matched:
                self._job["id"] = matched.group(1)
                self._job["url"] = urljoin(self.response_url, href or "")
                self._begin_field("title")

    def handle_data(self, data: str) -> None:
        if self._in_brand and self._job is None:
            self._parts.append(data)
        if self._job is not None:
            self._fragment.append(data)
            if self._field:
                self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._in_brand and tag == "a" and self._job is None:
            self.company = " ".join("".join(self._parts).split()) or None
            self._in_brand = False
            self._parts = []
        if self._job is None:
            return
        self._fragment.append(f"</{tag}>")
        if self._field and self._job_depth == self._field_depth:
            self._finish_field()
        self._job_depth -= 1
        if self._job_depth == 0:
            if self._field:
                self._finish_field()
            self._job["html"] = "".join(self._fragment)
            if self._job.get("id") and self._job.get("title") and self._job.get("url"):
                self.jobs.append(self._job)
            self._job = None
            self._fragment = []

    def _begin_field(self, field: str) -> None:
        self._field = field
        self._field_depth = self._job_depth
        self._parts = []

    def _finish_field(self) -> None:
        assert self._job is not None and self._field is not None
        value = " ".join("".join(self._parts).split())
        if value:
            if self._field == "location":
                self._job["locations"] = [
                    part.strip(" ,") for part in value.split(";") if part.strip(" ,")
                ]
            else:
                self._job[self._field] = value
        self._field = None
        self._parts = []
