"""Connector for employer-enabled public Personio XML job feeds."""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlencode
from xml.etree import ElementTree

import httpx

from .base import JobQuery, RawJobRecord, SourceInfo, local_query_match, parse_datetime
from .http import HTTPTransport, RetryPolicy, TimeoutOptions

_ACCOUNT_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,98}[A-Za-z0-9]$|^[A-Za-z0-9]$")


class PersonioJobFeedConnector:
    def __init__(
        self,
        account: str,
        *,
        company: str | None = None,
        language: str = "de",
        client: httpx.Client | None = None,
        timeout: TimeoutOptions | None = None,
        retry: RetryPolicy | None = None,
    ) -> None:
        account = account.strip().casefold()
        if not _ACCOUNT_PATTERN.fullmatch(account):
            raise ValueError("Personio account must be a valid jobs.personio.de subdomain")
        language = language.strip().casefold()
        if not re.fullmatch(r"[a-z]{2}", language):
            raise ValueError("Personio language must be a two-letter code")
        self.account = account
        self.company = company
        self.language = language
        self.base_url = f"https://{account}.jobs.personio.de"
        self.source_info = SourceInfo(
            key=f"personio:{account}",
            name=f"Personio job feed ({account})",
            acquisition="official_public_feed",
            official=True,
            documentation_url=(
                "https://support.personio.de/hc/en-us/search?query=XML%20feed%20job%20advertisements"
            ),
        )
        self._http = HTTPTransport(
            self.base_url,
            client=client,
            timeout=timeout,
            retry=retry,
            default_headers={"Accept": "application/xml,text/xml"},
        )

    def close(self) -> None:
        self._http.close()

    def iter_jobs(self, query: JobQuery | None = None) -> Iterator[RawJobRecord]:
        response = self._http.request("GET", "/xml", params={"language": self.language})
        try:
            root = ElementTree.fromstring(response.content)
        except ElementTree.ParseError as exc:
            raise ValueError("Personio response is not valid XML") from exc

        for position in root.iter():
            if _local_name(position.tag) != "position":
                continue
            record = _normalize_personio(
                self.source_info.key,
                self.base_url,
                self.language,
                self.company,
                position,
            )
            if record is not None and local_query_match(record, query):
                yield record


def _normalize_personio(
    source: str,
    base_url: str,
    language: str,
    configured_company: str | None,
    position: ElementTree.Element,
) -> RawJobRecord | None:
    job_id = _child_text(position, "id")
    title = _child_text(position, "name")
    if not job_id or not title:
        return None

    locations: list[str] = []
    office = _child_text(position, "office")
    if office:
        locations.append(office)
    for node in position.iter():
        if _local_name(node.tag) != "additionalOffices":
            continue
        for child in node:
            value = _text(child.text)
            if _local_name(child.tag) == "office" and value and value not in locations:
                locations.append(value)

    description_parts: list[str] = []
    for node in position.iter():
        if _local_name(node.tag) != "jobDescription":
            continue
        heading = _child_text(node, "name")
        value = _child_text(node, "value")
        if value:
            description_parts.append(f"<h2>{heading}</h2>{value}" if heading else value)
    description = "\n".join(description_parts) or None

    employment_types: list[str] = []
    for field_name in ("employmentType", "schedule"):
        value = _child_text(position, field_name)
        if value and value not in employment_types:
            employment_types.append(value)

    query = urlencode({"language": language})
    canonical_url = f"{base_url}/job/{job_id}?{query}"
    remote = (
        True
        if any(
            term in location.casefold()
            for location in locations
            for term in ("remote", "homeoffice", "home office")
        )
        else None
    )

    return RawJobRecord(
        source=source,
        source_job_id=job_id,
        title=title,
        company=configured_company or _child_text(position, "subcompany"),
        locations=tuple(locations),
        description=description,
        description_format="html" if description else None,
        canonical_url=canonical_url,
        apply_url=canonical_url,
        published_at=parse_datetime(_child_text(position, "createdAt")),
        employment_types=tuple(employment_types),
        remote=remote,
        metadata={
            "department": _child_text(position, "department"),
            "recruiting_category": _child_text(position, "recruitingCategory"),
            "seniority": _child_text(position, "seniority"),
            "years_of_experience": _child_text(position, "yearsOfExperience"),
            "occupation": _child_text(position, "occupation"),
            "language": language,
        },
        raw={"xml": ElementTree.tostring(position, encoding="unicode")},
    )


def _child_text(element: ElementTree.Element, name: str) -> str | None:
    for child in element:
        if _local_name(child.tag) == name:
            return _text(child.text)
    return None


def _local_name(tag: Any) -> str:
    value = str(tag)
    return value.rsplit("}", 1)[-1]


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
