"""Connector for tenant-specific, server-rendered JOIN company boards."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping
from html.parser import HTMLParser
from typing import Any
from urllib.parse import quote

import httpx

from .base import JobQuery, RawJobRecord, SourceInfo, local_query_match, parse_datetime
from .http import HTTPTransport, RetryPolicy, TimeoutOptions

_TENANT_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")


class JoinConnector:
    BASE_URL = "https://join.com"

    def __init__(
        self,
        tenant: str,
        *,
        company: str | None = None,
        client: httpx.Client | None = None,
        timeout: TimeoutOptions | None = None,
        retry: RetryPolicy | None = None,
    ) -> None:
        tenant = tenant.strip().casefold()
        if not _TENANT_PATTERN.fullmatch(tenant):
            raise ValueError("JOIN tenant must be a valid company slug")
        self.tenant = tenant
        self.company = company.strip() if company and company.strip() else None
        self.source_info = SourceInfo(
            key=f"join:{tenant}",
            name=f"JOIN company board ({tenant})",
            acquisition="public_server_rendered_board",
            official=True,
            documentation_url="https://join.com",
        )
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
        response = self._http.request("GET", f"/companies/{quote(self.tenant, safe='')}")
        parser = _NextDataParser()
        parser.feed(response.text)
        parser.close()
        if parser.payload is None:
            raise ValueError("JOIN response has no __NEXT_DATA__ payload")
        company, jobs = _extract_state(parser.payload)
        company_name = self.company or _text(company.get("name"))
        company_domain = _text(company.get("domain"))
        if company_domain and company_domain.casefold() != self.tenant:
            raise ValueError("JOIN response belongs to a different tenant")
        for job in jobs:
            record = _normalize_join(self.source_info.key, self.tenant, company_name, job)
            if record is not None and local_query_match(record, query):
                yield record


def _extract_state(payload: Any) -> tuple[Mapping[str, Any], list[Mapping[str, Any]]]:
    if not isinstance(payload, Mapping):
        raise TypeError("JOIN __NEXT_DATA__ payload must be an object")
    props = payload.get("props")
    page_props = props.get("pageProps") if isinstance(props, Mapping) else None
    state = page_props.get("initialState") if isinstance(page_props, Mapping) else None
    if not isinstance(state, Mapping):
        raise ValueError("JOIN __NEXT_DATA__ has no initialState")
    company = state.get("company")
    jobs_state = state.get("jobs")
    items = jobs_state.get("items") if isinstance(jobs_state, Mapping) else None
    if not isinstance(company, Mapping) or not isinstance(items, list):
        raise ValueError("JOIN initialState has no usable company/job collection")
    return company, [item for item in items if isinstance(item, Mapping)]


def _normalize_join(
    source: str, tenant: str, company: str | None, job: Mapping[str, Any]
) -> RawJobRecord | None:
    job_id = _text(job.get("id"))
    title = _text(job.get("title"))
    id_param = _text(job.get("idParam")) or job_id
    if not job_id or not title or not id_param:
        return None
    city = job.get("city")
    location = None
    if isinstance(city, Mapping):
        location = ", ".join(
            part for part in (_text(city.get("cityName")), _text(city.get("countryName"))) if part
        ) or None
    canonical_url = f"https://join.com/companies/{tenant}/{quote(id_param, safe='-')}"
    employment = job.get("employmentType")
    employment_name = _text(employment.get("name")) if isinstance(employment, Mapping) else None
    workplace = _text(job.get("workplaceType"))
    remote = True if workplace and workplace.casefold() == "remote" else None
    return RawJobRecord(
        source=source,
        source_job_id=job_id,
        title=title,
        company=company,
        locations=(location,) if location else (),
        canonical_url=canonical_url,
        apply_url=canonical_url,
        published_at=parse_datetime(job.get("createdAt")),
        updated_at=parse_datetime(job.get("updatedAt")),
        employment_types=(employment_name,) if employment_name else (),
        remote=remote,
        metadata={"workplace_type": workplace} if workplace else {},
        raw=dict(job),
    )


class _NextDataParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.payload: Any = None
        self._capture = False
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "script" and values.get("id") == "__NEXT_DATA__":
            self._capture = True

    def handle_data(self, data: str) -> None:
        if self._capture:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag != "script" or not self._capture:
            return
        self._capture = False
        try:
            self.payload = json.loads("".join(self._parts))
        except json.JSONDecodeError as exc:
            raise ValueError("JOIN __NEXT_DATA__ is not valid JSON") from exc


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
