"""Minimal Firecrawl API v2 client.

No crawl is started implicitly. ``start_crawl`` is a state-changing call and is
therefore not automatically retried; status-page GETs use the shared retry
policy. ``iter_crawl_documents`` follows result pagination but does not poll an
unfinished crawl.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from .http import HTTPTransport, RetryPolicy, TimeoutOptions


@dataclass(frozen=True, slots=True)
class FirecrawlDocument:
    url: str | None
    markdown: str | None
    html: str | None
    metadata: Mapping[str, Any]
    raw: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class FirecrawlJobStatus:
    job_id: str
    status: str
    documents: tuple[FirecrawlDocument, ...]
    next_url: str | None
    completed: int | None
    total: int | None
    raw: Mapping[str, Any]


class FirecrawlV2Client:
    BASE_URL = "https://api.firecrawl.dev/v2"

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str = BASE_URL,
        client: httpx.Client | None = None,
        timeout: TimeoutOptions | None = None,
        retry: RetryPolicy | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        normalized_base_url = base_url.strip().rstrip("/")
        if not normalized_base_url:
            raise ValueError("Firecrawl base_url must not be empty")
        if not normalized_base_url.endswith("/v2"):
            normalized_base_url = f"{normalized_base_url}/v2"
        self._ready_url = normalized_base_url.removesuffix("/v2")
        self._sleeper = sleeper
        self._monotonic = monotonic
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if api_key and api_key.strip():
            headers["Authorization"] = f"Bearer {api_key.strip()}"
        self._http = HTTPTransport(
            normalized_base_url,
            client=client,
            timeout=timeout,
            retry=retry,
            default_headers=headers,
            sleeper=sleeper,
        )

    def close(self) -> None:
        self._http.close()

    def wait_until_ready(
        self,
        *,
        timeout_seconds: float = 60.0,
        poll_interval_seconds: float = 1.0,
    ) -> None:
        """Wait until the Firecrawl HTTP service accepts connections."""

        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be greater than zero")

        deadline = self._monotonic() + timeout_seconds
        while True:
            try:
                response = self._http.request("GET", self._ready_url, retryable=False)
            except httpx.HTTPStatusError as exc:
                # Any HTTP response proves the API process is listening. Authentication
                # and endpoint errors remain the responsibility of the actual v2 call.
                exc.response.close()
                return
            except httpx.TransportError:
                remaining = deadline - self._monotonic()
                if remaining <= 0:
                    raise
                self._sleeper(min(poll_interval_seconds, remaining))
                continue
            response.close()
            return

    def scrape(
        self,
        url: str,
        *,
        formats: tuple[str, ...] = ("markdown",),
        only_main_content: bool = True,
        options: Mapping[str, Any] | None = None,
    ) -> FirecrawlDocument:
        body = dict(options or {})
        body.update(
            {
                "url": url,
                "formats": list(formats),
                "onlyMainContent": only_main_content,
            }
        )
        payload = self._http.request_json("POST", "/scrape", json=body, retryable=False)
        if not isinstance(payload, Mapping):
            raise TypeError("Firecrawl scrape response must be a JSON object")
        data = payload.get("data") if isinstance(payload.get("data"), Mapping) else payload
        return _document(data)

    def search(
        self,
        query: str,
        *,
        include_domains: tuple[str, ...],
        limit: int = 10,
        page: int = 1,
        country: str = "DE",
        location: str | None = None,
    ) -> tuple[FirecrawlDocument, ...]:
        """Discover one result window anchored to the requested domains."""

        normalized_query = query.strip()
        if not normalized_query:
            raise ValueError("Firecrawl search query must not be empty")
        domains = tuple(dict.fromkeys(domain.strip().casefold() for domain in include_domains))
        if not domains or any(not domain for domain in domains):
            raise ValueError("Firecrawl search requires non-empty include domains")
        if not 1 <= limit <= 100:
            raise ValueError("Firecrawl search limit must be between 1 and 100")
        if page < 1:
            raise ValueError("Firecrawl search page must be at least 1")
        body: dict[str, Any] = {
            "query": normalized_query,
            "limit": limit,
            "page": page,
            "sources": ["web"],
            "includeDomains": list(domains),
            "country": country,
        }
        if location and location.strip():
            body["location"] = location.strip()
        payload = self._http.request_json("POST", "/search", json=body, retryable=False)
        if not isinstance(payload, Mapping):
            raise TypeError("Firecrawl search response must be a JSON object")
        data = payload.get("data")
        web = data.get("web") if isinstance(data, Mapping) else None
        if not isinstance(web, list):
            return ()
        return tuple(_search_document(item) for item in web if isinstance(item, Mapping))




    def start_crawl(
        self,
        url: str,
        *,
        limit: int | None = None,
        scrape_options: Mapping[str, Any] | None = None,
        options: Mapping[str, Any] | None = None,
    ) -> FirecrawlJobStatus:
        body = dict(options or {})
        body["url"] = url
        if limit is not None:
            if limit < 1:
                raise ValueError("Firecrawl crawl limit must be at least 1")
            body["limit"] = limit
        if scrape_options:
            body["scrapeOptions"] = dict(scrape_options)
        payload = self._http.request_json("POST", "/crawl", json=body, retryable=False)
        if not isinstance(payload, Mapping):
            raise TypeError("Firecrawl crawl response must be a JSON object")
        job_id = str(payload.get("id") or "").strip()
        if not job_id:
            raise ValueError("Firecrawl crawl response did not contain an id")
        return _crawl_status(job_id, payload)

    def get_crawl(
        self,
        job_id: str,
        *,
        page_url: str | None = None,
    ) -> FirecrawlJobStatus:
        if not job_id.strip():
            raise ValueError("Firecrawl job_id must not be empty")
        path = page_url or f"/crawl/{quote(job_id, safe='')}"
        payload = self._http.request_json("GET", path)
        if not isinstance(payload, Mapping):
            raise TypeError("Firecrawl crawl status response must be a JSON object")
        return _crawl_status(job_id, payload)

    def iter_crawl_documents(self, job_id: str) -> Iterator[FirecrawlDocument]:
        """Yield all currently available result pages without polling status."""

        page_url: str | None = None
        seen: set[str] = set()
        while True:
            status = self.get_crawl(job_id, page_url=page_url)
            yield from status.documents
            page_url = status.next_url
            if not page_url:
                return
            if page_url in seen:
                raise RuntimeError("Firecrawl pagination returned a repeated next URL")
            seen.add(page_url)


def _document(value: Any) -> FirecrawlDocument:
    if not isinstance(value, Mapping):
        raise TypeError("Firecrawl document must be a JSON object")
    metadata = value.get("metadata") if isinstance(value.get("metadata"), Mapping) else {}
    url = metadata.get("sourceURL") or metadata.get("url") or value.get("url")
    return FirecrawlDocument(
        url=str(url) if url else None,
        markdown=str(value["markdown"]) if value.get("markdown") is not None else None,
        html=str(value["html"]) if value.get("html") is not None else None,
        metadata=dict(metadata),
        raw=dict(value),
    )


def _search_document(value: Mapping[str, Any]) -> FirecrawlDocument:
    metadata = dict(value.get("metadata") if isinstance(value.get("metadata"), Mapping) else {})
    url = metadata.get("sourceURL") or metadata.get("url") or value.get("url")
    title = value.get("title")
    description = value.get("description")
    markdown = value.get("markdown") or description
    if title:
        metadata.setdefault("jobTitle", str(title))
    if description:
        metadata.setdefault("searchDescription", str(description))
    metadata["firecrawlSearchResult"] = True
    return FirecrawlDocument(
        url=str(url) if url else None,
        markdown=str(markdown) if markdown is not None else None,
        html=str(value["html"]) if value.get("html") is not None else None,
        metadata=metadata,
        raw=dict(value),
    )





def _crawl_status(job_id: str, payload: Mapping[str, Any]) -> FirecrawlJobStatus:
    data = payload.get("data")
    values = data if isinstance(data, list) else []
    documents = tuple(_document(value) for value in values if isinstance(value, Mapping))
    return FirecrawlJobStatus(
        job_id=job_id,
        status=str(payload.get("status") or "queued"),
        documents=documents,
        next_url=str(payload["next"]) if payload.get("next") else None,
        completed=_optional_int(payload.get("completed")),
        total=_optional_int(payload.get("total")),
        raw=dict(payload),
    )


def _optional_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
