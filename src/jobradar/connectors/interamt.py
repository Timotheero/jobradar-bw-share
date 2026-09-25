"""Connector for INTERAMT's public, server-rendered job search."""

from __future__ import annotations

import html
import re
from collections.abc import Iterator
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin

import httpx

from .base import JobQuery, RawJobRecord, SourceInfo, parse_datetime
from .http import HTTPTransport, RetryPolicy, TimeoutOptions


class InteramtConnector:
    """Search INTERAMT through its public Wicket form and bounded AJAX pagination."""

    BASE_URL = "https://interamt.de/koop/app/"
    MAX_LOAD_MORE_REQUESTS = 50
    source_info = SourceInfo(
        key="interamt",
        name="INTERAMT",
        acquisition="public_html_search",
        official=True,
        default_enabled=True,
        experimental=True,
        documentation_url="https://interamt.de/koop/app/",
        terms_url="https://interamt.de/cms/legal/nutzungsbedingungen.html",
        notes="Public job search; browser-equivalent Wicket session flow.",
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
            default_headers={
                "Accept-Language": "de-DE,de;q=0.9,en;q=0.5",
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/150 Safari/537.36"
                ),
            },
        )

    def close(self) -> None:
        self._http.close()

    def iter_jobs(self, query: JobQuery | None = None) -> Iterator[RawJobRecord]:
        query = query or JobQuery()
        response = self._http.request("GET", self.BASE_URL)
        form_action = _quicksearch_action(response.text)
        if form_action is None:
            raise ValueError("INTERAMT quick-search form was not found")

        values = {
            "PLZ": query.location or "",
            "idOrSuchtext": query.text or "",
            "navFooter:navFooter_body:submitRow:actions:0:button": "",
        }
        results = self._http.request(
            "POST",
            urljoin(str(response.url), html.unescape(form_action)),
            data=values,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": "https://interamt.de",
                "Referer": str(response.url),
            },
            retryable=False,
        )
        yield from self._records(results.text, str(results.url))

        page_limit = min(
            query.pagination.max_pages or self.MAX_LOAD_MORE_REQUESTS + 1,
            self.MAX_LOAD_MORE_REQUESTS + 1,
        )
        for _ in range(1, page_limit):
            load_more = _load_more_url(results.text)
            if load_more is None:
                break
            base_url = _wicket_base_url(results.text) or _relative_base(str(results.url))
            results = self._http.request(
                "GET",
                urljoin(str(results.url), html.unescape(load_more)),
                headers={
                    "Referer": str(results.url),
                    "Wicket-Ajax": "true",
                    "Wicket-Ajax-BaseURL": base_url,
                    "X-Requested-With": "XMLHttpRequest",
                },
            )
            records = tuple(self._records(results.text, str(results.url)))
            if not records:
                break
            yield from records

    def _records(self, body: str, response_url: str) -> Iterator[RawJobRecord]:
        parser = _ResultParser()
        parser.feed(html.unescape(body).replace("<![CDATA[", "").replace("]]>", ""))
        for row in parser.rows:
            job_id = _job_id(row.get("StellenangebotId"))
            title = _text(row.get("Stellenbezeichnung"))
            if not job_id or not title:
                continue
            location = _text(row.get("PLZOrte"))
            workplace = _text(row.get("Dienstort"))
            detail_url = f"https://interamt.de/koop/app/stelle?id={job_id}"
            yield RawJobRecord(
                source=self.source_info.key,
                source_job_id=job_id,
                title=title,
                company=_text(row.get("Behoerde")),
                locations=(location,) if location else (),
                description=None,
                canonical_url=detail_url,
                apply_url=detail_url,
                published_at=_german_date(row.get("Von")),
                remote=_remote(workplace),
                salary=_salary(row),
                metadata={
                    "workplace_type": workplace,
                    "application_deadline": _text(row.get("Bewerbungsfrist")),
                    "pay_grade": _text(row.get("BesoldungGruppeDisplayString")),
                    "collective_pay": _text(row.get("TarifEbeneDisplayString")),
                    "result_url": response_url,
                },
                raw=dict(row),
            )


class _ResultParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[dict[str, str]] = []
        self._row: dict[str, str] | None = None
        self._field: str | None = None
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key: value or "" for key, value in attrs}
        classes = attributes.get("class", "").split()
        if tag == "tr" and "ia-e-table__row" in classes:
            self._row = {}
        elif tag == "td" and self._row is not None and attributes.get("data-field"):
            self._field = attributes["data-field"]
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._field is not None:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "td" and self._row is not None and self._field is not None:
            self._row[self._field] = " ".join("".join(self._parts).split())
            self._field = None
            self._parts = []
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None
            self._field = None


def _quicksearch_action(body: str) -> str | None:
    match = re.search(
        r'<form[^>]+class="[^"]*ia-m-form--stage-quicksearch[^"]*"[^>]+action="([^"]+)"',
        body,
        re.IGNORECASE,
    )
    return match.group(1) if match else None


def _load_more_url(body: str) -> str | None:
    matches = re.findall(
        r'Wicket\.Ajax\.ajax\(\{"u":"([^"]*loadMoreLink)"', body, re.IGNORECASE
    )
    return matches[-1] if matches else None


def _wicket_base_url(body: str) -> str | None:
    match = re.search(r'Wicket\.Ajax\.baseUrl="([^"]+)"', body)
    return html.unescape(match.group(1)) if match else None


def _relative_base(url: str) -> str:
    return url.split("://", 1)[-1].split("/", 1)[-1]


def _job_id(value: Any) -> str | None:
    text = _text(value)
    match = re.match(r"\d+", text or "")
    return match.group(0) if match else None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split()).strip()
    return text or None


def _german_date(value: Any):
    text = _text(value)
    if not text:
        return None
    match = re.fullmatch(r"(\d{2})\.(\d{2})\.(\d{4})", text)
    if not match:
        return parse_datetime(text)
    day, month, year = match.groups()
    return parse_datetime(f"{year}-{month}-{day}")


def _remote(workplace: str | None) -> bool | None:
    normalized = (workplace or "").casefold()
    if "hybrid" in normalized or "homeoffice" in normalized or "remote" in normalized:
        return True
    if "vor ort" in normalized or "präsenz" in normalized:
        return False
    return None


def _salary(row: dict[str, str]) -> str | None:
    values = [
        value
        for key in ("BesoldungGruppeDisplayString", "TarifEbeneDisplayString")
        if (value := _text(row.get(key)))
    ]
    return " / ".join(values) or None
