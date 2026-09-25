"""Job connector backed by an explicitly configured self-hosted Firecrawl v2.

The connector is lazy: importing or constructing it performs no HTTP work and
does not even construct ``FirecrawlV2Client``.  A client is created only when
``iter_jobs`` sees at least one configured target.  This lets the source stay
enabled as a prepared source without turning an empty configuration into a
network call.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from html import unescape
from html.parser import HTMLParser
from typing import Any, Protocol
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlsplit, urlunsplit

from .base import (
    JobQuery,
    RawJobRecord,
    SourceInfo,
    local_query_match,
    parse_datetime,
    string_tuple,
)
from .firecrawl import FirecrawlDocument, FirecrawlJobStatus, FirecrawlV2Client
from .public_url import PreparedPublicURL, prepare_public_url

SELF_HOSTED_BASE_URL = "http://firecrawl-api:3002/v2"
TERMINAL_SUCCESS = frozenset({"completed", "complete", "done"})
TERMINAL_FAILURE = frozenset({"failed", "cancelled", "canceled", "error"})
READINESS_TIMEOUT_SECONDS = 60.0


class FirecrawlJobsError(RuntimeError):
    pass


class FirecrawlJobsTimeout(FirecrawlJobsError):
    pass


class FirecrawlReadinessError(FirecrawlJobsError):
    pass


class FirecrawlClientLike(Protocol):
    def wait_until_ready(
        self,
        *,
        timeout_seconds: float = READINESS_TIMEOUT_SECONDS,
        poll_interval_seconds: float = 1.0,
    ) -> None: ...

    def scrape(
        self,
        url: str,
        *,
        formats: tuple[str, ...] = ("markdown",),
        only_main_content: bool = True,
        options: Mapping[str, Any] | None = None,
    ) -> FirecrawlDocument: ...


    def search(
        self,
        query: str,
        *,
        include_domains: tuple[str, ...],
        limit: int = 10,
        page: int = 1,
        country: str = "DE",
        location: str | None = None,
    ) -> tuple[FirecrawlDocument, ...]: ...

    def start_crawl(
        self,
        url: str,
        *,
        limit: int | None = None,
        scrape_options: Mapping[str, Any] | None = None,
        options: Mapping[str, Any] | None = None,
    ) -> FirecrawlJobStatus: ...

    def get_crawl(
        self,
        job_id: str,
        *,
        page_url: str | None = None,
    ) -> FirecrawlJobStatus: ...

    def close(self) -> None: ...


ClientFactory = Callable[..., FirecrawlClientLike]
TargetURLPreparer = Callable[[str], PreparedPublicURL]


@dataclass(frozen=True, slots=True)
class FirecrawlTarget:
    url: str
    mode: str = "crawl"
    label: str | None = None
    target_kind: str | None = None
    company: str | None = None
    default_location: str | None = None
    limit: int | None = None
    poll_interval_seconds: float | None = None
    timeout_seconds: float | None = None
    accept_all_pages: bool = False
    formats: tuple[str, ...] = ("markdown", "html")
    scrape_options: Mapping[str, Any] = field(default_factory=dict)
    crawl_options: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.url.strip():
            raise ValueError("Firecrawl target URL must not be empty")
        split = urlsplit(self.url.strip())
        if split.scheme.casefold() not in {"http", "https"} or not split.netloc:
            raise ValueError("Firecrawl target URL must be an absolute HTTP(S) URL")
        if split.username or split.password:
            raise ValueError("Firecrawl target credentials must not be embedded in the URL")
        if self.mode not in {"crawl", "scrape"}:
            raise ValueError("Firecrawl target mode must be 'crawl' or 'scrape'")
        if self.target_kind not in {None, "job_portal", "company_site"}:
            raise ValueError("Firecrawl target kind must be 'job_portal' or 'company_site'")
        if self.limit is not None and self.limit < 1:
            raise ValueError("Firecrawl target limit must be at least 1")
        if self.poll_interval_seconds is not None and self.poll_interval_seconds <= 0:
            raise ValueError("Firecrawl poll interval must be greater than zero")
        if self.timeout_seconds is not None and self.timeout_seconds <= 0:
            raise ValueError("Firecrawl timeout must be greater than zero")
        if not self.formats:
            raise ValueError("Firecrawl formats must not be empty")

@dataclass(frozen=True, slots=True)
class _PortalCandidate:
    search_document: FirecrawlDocument
    url: str
    configured_target: FirecrawlTarget
    prepared_target: FirecrawlTarget
    external: bool = False


class _JsonLdParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.documents: list[str] = []
        self._capturing = False
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() != "script":
            return
        attributes = {key.casefold(): (value or "") for key, value in attrs}
        if "ld+json" in attributes.get("type", "").casefold():
            self._capturing = True
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._capturing:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "script" and self._capturing:
            self.documents.append("".join(self._parts))
            self._capturing = False
            self._parts = []


class _HrefParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() != "a":
            return
        attributes = {key.casefold(): value for key, value in attrs}
        if href := attributes.get("href"):
            self.links.append(href)


class FirecrawlJobsConnector:
    """Turn configured Firecrawl pages into traceable, broad job candidates."""

    source_info = SourceInfo(
        key="firecrawl_self_hosted",
        name="Firecrawl (selbst gehostet)",
        acquisition="self_hosted_crawler",
        official=False,
        default_enabled=True,
        experimental=False,
        documentation_url="https://docs.firecrawl.dev/contributing/self-host",
        notes="Core scrape/crawl only; no Firecrawl Cloud or Fire-engine dependency.",
    )

    def __init__(
        self,
        targets: tuple[FirecrawlTarget, ...] = (),
        *,
        base_url: str = SELF_HOSTED_BASE_URL,
        api_key: str | None = None,
        poll_interval_seconds: float = 2.0,
        timeout_seconds: float = 300.0,
        default_crawl_limit: int | None = None,
        continue_on_target_error: bool = True,
        portal_search_enabled: bool = False,
        portal_search_limit: int = 100,
        portal_search_accept_external_results: bool = False,
        client: FirecrawlClientLike | None = None,
        client_factory: ClientFactory | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        target_url_preparer: TargetURLPreparer = prepare_public_url,
    ) -> None:
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be greater than zero")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")
        if default_crawl_limit is not None and default_crawl_limit < 1:
            raise ValueError("default_crawl_limit must be at least 1")
        if not 1 <= portal_search_limit <= 100:
            raise ValueError("portal_search_limit must be between 1 and 100")
        self.targets = _deduplicate_targets(targets)
        self.base_url = _v2_base_url(base_url)
        self.api_key = api_key.strip() if api_key and api_key.strip() else None
        self.poll_interval_seconds = poll_interval_seconds
        self.timeout_seconds = timeout_seconds
        self.default_crawl_limit = default_crawl_limit
        self.continue_on_target_error = continue_on_target_error
        self.portal_search_enabled = portal_search_enabled
        self.portal_search_limit = portal_search_limit
        self.portal_search_accept_external_results = portal_search_accept_external_results
        self._client = client
        self._client_factory = client_factory
        self._sleeper = sleeper
        self._monotonic = monotonic
        self._target_url_preparer = target_url_preparer
        self._client_created = client is not None
        self._diagnostics: dict[str, Any] = self._initial_diagnostics()
        self._run_started = False
        self._client_ready = False
        self._emitted_keys: set[str] = set()
        self._started_target_urls: set[str] = set()
        self._completed_target_urls: set[str] = set()
        self._seen_portal_detail_urls: set[str] = set()
        self._seen_portal_listing_urls: set[str] = set()
        self._prepared_portal_targets: tuple[
            tuple[FirecrawlTarget, FirecrawlTarget], ...
        ] | None = None

    @classmethod
    def from_source(
        cls,
        source: Any,
        *,
        environment: Mapping[str, str] | None = None,
        client_factory: ClientFactory | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        target_url_preparer: TargetURLPreparer = prepare_public_url,
    ) -> FirecrawlJobsConnector:
        """Build lazily from ``Source.base_url`` and ``Source.metadata_json``."""

        env = environment if environment is not None else os.environ
        metadata = source.metadata_json if isinstance(source.metadata_json, Mapping) else {}
        nested = metadata.get("firecrawl")
        config = nested if isinstance(nested, Mapping) else {}
        portal_search = _mapping(config.get("portal_search"))
        raw_targets = config.get(
            "targets", metadata.get("targets", metadata.get("target_urls", ()))
        )
        defaults = {
            "limit": _optional_int(config.get("crawl_limit")),
            "poll_interval_seconds": _optional_float(config.get("poll_interval_seconds")),
            "timeout_seconds": _optional_float(config.get("timeout_seconds")),
            "company": _optional_text(config.get("company")),
            "default_location": _optional_text(config.get("default_location")),
            "accept_all_pages": _optional_bool(config.get("accept_all_pages"), default=False),
            "scrape_options": _mapping(config.get("scrape_options")),
            "crawl_options": _mapping(config.get("crawl_options")),
        }
        targets = _parse_targets(raw_targets, defaults=defaults)

        configured_key_env = _optional_text(config.get("api_key_env"))
        api_key = (
            (env.get(configured_key_env) if configured_key_env else None)
            or env.get("FIRECRAWL_API_KEY")
            or _optional_text(config.get("api_key"))
            or _optional_text(metadata.get("api_key"))
        )
        base_url = (
            env.get("FIRECRAWL_BASE_URL")
            or _optional_text(config.get("base_url"))
            or _optional_text(getattr(source, "base_url", None))
            or SELF_HOSTED_BASE_URL
        )
        configured_interval = _optional_float(config.get("poll_interval_seconds"))
        configured_timeout = _optional_float(config.get("timeout_seconds"))
        return cls(
            targets,
            base_url=base_url,
            api_key=api_key,
            poll_interval_seconds=(configured_interval if configured_interval is not None else 2.0),
            timeout_seconds=configured_timeout if configured_timeout is not None else 300.0,
            default_crawl_limit=(
                _optional_int(config.get("crawl_limit"))
                if config.get("crawl_limit") is not None
                else None
            ),
            continue_on_target_error=_optional_bool(
                config.get("continue_on_target_error"), default=True
            ),
            portal_search_enabled=_optional_bool(
                portal_search.get("enabled"), default=False
            ),
            portal_search_limit=_optional_int(portal_search.get("limit")) or 100,
            portal_search_accept_external_results=_optional_bool(
                portal_search.get("accept_external_results"), default=False
            ),
            client_factory=client_factory,
            sleeper=sleeper,
            monotonic=monotonic,
            target_url_preparer=target_url_preparer,
        )

    @property
    def client_created(self) -> bool:
        return self._client_created

    @property
    def run_diagnostics(self) -> dict[str, Any]:
        return dict(self._diagnostics)

    def iter_jobs(self, query: JobQuery | None = None) -> Iterator[RawJobRecord]:
        first_call = not self._run_started
        if first_call:
            self._diagnostics = self._initial_diagnostics()
            self._run_started = True
        if not self.targets:
            self._diagnostics.update(
                {
                    "skipped": True,
                    "skip_reason": "no_targets",
                    "message": "Keine Firecrawl-Zielseiten konfiguriert.",
                }
            )
            return

        portal_targets = tuple(
            target for target in self.targets if target.target_kind == "job_portal"
        )
        if self.portal_search_enabled and portal_targets and query and query.text:
            yield from self._search_portals(query, portal_targets)

        if not first_call:
            return
        direct_targets = tuple(
            target
            for target in self.targets
            if not (self.portal_search_enabled and target.target_kind == "job_portal")
        )
        yield from self._crawl_targets(
            direct_targets,
            None if self.portal_search_enabled else query,
        )

    def _search_portals(
        self,
        query: JobQuery,
        targets: tuple[FirecrawlTarget, ...],
    ) -> Iterator[RawJobRecord]:
        try:
            client, prepared_targets = self._prepare_search_targets(targets)
        except FirecrawlReadinessError as exc:
            self._record_target_error(targets[0], exc)
            return
        except Exception as exc:
            for target in targets:
                self._record_target_error(target, exc)
            if not self.continue_on_target_error:
                raise
            return

        search_text = f"({query.text}) (Job OR Stelle OR Stellenangebot)"
        if query.location:
            search_text = f'{search_text} "{query.location}"'
        if query.filters:
            search_text = f"{search_text} Remote"
        title_terms = _quoted_search_terms(query.text)
        for configured_target, prepared_target in prepared_targets:
            domain = _hostname(prepared_target.url)
            if domain is None:
                continue
            group = ((configured_target, prepared_target),)
            target_result = self._target_result(configured_target)
            seen_search_urls: set[str] = set()
            page = 1
            while True:
                self._diagnostics["search_requests"] += 1
                try:
                    documents = client.search(
                        search_text,
                        include_domains=(domain,),
                        limit=self.portal_search_limit,
                        page=page,
                        location=query.location,
                    )
                except Exception as exc:
                    if page == 1:
                        self._record_target_error(configured_target, exc)
                    else:
                        self._record_page_error(
                            None,
                            configured_target,
                            target_result,
                            exc,
                        )
                    if not self.continue_on_target_error:
                        raise
                    break
                new_documents: list[FirecrawlDocument] = []
                for document in documents:
                    url_key = _search_result_url_key(document.url)
                    if not url_key or url_key in seen_search_urls:
                        continue
                    seen_search_urls.add(url_key)
                    new_documents.append(document)
                if not new_documents:
                    break
                self._diagnostics["search_results_seen"] += len(new_documents)
                yield from self._portal_records_from_documents(
                    client,
                    tuple(new_documents),
                    group,
                    title_terms,
                )
                page += 1
            self._mark_target_completed(configured_target)

    def _portal_records_from_documents(
        self,
        client: FirecrawlClientLike,
        documents: tuple[FirecrawlDocument, ...],
        group: tuple[tuple[FirecrawlTarget, FirecrawlTarget], ...],
        title_terms: tuple[str, ...],
    ) -> Iterator[RawJobRecord]:
        candidates = self._portal_candidates(client, documents, group)
        for candidate in candidates:
            self._seen_portal_detail_urls.add(candidate.url)
            target_result = self._target_result(candidate.configured_target)
            try:
                detail = client.scrape(
                    candidate.url,
                    formats=("markdown", "html"),
                    only_main_content=True,
                )
                search_metadata = dict(candidate.search_document.metadata)
                detail_metadata = dict(detail.metadata)
                detailed_document = replace(
                    detail,
                    url=candidate.url,
                    metadata={
                        **search_metadata,
                        **detail_metadata,
                        "jobTitle": _first(detail_metadata, "jobTitle", "title")
                        or _first(search_metadata, "jobTitle", "title"),
                        "firecrawlSearchResult": True,
                    },
                    raw={
                        "search": dict(candidate.search_document.raw),
                        "scrape": dict(detail.raw),
                    },
                )
                record = self._record_from_document(
                    detailed_document,
                    candidate.prepared_target,
                    target_result,
                    query=None,
                    title_terms=title_terms,
                    canonical_url_override=candidate.url,
                )
                if record is not None and candidate.external:
                    record = replace(
                        record,
                        metadata={
                            **dict(record.metadata),
                            "firecrawl_discovered_domain": _hostname(
                                record.canonical_url or ""
                            ),
                            "firecrawl_search_origin_target_url": (
                                candidate.configured_target.url
                            ),
                        },
                    )
                    domain = _hostname(record.canonical_url or "")
                    self._diagnostics["external_records_emitted"] += 1
                    target_result["external_records_emitted"] = (
                        target_result.get("external_records_emitted", 0) + 1
                    )
                    if domain:
                        if domain not in self._diagnostics["discovered_portal_domains"]:
                            self._diagnostics["discovered_portal_domains"].append(domain)
                        target_domains = target_result.setdefault(
                            "discovered_portal_domains", []
                        )
                        if domain not in target_domains:
                            target_domains.append(domain)
            except Exception as exc:
                self._record_page_error(
                    candidate.url,
                    candidate.configured_target,
                    target_result,
                    exc,
                )
                continue
            if record is not None:
                yield record

    def _portal_candidates(
        self,
        client: FirecrawlClientLike,
        documents: tuple[FirecrawlDocument, ...],
        group: tuple[tuple[FirecrawlTarget, FirecrawlTarget], ...],
    ) -> tuple[_PortalCandidate, ...]:
        candidates: list[_PortalCandidate] = []
        candidate_urls: set[str] = set()
        listing_targets: set[str] = set()
        search_target, search_prepared_target = group[0]

        for document in documents:
            pair = _matching_target(document.url, group)
            configured = pair[0] if pair is not None else search_target
            prepared_target = pair[1] if pair is not None else search_prepared_target
            target_result = self._target_result(configured)
            try:
                safe_url = self._target_url_preparer(document.url or "").final_url
                final_pair = _matching_target(safe_url, group)
                external = final_pair is None
                if external:
                    if not self.portal_search_accept_external_results:
                        self._diagnostics["pages_rejected"] += 1
                        target_result["pages_rejected"] += 1
                        continue
                    configured = search_target
                    prepared_target = _discovered_portal_target(
                        safe_url, search_prepared_target
                    )
                    target_result = self._target_result(configured)
                else:
                    configured, prepared_target = final_pair
                    target_result = self._target_result(configured)
                if _strong_job_like_url(safe_url):
                    if (
                        safe_url not in candidate_urls
                        and safe_url not in self._seen_portal_detail_urls
                    ):
                        candidates.append(
                            _PortalCandidate(
                                document,
                                safe_url,
                                configured,
                                prepared_target,
                                external,
                            )
                        )
                        candidate_urls.add(safe_url)
                    continue
                if safe_url in self._seen_portal_listing_urls:
                    self._diagnostics["duplicates_skipped"] += 1
                    target_result["duplicates_skipped"] += 1
                    continue
                listing_key = _hostname(safe_url) or safe_url
                if listing_key in listing_targets:
                    self._diagnostics["pages_rejected"] += 1
                    target_result["pages_rejected"] += 1
                    continue
                listing_targets.add(listing_key)
                self._seen_portal_listing_urls.add(safe_url)
                listing = client.scrape(
                    safe_url,
                    formats=("markdown", "html"),
                    only_main_content=True,
                )
                self._diagnostics["documents_seen"] += 1
                target_result["documents_seen"] += 1
                search_document = replace(
                    document,
                    raw={
                        "search": dict(document.raw),
                        "listing": dict(listing.raw),
                    },
                )
                discovered = 0
                for link in _document_links(listing):
                    if not _strong_job_like_url(link):
                        continue
                    detail_url = self._target_url_preparer(link).final_url
                    detail_pair = _matching_target(detail_url, group)
                    detail_external = detail_pair is None
                    if detail_external:
                        if not self.portal_search_accept_external_results:
                            continue
                        detail_configured = search_target
                        detail_prepared = _discovered_portal_target(
                            detail_url, search_prepared_target
                        )
                    else:
                        detail_configured, detail_prepared = detail_pair
                    if (
                        detail_url in candidate_urls
                        or detail_url in self._seen_portal_detail_urls
                    ):
                        continue
                    candidates.append(
                        _PortalCandidate(
                            search_document,
                            detail_url,
                            detail_configured,
                            detail_prepared,
                            detail_external,
                        )
                    )
                    candidate_urls.add(detail_url)
                    discovered += 1
                if discovered == 0:
                    self._diagnostics["pages_rejected"] += 1
                    target_result["pages_rejected"] += 1
            except Exception as exc:
                self._record_page_error(document.url, configured, target_result, exc)
        return tuple(candidates)

    def _prepare_search_targets(
        self,
        targets: tuple[FirecrawlTarget, ...],
    ) -> tuple[
        FirecrawlClientLike,
        tuple[tuple[FirecrawlTarget, FirecrawlTarget], ...],
    ]:
        if self._prepared_portal_targets is not None:
            return self._get_client(), self._prepared_portal_targets
        prepared = tuple(
            (target, replace(target, url=self._target_url_preparer(target.url).final_url))
            for target in targets
        )
        client = self._get_client()
        if not self._client_ready:
            try:
                client.wait_until_ready(timeout_seconds=READINESS_TIMEOUT_SECONDS)
            except Exception as exc:
                raise FirecrawlReadinessError(str(exc)) from exc
            self._client_ready = True
            prepared = tuple(
                (target, replace(target, url=self._target_url_preparer(target.url).final_url))
                for target in targets
            )
        self._prepared_portal_targets = prepared
        return client, prepared

    def _crawl_targets(
        self,
        targets: tuple[FirecrawlTarget, ...],
        query: JobQuery | None,
    ) -> Iterator[RawJobRecord]:
        client: FirecrawlClientLike | None = None
        for configured_target in targets:
            target_result = self._target_result(configured_target)
            try:
                prepared_url = self._target_url_preparer(configured_target.url)
                if client is None:
                    client = self._get_client()
                    if not self._client_ready:
                        try:
                            client.wait_until_ready(timeout_seconds=READINESS_TIMEOUT_SECONDS)
                        except Exception as exc:
                            raise FirecrawlReadinessError(str(exc)) from exc
                        self._client_ready = True
                        prepared_url = self._target_url_preparer(configured_target.url)
                target = replace(configured_target, url=prepared_url.final_url)
                documents = (
                    (self._scrape_document(client, target),)
                    if target.mode == "scrape"
                    else self._crawl_documents(client, target)
                )
            except Exception as exc:
                self._record_target_error(configured_target, exc)
                if not self.continue_on_target_error:
                    raise
                if isinstance(exc, FirecrawlReadinessError):
                    return
                continue
            for document in documents:
                try:
                    record = self._record_from_document(
                        document,
                        target,
                        target_result,
                        query=query,
                    )
                except Exception as exc:
                    self._record_page_error(
                        document.url,
                        configured_target,
                        target_result,
                        exc,
                    )
                    continue
                if record is not None:
                    yield record
            self._mark_target_completed(configured_target)

    def _target_result(self, target: FirecrawlTarget) -> dict[str, Any]:
        for result in self._diagnostics["target_results"]:
            if result["target"] == target.url:
                return result
        result = {
            "target": target.url,
            "label": target.label,
            "target_kind": target.target_kind,
            "status": "running",
            "documents_seen": 0,
            "records_emitted": 0,
            "pages_rejected": 0,
            "duplicates_skipped": 0,
            "query_filtered": 0,
            "page_error_count": 0,
        }
        self._diagnostics["target_results"].append(result)
        if target.url not in self._started_target_urls:
            self._started_target_urls.add(target.url)
            self._diagnostics["targets_started"] += 1
        return result

    def _mark_target_completed(self, target: FirecrawlTarget) -> None:
        result = self._target_result(target)
        if result["status"] not in {"failed", "blocked", "rate_limited"}:
            if result["page_error_count"]:
                result["status"] = "partial" if result["records_emitted"] else "failed"
            elif result["records_emitted"]:
                result["status"] = "completed"
            elif result["documents_seen"]:
                result["status"] = "insufficient"
            else:
                result["status"] = "empty"
        if target.url not in self._completed_target_urls:
            self._completed_target_urls.add(target.url)
            self._diagnostics["targets_completed"] += 1

    def _record_target_error(self, target: FirecrawlTarget, exc: Exception) -> None:
        error = _safe_error(exc)
        target_result = self._target_result(target)
        target_result["status"] = _target_error_status(exc)
        target_result["error"] = error
        self._diagnostics["target_error_count"] += 1
        self._diagnostics["target_errors"].append(
            {"target": target.url, "label": target.label, "error": error}
        )

    def _record_page_error(
        self,
        document_url: str | None,
        target: FirecrawlTarget,
        target_result: dict[str, Any],
        exc: Exception,
    ) -> None:
        self._diagnostics["page_error_count"] += 1
        target_result["page_error_count"] += 1
        self._diagnostics["page_errors"].append(
            {
                "document_url": document_url,
                "target": target.url,
                "error": _safe_error(exc),
            }
        )

    def _record_from_document(
        self,
        document: FirecrawlDocument,
        target: FirecrawlTarget,
        target_result: dict[str, Any],
        *,
        query: JobQuery | None,
        title_terms: tuple[str, ...] = (),
        canonical_url_override: str | None = None,
    ) -> RawJobRecord | None:
        self._diagnostics["documents_seen"] += 1
        target_result["documents_seen"] += 1
        record = _normalize_document(
            document,
            target,
            canonical_url_override=canonical_url_override,
        )
        if record is None:
            self._diagnostics["pages_rejected"] += 1
            target_result["pages_rejected"] += 1
            return None
        if title_terms and not _title_matches_search_terms(record.title, title_terms):
            self._diagnostics["query_filtered"] += 1
            target_result["query_filtered"] += 1
            return None
        if not local_query_match(record, query):
            self._diagnostics["query_filtered"] += 1
            target_result["query_filtered"] += 1
            return None
        if record.source_job_id in self._emitted_keys:
            self._diagnostics["duplicates_skipped"] += 1
            target_result["duplicates_skipped"] += 1
            return None
        self._emitted_keys.add(record.source_job_id)
        self._diagnostics["records_emitted"] += 1
        target_result["records_emitted"] += 1
        return record

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def _initial_diagnostics(self) -> dict[str, Any]:
        return {
            "target_count": len(self.targets),
            "targets_started": 0,
            "targets_completed": 0,
            "documents_seen": 0,
            "search_requests": 0,
            "search_results_seen": 0,
            "records_emitted": 0,
            "pages_rejected": 0,
            "duplicates_skipped": 0,
            "query_filtered": 0,
            "external_records_emitted": 0,
            "discovered_portal_domains": [],
            "target_error_count": 0,
            "target_errors": [],
            "target_results": [],
            "page_error_count": 0,
            "page_errors": [],
            "skipped": False,
        }

    def _get_client(self) -> FirecrawlClientLike:
        if self._client is None:
            factory = self._client_factory or FirecrawlV2Client
            self._client = factory(api_key=self.api_key, base_url=self.base_url)
            self._client_created = True
        return self._client

    def _scrape_document(
        self, client: FirecrawlClientLike, target: FirecrawlTarget
    ) -> FirecrawlDocument:
        return client.scrape(
            target.url,
            formats=target.formats,
            only_main_content=True,
            options=target.scrape_options,
        )

    def _crawl_documents(
        self, client: FirecrawlClientLike, target: FirecrawlTarget
    ) -> tuple[FirecrawlDocument, ...]:
        status = client.start_crawl(
            target.url,
            limit=target.limit or self.default_crawl_limit,
            scrape_options={
                "formats": list(target.formats),
                "onlyMainContent": True,
                **dict(target.scrape_options),
            },
            options={**dict(target.crawl_options), "allowExternalLinks": False},
        )
        documents: dict[str, FirecrawlDocument] = {}
        _remember_documents(documents, status.documents)
        timeout = target.timeout_seconds or self.timeout_seconds
        interval = target.poll_interval_seconds or self.poll_interval_seconds
        deadline = self._monotonic() + timeout

        while _status_name(status) not in TERMINAL_SUCCESS:
            current = _status_name(status)
            if current in TERMINAL_FAILURE:
                raise FirecrawlJobsError(
                    f"Firecrawl crawl {status.job_id} ended with status {status.status!r}"
                )
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise FirecrawlJobsTimeout(
                    f"Firecrawl crawl {status.job_id} exceeded {timeout:g} seconds"
                )
            self._sleeper(min(interval, remaining))
            status = client.get_crawl(status.job_id)
            _remember_documents(documents, status.documents)

        next_url = status.next_url
        seen_pages: set[str] = set()
        while next_url:
            if next_url in seen_pages:
                raise FirecrawlJobsError("Firecrawl returned a repeated result page URL")
            seen_pages.add(next_url)
            page = client.get_crawl(status.job_id, page_url=next_url)
            if _status_name(page) in TERMINAL_FAILURE:
                raise FirecrawlJobsError(
                    f"Firecrawl result page ended with status {page.status!r}"
                )
            _remember_documents(documents, page.documents)
            next_url = page.next_url
        return tuple(documents.values())


def _search_result_url_key(url: str | None) -> str | None:
    raw = str(url or "").strip()
    if not raw:
        return None
    try:
        return _canonical_url(raw)
    except ValueError:
        return raw


def _quoted_search_terms(value: str | None) -> tuple[str, ...]:
    return tuple(
        term.strip() for term in re.findall(r'"([^"]+)"', value or "") if term.strip()
    )

def _title_matches_search_terms(title: str, terms: tuple[str, ...]) -> bool:
    title_tokens = set(re.findall(r"[a-z0-9äöüß]+", title.casefold()))
    ignored = {"and", "der", "des", "die", "für", "fuer", "of", "the", "to", "und", "zur", "zum"}
    return any(
        bool(significant := set(re.findall(r"[a-z0-9äöüß]+", term.casefold())) - ignored)
        and significant <= title_tokens
        for term in terms
    )




def _hostname(url: str) -> str | None:
    hostname = (urlsplit(url).hostname or "").strip().casefold().rstrip(".")
    return hostname or None


def _matching_target(
    document_url: str | None,
    targets: tuple[tuple[FirecrawlTarget, FirecrawlTarget], ...],
) -> tuple[FirecrawlTarget, FirecrawlTarget] | None:
    if not document_url or (document_host := _hostname(document_url)) is None:
        return None
    for pair in targets:
        target_host = _hostname(pair[1].url)
        if target_host is None:
            continue
        bare_target = target_host.removeprefix("www.")
        bare_document = document_host.removeprefix("www.")
        if bare_document == bare_target or bare_document.endswith(f".{bare_target}"):
            return pair
    return None


def _discovered_portal_target(url: str, template: FirecrawlTarget) -> FirecrawlTarget:
    split = urlsplit(url)
    origin = urlunsplit((split.scheme, split.netloc, "/", "", ""))
    return replace(
        template,
        url=origin,
        label=_hostname(url),
        company=None,
    )




def _normalize_document(
    document: FirecrawlDocument,
    target: FirecrawlTarget,
    *,
    canonical_url_override: str | None = None,
) -> RawJobRecord | None:
    json_ld = _job_posting_json_ld(document.html)
    metadata = dict(document.metadata or {})
    document_url = (
        document.url or _first(metadata, "sourceURL", "url", "canonicalUrl", "ogUrl") or target.url
    )
    url = _canonical_url(
        canonical_url_override
        or urljoin(str(document_url), str(_first(json_ld, "url") or document_url))
    )
    if (
        target.target_kind == "job_portal"
        and not target.accept_all_pages
        and not json_ld
        and url == _canonical_url(target.url)
    ):
        return None
    markdown = document.markdown or ""
    html = document.html or ""
    title = _optional_text(_first(json_ld, "title")) or _first_text(
        metadata, "jobTitle", "title", "ogTitle"
    )
    title = title or _markdown_title(markdown) or _title_from_url(url)
    evidence: list[str] = []
    if json_ld:
        evidence.append("json_ld_jobposting")
    if _job_like_url(url):
        evidence.append("job_like_url")
    signal_count = _job_signal_count(f"{title or ''} {markdown} {html}")
    if signal_count:
        evidence.append(f"content_signals:{signal_count}")
    structured_metadata = any(
        _first_text(metadata, key) for key in ("jobTitle", "company", "location", "employmentType")
    )
    if structured_metadata:
        evidence.append("structured_metadata")
    search_result = bool(metadata.get("firecrawlSearchResult"))
    if search_result:
        evidence.append("firecrawl_search")
    generic_title = _generic_title(title)
    portal_structured_metadata = bool(
        _first_text(metadata, "jobTitle")
        and _first_text(metadata, "company", "companyName")
    )
    portal_search_evidence = (
        search_result and "job_like_url" in evidence and signal_count >= 2
    )
    accepted = bool(json_ld) or (
        bool(title)
        and not generic_title
        and (
            (portal_structured_metadata or portal_search_evidence)
            if target.target_kind == "job_portal"
            else "job_like_url" in evidence or signal_count >= 2 or structured_metadata
        )
    )
    if target.accept_all_pages and title and not generic_title:
        accepted = accepted or signal_count >= 1 or structured_metadata
        if accepted:
            evidence.append("target_accept_all_pages")
    if not accepted or not title:
        return None

    company = _organization_name(json_ld.get("hiringOrganization") if json_ld else None)
    company = company or _first_text(metadata, "company", "companyName")
    company = company or target.company or _first_text(metadata, "ogSiteName")
    locations, address_metadata = _job_locations(json_ld, metadata, target)
    remote = _remote_value(json_ld, metadata, locations, f"{title} {markdown[:5000]}")
    strong_remote_basis = _strong_remote_basis(json_ld, metadata)
    employment_types = string_tuple(
        _first(json_ld, "employmentType")
        or _first(metadata, "employmentType", "employment_types", "jobType")
    )
    published_at = parse_datetime(
        _first(json_ld, "datePosted") or _first(metadata, "datePosted", "publishedTime")
    )
    updated_at = parse_datetime(
        _first(json_ld, "dateModified")
        or _first(metadata, "modifiedTime", "updatedAt", "lastModified")
    )
    description = _optional_text(_first(json_ld, "description"))
    description_format = "html" if description else "markdown" if markdown else "html"
    description = description or markdown or html
    apply_url = _safe_apply_url(
        url,
        _first(metadata, "applyUrl", "applicationUrl", "apply_url")
        or _first(json_ld, "url")
        or url,
    )
    if canonical_url_override and not _same_site_url(url, apply_url):
        apply_url = url
    identifier = _identifier_value(_first(json_ld, "identifier"))
    normalized_metadata: dict[str, Any] = {
        "firecrawl_target_url": target.url,
        "firecrawl_target_label": target.label,
        "firecrawl_target_kind": target.target_kind,
        "firecrawl_document_url": document.url,
        "firecrawl_mode": target.mode,
        "normalization_method": evidence[0] if evidence else "broad_heuristic",
        "normalization_evidence": evidence,
        "normalization_confidence": (
            "high" if json_ld else "medium" if len(evidence) >= 2 else "low"
        ),
        "provider_identifier": identifier,
        "remote_signal": remote,
        "remote_basis": strong_remote_basis,
        "workplace_type": "remote" if strong_remote_basis else None,
        **address_metadata,
    }
    normalized_metadata = {
        key: value for key, value in normalized_metadata.items() if value is not None
    }
    raw = {
        "firecrawl_document": dict(document.raw or {}),
        "job_posting_json_ld": json_ld or None,
        "target": {
            "url": target.url,
            "mode": target.mode,
            "target_kind": target.target_kind,
            "label": target.label,
        },
    }
    external_id = f"firecrawl:{hashlib.sha256(url.encode('utf-8')).hexdigest()[:40]}"
    return RawJobRecord(
        source=FirecrawlJobsConnector.source_info.key,
        source_job_id=external_id,
        title=unescape(title).strip(),
        company=company,
        locations=locations,
        description=description,
        description_format=description_format,
        canonical_url=url,
        apply_url=apply_url,
        published_at=published_at,
        updated_at=updated_at,
        employment_types=employment_types,
        remote=remote,
        salary=_salary_text(_first(json_ld, "baseSalary") if json_ld else None),
        metadata=normalized_metadata,
        raw=raw,
    )


def _parse_targets(value: Any, *, defaults: Mapping[str, Any]) -> tuple[FirecrawlTarget, ...]:
    if value is None or value == "":
        return ()
    values = value if isinstance(value, (list, tuple)) else (value,)
    targets: list[FirecrawlTarget] = []
    for item in values:
        if isinstance(item, str):
            item = {"url": item}
        if not isinstance(item, Mapping):
            raise ValueError("Each Firecrawl target must be a URL string or object")
        url = _optional_text(item.get("url"))
        if not url:
            raise ValueError("Each Firecrawl target requires a URL")
        target_scrape_options = {
            **dict(_mapping(defaults.get("scrape_options"))),
            **dict(_mapping(item.get("scrape_options"))),
        }
        target_crawl_options = {
            **dict(_mapping(defaults.get("crawl_options"))),
            **dict(_mapping(item.get("crawl_options"))),
        }
        targets.append(
            FirecrawlTarget(
                url=url,
                mode=str(item.get("mode") or "crawl").strip().casefold(),
                label=_optional_text(item.get("label")),
                target_kind=_optional_text(item.get("target_kind") or item.get("category")),
                company=_optional_text(item.get("company")) or defaults.get("company"),
                default_location=_optional_text(item.get("default_location"))
                or defaults.get("default_location"),
                limit=_coalesce(_optional_int(item.get("limit")), defaults.get("limit")),
                poll_interval_seconds=_coalesce(
                    _optional_float(item.get("poll_interval_seconds")),
                    defaults.get("poll_interval_seconds"),
                ),
                timeout_seconds=_coalesce(
                    _optional_float(item.get("timeout_seconds")),
                    defaults.get("timeout_seconds"),
                ),
                accept_all_pages=_optional_bool(
                    item.get("accept_all_pages"),
                    default=bool(defaults.get("accept_all_pages", False)),
                ),
                formats=string_tuple(item.get("formats")) or ("markdown", "html"),
                scrape_options=target_scrape_options,
                crawl_options=target_crawl_options,
            )
        )
    return _deduplicate_targets(tuple(targets))


def _deduplicate_targets(targets: tuple[FirecrawlTarget, ...]) -> tuple[FirecrawlTarget, ...]:
    result: list[FirecrawlTarget] = []
    seen: set[tuple[str, str]] = set()
    for target in targets:
        key = (_canonical_url(target.url), target.mode)
        if key not in seen:
            seen.add(key)
            result.append(target)
    return tuple(result)


def _remember_documents(
    destination: dict[str, FirecrawlDocument], documents: tuple[FirecrawlDocument, ...]
) -> None:
    for document in documents:
        if document.url:
            try:
                key = _canonical_url(document.url)
            except ValueError:
                key = ""
        else:
            key = ""
        if not key:
            encoded = json.dumps(document.raw, sort_keys=True, default=str).encode("utf-8")
            key = hashlib.sha256(encoded).hexdigest()
        destination[key] = document


def _job_posting_json_ld(html: str | None) -> dict[str, Any]:
    if not html:
        return {}
    parser = _JsonLdParser()
    try:
        parser.feed(html)
    except Exception:
        return {}
    for raw in parser.documents:
        candidate = raw.strip().removeprefix("<!--").removesuffix("-->").strip()
        try:
            payload = json.loads(candidate)
        except (TypeError, ValueError):
            continue
        found = _find_job_posting(payload)
        if found is not None:
            return found
    return {}


def _find_job_posting(value: Any) -> dict[str, Any] | None:
    if isinstance(value, Mapping):
        raw_type = value.get("@type")
        types = string_tuple(raw_type)
        if any(
            item.casefold() == "jobposting"
            or item.casefold().endswith(("/jobposting", "#jobposting"))
            for item in types
        ):
            return dict(value)
        for item in value.values():
            found = _find_job_posting(item)
            if found is not None:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_job_posting(item)
            if found is not None:
                return found
    return None


def _job_locations(
    job: Mapping[str, Any],
    metadata: Mapping[str, Any],
    target: FirecrawlTarget,
) -> tuple[tuple[str, ...], dict[str, str]]:
    raw_locations = job.get("jobLocation") if job else None
    values = raw_locations if isinstance(raw_locations, list) else [raw_locations]
    locations: list[str] = []
    address_metadata: dict[str, str] = {}
    for value in values:
        if not isinstance(value, Mapping):
            continue
        address = value.get("address") if isinstance(value.get("address"), Mapping) else value
        locality = _optional_text(address.get("addressLocality"))
        region = _optional_text(address.get("addressRegion"))
        postcode = _optional_text(address.get("postalCode"))
        country = address.get("addressCountry")
        if isinstance(country, Mapping):
            country = country.get("name")
        country_text = _optional_text(country)
        rendered = " ".join(item for item in (postcode, locality) if item)
        suffix = ", ".join(item for item in (region, country_text) if item)
        location = ", ".join(item for item in (rendered, suffix) if item)
        if location and location not in locations:
            locations.append(location)
        if not address_metadata:
            address_metadata = {
                key: item
                for key, item in {
                    "postcode": postcode,
                    "city": locality,
                    "state": region,
                    "country": country_text,
                }.items()
                if item
            }
    fallback = _first_text(metadata, "jobLocation", "location", "address")
    fallback = fallback or _location_from_title(_first_text(metadata, "jobTitle", "title"))
    fallback = fallback or target.default_location
    if not locations and fallback:
        locations.append(fallback)
    remote_regions = job.get("applicantLocationRequirements") if job else None
    for region in string_tuple(remote_regions):
        rendered = f"{region} (Remote)"
        if rendered not in locations:
            locations.append(rendered)
    return tuple(locations), address_metadata

def _location_from_title(title: str | None) -> str | None:
    if not title:
        return None
    patterns = (
        r"\bjob in (?P<location>.+)$",
        r"\bhiring .+ in (?P<location>.+?)\s*\|\s*linkedin$",
        r"\bstellen (?P<location>[^|]+)$",
    )
    for pattern in patterns:
        if match := re.search(pattern, title.strip(), flags=re.IGNORECASE):
            return match.group("location").strip(" -|") or None
    return None


def _remote_value(
    job: Mapping[str, Any],
    metadata: Mapping[str, Any],
    locations: tuple[str, ...],
    text: str,
) -> bool | None:
    location_type = str(job.get("jobLocationType") or "").casefold() if job else ""
    if "telecommute" in location_type or "remote" in location_type:
        return True
    explicit = _first(metadata, "remote", "isRemote", "workplaceType", "remote_type")
    if isinstance(explicit, bool):
        return explicit
    explicit_text = str(explicit or "").casefold()
    if "remote" in explicit_text or "homeoffice" in explicit_text:
        return True
    joined = f"{' '.join(locations)} {text[:5000]}".casefold()
    if any(token in joined for token in ("100% remote", "fully remote", "vollständig remote")):
        return True
    if any(token in joined for token in ("remote", "homeoffice", "home-office")):
        return True
    return None


def _strong_remote_basis(job: Mapping[str, Any], metadata: Mapping[str, Any]) -> str | None:
    location_type = str(job.get("jobLocationType") or "").strip().casefold() if job else ""
    if "telecommute" in location_type:
        return "json_ld_telecommute"
    explicit = _first(metadata, "workplaceType", "remote_type")
    normalized = re.sub(r"[\s_-]+", " ", str(explicit or "").casefold()).strip()
    if normalized in {"remote", "fully remote", "full remote", "100% remote"}:
        return "structured_metadata_remote"
    return None


def _organization_name(value: Any) -> str | None:
    if isinstance(value, Mapping):
        return _optional_text(value.get("name") or value.get("legalName"))
    return _optional_text(value)


def _identifier_value(value: Any) -> str | None:
    if isinstance(value, Mapping):
        value = value.get("value") or value.get("name")
    return _optional_text(value)


def _salary_text(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        return _optional_text(value)
    currency = _optional_text(value.get("currency"))
    unit = _optional_text(value.get("unitText"))
    amount = value.get("value")
    if isinstance(amount, Mapping):
        minimum = amount.get("minValue")
        maximum = amount.get("maxValue")
        exact = amount.get("value")
        fallback = exact if exact is not None else minimum
        fallback = fallback if fallback is not None else maximum
        amount_text = (
            f"{minimum}-{maximum}"
            if minimum is not None and maximum is not None
            else str(fallback or "")
        )
        unit = unit or _optional_text(amount.get("unitText"))
    else:
        amount_text = str(amount or "")
    rendered = " ".join(item for item in (amount_text, currency, unit) if item)
    return rendered or None


def _job_signal_count(text: str) -> int:
    normalized = text.casefold()
    groups = (
        ("aufgaben", "responsibilities", "your role", "what you'll do"),
        ("anforderungen", "qualifikation", "requirements", "your profile"),
        ("bewerben", "bewerbung", "apply now", "apply for"),
        ("benefits", "wir bieten", "what we offer"),
    )
    return sum(any(term in normalized for term in group) for group in groups)


def _document_links(document: FirecrawlDocument) -> tuple[str, ...]:
    links = [
        unescape(value).strip("<>").rstrip("\\")
        for value in re.findall(
            r"\]\(\s*<?(https?://[^)\s>]+)",
            document.markdown or "",
        )
    ]
    if document.html:
        parser = _HrefParser()
        parser.feed(document.html)
        links.extend(urljoin(document.url or "", unescape(value)) for value in parser.links)
    return tuple(
        dict.fromkeys(
            link
            for link in links
            if urlsplit(link).scheme.casefold() in {"http", "https"}
        )
    )


def _strong_job_like_url(url: str) -> bool:
    split = urlsplit(url)
    path = split.path.casefold().rstrip("/")
    patterns = (
        r"/(?:job|jobs)/view/[^/]+$",
        r"/(?:job|position|vacancy|stellenangebot|stellenanzeige|karriere|jdp)/[^/]+$",
        r"/(?:job-listing|job-openings|desc|details)/[^/]+$",
        r"/stellenangebote--.+",
        r"/jobs/[^/]*\d[^/]*$",
    )
    return any(re.search(pattern, path) for pattern in patterns) or any(
        key.casefold() in {"id", "jk", "jobid", "job_id", "gh_jid", "vjk"}
        for key, _ in parse_qsl(split.query)
    )


def _job_like_url(url: str) -> bool:
    split = urlsplit(url)
    path = split.path.casefold().rstrip("/")
    patterns = (
        r"/(?:job|jobs|position|positions|vacancy|vacancies)/(?:view/)?[^/]+$",
        r"/(?:stellenangebot|stellenangebote|stellenanzeige|karriere|jdp)/[^/]+$",
        r"/(?:job-listing|job-openings|desc|details)/[^/]+$",
        r"/stellenangebote--.+",
    )
    return any(re.search(pattern, path) for pattern in patterns) or any(
        key.casefold() in {"id", "jk", "jobid", "job_id", "gh_jid", "vjk"}
        for key, _ in parse_qsl(split.query)
    )


def _generic_title(value: str | None) -> bool:
    normalized = re.sub(r"\s+", " ", (value or "").casefold()).strip(" -|")
    return normalized in {
        "",
        "jobs",
        "careers",
        "karriere",
        "stellenangebote",
        "jobsearch",
        "search",
        "suche",
        "offene stellen",
        "vacancies",
        "career opportunities",
    }


def _markdown_title(markdown: str) -> str | None:
    match = re.search(r"(?m)^\s*#\s+(.+?)\s*$", markdown)
    return _optional_text(match.group(1)) if match else None


def _title_from_url(url: str) -> str | None:
    segment = unquote(urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1])
    rendered = re.sub(r"[-_]+", " ", segment).strip()
    return rendered.title() if rendered else None


def _same_site_url(left: str, right: str) -> bool:
    left_host = (_hostname(left) or "").removeprefix("www.")
    right_host = (_hostname(right) or "").removeprefix("www.")
    return bool(left_host and right_host and left_host == right_host)


def _canonical_url(value: Any) -> str:
    raw = str(value or "").strip()
    split = urlsplit(raw)
    if split.scheme.casefold() not in {"http", "https"} or not split.netloc:
        raise ValueError("Job document URL must be an absolute HTTP(S) URL")
    query = sorted(
        (key, item)
        for key, item in parse_qsl(split.query, keep_blank_values=True)
        if not key.casefold().startswith("utm_") and key.casefold() not in {"fbclid", "gclid"}
    )
    path = split.path or "/"
    if path != "/":
        path = path.rstrip("/")
    return urlunsplit(
        (split.scheme.casefold(), split.netloc.casefold(), path, urlencode(query), "")
    )


def _safe_apply_url(base_url: str, value: Any) -> str:
    try:
        return _canonical_url(urljoin(base_url, str(value or "")))
    except ValueError:
        return base_url


def _v2_base_url(value: str) -> str:
    normalized = value.strip().rstrip("/")
    if not normalized:
        normalized = SELF_HOSTED_BASE_URL
    return normalized if normalized.endswith("/v2") else f"{normalized}/v2"


def _first(value: Mapping[str, Any], *keys: str) -> Any:
    if not isinstance(value, Mapping):
        return None
    folded = {str(key).casefold(): item for key, item in value.items()}
    for key in keys:
        item = folded.get(key.casefold())
        if item is not None and item != "":
            return item
    return None


def _first_text(value: Mapping[str, Any], *keys: str) -> str | None:
    return _optional_text(_first(value, *keys))


def _optional_text(value: Any) -> str | None:
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return None
    rendered = re.sub(r"\s+", " ", str(value)).strip()
    return rendered or None


def _optional_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _optional_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _optional_bool(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    normalized = str(value).strip().casefold()
    if normalized in {"1", "true", "yes", "on", "ja"}:
        return True
    if normalized in {"0", "false", "no", "off", "nein"}:
        return False
    raise ValueError(f"Invalid boolean value: {value!r}")


def _coalesce(value: Any, fallback: Any) -> Any:
    return fallback if value is None else value


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _status_name(status: FirecrawlJobStatus) -> str:
    return status.status.strip().casefold()


def _target_error_status(exc: Exception) -> str:
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    if status_code == 403:
        return "blocked"
    if status_code == 429:
        return "rate_limited"
    return "failed"


def _safe_error(exc: Exception) -> str:
    message = re.sub(r"\s+", " ", str(exc)).strip()
    return f"{type(exc).__name__}: {message}"[:1000]
