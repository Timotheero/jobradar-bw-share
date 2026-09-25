"""Connector for Rheinmetall's server-rendered public vacancy search."""

from __future__ import annotations

import re
from collections.abc import Iterator
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urljoin

import httpx

from .base import JobQuery, RawJobRecord, SourceInfo, local_query_match
from .http import HTTPTransport, RetryPolicy, TimeoutOptions


class RheinmetallConnector:
    source_info = SourceInfo(
        key="rheinmetall",
        name="Rheinmetall vacancies",
        acquisition="official_public_career_site",
        official=True,
        documentation_url="https://www.rheinmetall.com/de/karriere/aktuelle-stellenangebote",
    )

    def __init__(
        self,
        *,
        language: str = "de",
        client: httpx.Client | None = None,
        timeout: TimeoutOptions | None = None,
        retry: RetryPolicy | None = None,
    ) -> None:
        language = language.strip().casefold()
        if language not in {"de", "en"}:
            raise ValueError("Rheinmetall language must be 'de' or 'en'")
        self.language = language
        self.path = (
            "/de/karriere/aktuelle-stellenangebote" if language == "de" else "/en/career/vacancies"
        )
        self._http = HTTPTransport(
            "https://www.rheinmetall.com",
            client=client,
            timeout=timeout,
            retry=retry,
            default_headers={"Accept": "text/html"},
        )

    def close(self) -> None:
        self._http.close()

    def iter_jobs(self, query: JobQuery | None = None) -> Iterator[RawJobRecord]:
        options = query.pagination if query else None
        start, cap = (
            (options.start_page if options else 1),
            min(options.max_pages or 100, 100) if options else 100,
        )
        seen: set[str] = set()
        for page in range(start, start + cap):
            params = {"page": page}
            if query and query.text:
                params["search"] = query.text
            if query and query.location:
                params["location"] = query.location
            response = self._http.request("GET", self.path, params=params)
            if "html" not in response.headers.get(
                "content-type", ""
            ).casefold() and not response.text.lstrip().startswith("<"):
                raise ValueError("Rheinmetall vacancy response is not HTML")
            parser = _VacancyParser()
            parser.feed(response.text)
            if not parser.saw_search:
                raise ValueError("Rheinmetall response lacks the vacancy search structure")
            if not parser.rows:
                break
            new = 0
            for href, title, company_location in parser.rows:
                match = re.search(r"/(\d+)(?:[/?#]|$)", href)
                if not match or not title:
                    continue
                job_id = match.group(1)
                if job_id in seen:
                    continue
                seen.add(job_id)
                new += 1
                company, _, location = company_location.partition("|")
                record = RawJobRecord(
                    source="rheinmetall",
                    source_job_id=job_id,
                    title=title,
                    company=company.strip() or "Rheinmetall",
                    locations=(location.strip(),) if location.strip() else (),
                    canonical_url=urljoin("https://www.rheinmetall.com", href),
                    apply_url=urljoin("https://www.rheinmetall.com", href),
                    raw={"href": href, "title": title, "company_location": company_location},
                )
                if local_query_match(record, query):
                    yield record
            if new == 0 or parser.last_page is not None and page >= parser.last_page:
                break


class _VacancyParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[tuple[str, str, str]] = []
        self.saw_search = False
        self.last_page: int | None = None
        self._href: str | None = None
        self._depth = 0
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if values.get("id") == "anchor-jobsuche" or values.get("data-id") == "anchor-jobsuche":
            self.saw_search = True
        if self._href is not None:
            self._depth += 1
        elif (
            tag == "a"
            and values.get("href")
            and re.search(r"/(?:de|en)/job/", values["href"] or "")
        ):
            self._href = values["href"]
            self._depth = 1
            self._text = []

    def handle_endtag(self, tag: str) -> None:
        if self._href is None:
            return
        self._depth -= 1
        if self._depth == 0:
            text = " ".join(" ".join(self._text).split())
            if text and not any(row[0] == self._href for row in self.rows):
                parts = [part.strip() for part in text.split(" | ")]
                self.rows.append((self._href, parts[0], " | ".join(parts[1:])))
            self._href = None

    def handle_data(self, data: str) -> None:
        text = unescape(data).strip()
        if self._href is not None and text:
            self._text.append(text)
        match = re.search(r"(?:Seite|Page)\s+\d+\s+(?:von|of)\s+(\d+)", text, re.I)
        if match:
            self.last_page = int(match.group(1))
