"""Connector for Recruitee's public tenant Offers API."""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from typing import Any
from urllib.parse import quote, urlparse

import httpx

from .base import (
    JobQuery,
    RawJobRecord,
    SourceInfo,
    local_query_match,
    parse_datetime,
    string_tuple,
)
from .http import HTTPTransport, RetryPolicy, TimeoutOptions

_TENANT_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


class RecruiteeJobBoardConnector:
    """Read one employer's public Recruitee careers API."""

    MAX_PAGE_SIZE = 100
    MAX_PAGES = 100

    def __init__(
        self,
        tenant: str,
        *,
        company: str | None = None,
        client: httpx.Client | None = None,
        timeout: TimeoutOptions | None = None,
        retry: RetryPolicy | None = None,
    ) -> None:
        self.tenant = _tenant_name(tenant, "recruitee.com")
        self.company = company
        self.base_url = f"https://{self.tenant}.recruitee.com"
        self.source_info = SourceInfo(
            key=f"recruitee:{self.tenant}",
            name=f"Recruitee job board ({self.tenant})",
            acquisition="official_public_api",
            official=True,
            documentation_url="https://support.recruitee.com/en/articles/1066281-integrate-recruitee-with-your-website",
        )
        self._http = HTTPTransport(
            self.base_url,
            client=client,
            timeout=timeout,
            retry=retry,
            default_headers={"Accept": "application/json"},
        )

    def close(self) -> None:
        self._http.close()

    def iter_jobs(self, query: JobQuery | None = None) -> Iterator[RawJobRecord]:
        query = query or JobQuery()
        page_size = min(query.pagination.page_size, self.MAX_PAGE_SIZE)
        page = query.pagination.start_page
        page_limit = min(query.pagination.max_pages or self.MAX_PAGES, self.MAX_PAGES)
        seen_ids: set[str] = set()

        for _ in range(page_limit):
            payload = self._http.request_json(
                "GET", "/api/offers", params={"page": page, "limit": page_size}
            )
            offers = _offers(payload)
            if not offers:
                return

            new_on_page = 0
            for offer in offers:
                record = _normalize_recruitee(
                    self.source_info.key, self.company, self.base_url, offer
                )
                if record is None or record.source_job_id in seen_ids:
                    continue
                seen_ids.add(record.source_job_id)
                new_on_page += 1
                if local_query_match(record, query):
                    yield record

            if new_on_page == 0 or len(offers) < page_size:
                return
            page += 1


def _tenant_name(value: str, provider_host: str) -> str:
    candidate = value.strip().casefold()
    if "://" in candidate:
        parsed = urlparse(candidate)
        if (
            parsed.scheme != "https"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError(f"tenant URL must be an HTTPS {provider_host} origin")
        suffix = f".{provider_host}"
        if not parsed.hostname or not parsed.hostname.endswith(suffix):
            raise ValueError(f"tenant URL must use a {provider_host} subdomain")
        candidate = parsed.hostname[: -len(suffix)]
    if not _TENANT_PATTERN.fullmatch(candidate):
        raise ValueError(f"tenant must be a valid {provider_host} subdomain")
    return candidate


def _offers(payload: Any) -> list[Mapping[str, Any]]:
    if not isinstance(payload, Mapping):
        raise TypeError("Recruitee response must be a JSON object")
    offers = payload.get("offers")
    if not isinstance(offers, list):
        raise TypeError("Recruitee response must contain an offers array")
    return [offer for offer in offers if isinstance(offer, Mapping)]


def _normalize_recruitee(
    source: str,
    configured_company: str | None,
    base_url: str,
    offer: Mapping[str, Any],
) -> RawJobRecord | None:
    job_id = _text(offer.get("id")) or _text(offer.get("slug"))
    title = _text(offer.get("title"))
    if not job_id or not title:
        return None

    locations: list[str] = list(string_tuple(offer.get("location")))
    for location in string_tuple(offer.get("locations")):
        if location not in locations:
            locations.append(location)
    remote_value = offer.get("remote")
    remote = remote_value if isinstance(remote_value, bool) else None
    if remote is None and any("remote" in value.casefold() for value in locations):
        remote = True

    careers_url = _provider_url(
        offer.get("careers_url") or offer.get("careersUrl"), base_url
    )
    if careers_url is None:
        slug = _text(offer.get("slug"))
        if slug:
            careers_url = f"{base_url}/o/{quote(slug, safe='')}"
    description = _text(
        offer.get("description")
        or offer.get("description_html")
        or offer.get("descriptionHtml")
    )
    employment = offer.get("employment_type") or offer.get("employmentType")
    company = configured_company or _text(offer.get("company_name"))

    return RawJobRecord(
        source=source,
        source_job_id=job_id,
        title=title,
        company=company,
        locations=tuple(locations),
        description=description,
        description_format="html" if description else None,
        canonical_url=careers_url,
        apply_url=_provider_url(
            offer.get("apply_url") or offer.get("applyUrl"), base_url
        )
        or careers_url,
        published_at=parse_datetime(offer.get("published_at") or offer.get("publishedAt")),
        updated_at=parse_datetime(offer.get("updated_at") or offer.get("updatedAt")),
        employment_types=string_tuple(employment),
        remote=remote,
        salary=_text(offer.get("salary")),
        metadata={
            "department": offer.get("department"),
            "category": offer.get("category"),
            "experience_code": offer.get("experience_code"),
        },
        raw=dict(offer),
    )


def _provider_url(value: Any, base_url: str) -> str | None:
    text = _text(value)
    if not text:
        return None
    parsed = urlparse(text)
    if (
        parsed.scheme != "https"
        or parsed.hostname != urlparse(base_url).hostname
        or parsed.username
        or parsed.password
        or parsed.port is not None
    ):
        return None
    return text


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
