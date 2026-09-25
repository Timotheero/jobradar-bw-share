from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from jobradar.connectors.base import JobQuery  # noqa: E402
from jobradar.connectors.firecrawl import (  # noqa: E402
    FirecrawlDocument,
    FirecrawlJobStatus,
    FirecrawlV2Client,
)
from jobradar.connectors.firecrawl_jobs import (  # noqa: E402
    FirecrawlJobsConnector,
    FirecrawlJobsTimeout,
    FirecrawlTarget,
)
from jobradar.connectors.public_url import PreparedPublicURL  # noqa: E402


def _prepare(url: str) -> PreparedPublicURL:
    return PreparedPublicURL(url, url, 0)



def _status(
    state: str,
    *,
    documents: tuple[FirecrawlDocument, ...] = (),
    next_url: str | None = None,
) -> FirecrawlJobStatus:
    return FirecrawlJobStatus(
        job_id="crawl-1",
        status=state,
        documents=documents,
        next_url=next_url,
        completed=len(documents),
        total=None,
        raw={"status": state},
    )


def _json_ld_document(
    url: str = "https://jobs.example.test/jobs/executive-assistant?utm_source=test",
) -> FirecrawlDocument:
    posting = {
        "@context": "https://schema.org",
        "@type": "JobPosting",
        "identifier": {"value": "EA-42"},
        "title": "Executive Assistant to the CEO",
        "description": "<p>Support the CEO, coordinate meetings and prepare presentations.</p>",
        "url": url,
        "datePosted": "2026-07-10",
        "employmentType": ["FULL_TIME"],
        "jobLocationType": "TELECOMMUTE",
        "hiringOrganization": {"@type": "Organization", "name": "Acme GmbH"},
        "jobLocation": {
            "@type": "Place",
            "address": {
                "@type": "PostalAddress",
                "postalCode": "70173",
                "addressLocality": "Stuttgart",
                "addressRegion": "Baden-Württemberg",
                "addressCountry": "DE",
            },
        },
        "baseSalary": {
            "currency": "EUR",
            "value": {"minValue": 55000, "maxValue": 65000, "unitText": "YEAR"},
        },
    }
    html = (
        '<html><script data-purpose="job" type="application/ld+json">'
        f"{json.dumps(posting, ensure_ascii=False)}"
        "</script></html>"
    )
    return FirecrawlDocument(
        url=url,
        markdown="# Executive Assistant\n\nResponsibilities and requirements. Apply now.",
        html=html,
        metadata={"sourceURL": url},
        raw={"markdown": "raw-one", "metadata": {"sourceURL": url}},
    )


def _heuristic_document() -> FirecrawlDocument:
    url = "https://jobs.example.test/jobs/chief-of-staff"
    return FirecrawlDocument(
        url=url,
        markdown=(
            "# Chief of Staff\n\nResponsibilities: Support management. "
            "Requirements: excellent organisation. Apply now."
        ),
        html=None,
        metadata={
            "sourceURL": url,
            "title": "Chief of Staff",
            "company": "Example AG",
            "location": "Karlsruhe",
            "employmentType": "Full-time",
        },
        raw={"markdown": "raw-two"},
    )


def test_client_waits_until_self_hosted_api_accepts_connections() -> None:
    attempts = 0
    elapsed = [0.0]

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        assert request.method == "GET"
        assert str(request.url) == "http://firecrawl.internal:3002"
        attempts += 1
        if attempts < 3:
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(200, request=request)

    client = FirecrawlV2Client(
        base_url="http://firecrawl.internal:3002/v2",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleeper=lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds),
        monotonic=lambda: elapsed[0],
    )

    client.wait_until_ready(timeout_seconds=5, poll_interval_seconds=1)

    assert attempts == 3
    assert elapsed == [2.0]


def test_client_search_limits_domains_and_maps_scraped_web_results() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/v2/search"
        body = json.loads(request.content)
        assert body == {
            "query": '("Executive Assistant") (Job OR Stelle)',
            "limit": 7,
            "page": 3,
            "sources": ["web"],
            "includeDomains": ["jobs.example.test"],
            "country": "DE",
            "location": "Baden-Württemberg",
        }
        return httpx.Response(
            200,
            request=request,
            json={
                "success": True,
                "data": {
                    "web": [
                        {
                            "url": "https://jobs.example.test/jobs/ea-42",
                            "title": "Executive Assistant to the CEO",
                            "description": "Responsibilities and requirements. Apply now.",
                            "markdown": "# Executive Assistant\n\nApply now.",
                        }
                    ]
                },
            },
        )

    client = FirecrawlV2Client(
        base_url="http://firecrawl.internal:3002/v2",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    documents = client.search(
        '("Executive Assistant") (Job OR Stelle)',
        include_domains=("jobs.example.test",),
        limit=7,
        page=3,
        location="Baden-Württemberg",
    )

    assert len(documents) == 1
    assert documents[0].url == "https://jobs.example.test/jobs/ea-42"
    assert documents[0].metadata["jobTitle"] == "Executive Assistant to the CEO"
    assert documents[0].metadata["firecrawlSearchResult"] is True


class PollingClient:
    def __init__(self) -> None:
        self.start_calls: list[dict[str, Any]] = []
        self.get_calls: list[tuple[str, str | None]] = []
        self.readiness_calls: list[dict[str, float]] = []
        self.closed = False
        self.poll_number = 0

    def wait_until_ready(self, **kwargs: float) -> None:
        self.readiness_calls.append(kwargs)

    def start_crawl(self, url: str, **kwargs: Any) -> FirecrawlJobStatus:
        self.start_calls.append({"url": url, **kwargs})
        return _status("queued")

    def get_crawl(self, job_id: str, *, page_url: str | None = None) -> FirecrawlJobStatus:
        self.get_calls.append((job_id, page_url))
        if page_url:
            return _status(
                "completed",
                documents=(_json_ld_document(), _heuristic_document()),
            )
        self.poll_number += 1
        if self.poll_number == 1:
            return _status("scraping")
        return _status(
            "completed",
            documents=(_json_ld_document(),),
            next_url="http://firecrawl.internal:3002/v2/crawl/crawl-1?skip=1",
        )

    def scrape(self, url: str, **kwargs: Any) -> FirecrawlDocument:
        raise AssertionError(f"unexpected scrape call: {url}, {kwargs}")

    def close(self) -> None:
        self.closed = True


def test_crawl_polls_to_completion_paginates_normalizes_and_deduplicates() -> None:
    client = PollingClient()
    elapsed = [0.0]

    connector = FirecrawlJobsConnector(
        (FirecrawlTarget("https://jobs.example.test/careers", limit=25),),
        client=client,
        poll_interval_seconds=1,
        timeout_seconds=10,
        sleeper=lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds),
        monotonic=lambda: elapsed[0],
        target_url_preparer=_prepare,
    )

    records = list(connector.iter_jobs())
    assert client.readiness_calls == [{"timeout_seconds": 60.0}]

    assert len(records) == 2
    assert len({record.source_job_id for record in records}) == 2
    structured = next(record for record in records if record.title.startswith("Executive"))
    assert structured.company == "Acme GmbH"
    assert structured.locations == ("70173 Stuttgart, Baden-Württemberg, DE",)
    assert structured.employment_types == ("FULL_TIME",)
    assert structured.canonical_url == "https://jobs.example.test/jobs/executive-assistant"
    assert structured.salary == "55000-65000 EUR YEAR"
    assert structured.metadata["normalization_confidence"] == "high"
    assert structured.metadata["provider_identifier"] == "EA-42"
    assert structured.metadata["workplace_type"] == "remote"
    assert structured.metadata["remote_basis"] == "json_ld_telecommute"
    assert structured.raw["target"]["url"] == "https://jobs.example.test/careers"

    heuristic = next(record for record in records if record.title == "Chief of Staff")
    assert heuristic.company == "Example AG"
    assert heuristic.metadata["normalization_confidence"] == "medium"
    assert client.start_calls[0]["limit"] == 25
    assert client.start_calls[0]["scrape_options"]["formats"] == ["markdown", "html"]
    assert client.get_calls == [
        ("crawl-1", None),
        ("crawl-1", None),
        (
            "crawl-1",
            "http://firecrawl.internal:3002/v2/crawl/crawl-1?skip=1",
        ),
    ]
    assert connector.run_diagnostics["documents_seen"] == 2
    assert connector.run_diagnostics["target_results"] == [
        {
            "target": "https://jobs.example.test/careers",
            "label": None,
            "target_kind": None,
            "status": "completed",
            "documents_seen": 2,
            "records_emitted": 2,
            "pages_rejected": 0,
            "duplicates_skipped": 0,
            "query_filtered": 0,
            "page_error_count": 0,
        }
    ]
    connector.close()
    assert client.closed is True


class ScrapeClient:
    def __init__(self, document: FirecrawlDocument) -> None:
        self.document = document
        self.scrape_calls: list[tuple[str, dict[str, Any]]] = []
        self.readiness_calls: list[dict[str, float]] = []
        self.closed = False

    def wait_until_ready(self, **kwargs: float) -> None:
        self.readiness_calls.append(kwargs)

    def scrape(self, url: str, **kwargs: Any) -> FirecrawlDocument:
        self.scrape_calls.append((url, kwargs))
        return self.document

    def start_crawl(self, url: str, **kwargs: Any) -> FirecrawlJobStatus:
        raise AssertionError(f"unexpected crawl call: {url}, {kwargs}")

    def get_crawl(self, job_id: str, *, page_url: str | None = None) -> FirecrawlJobStatus:
        raise AssertionError(f"unexpected status call: {job_id}, {page_url}")

    def close(self) -> None:
        self.closed = True


class PortalSearchClient:
    def __init__(self) -> None:
        self.search_calls: list[dict[str, Any]] = []
        self.scrape_calls: list[str] = []
        self.readiness_calls: list[dict[str, float]] = []

    def wait_until_ready(self, **kwargs: float) -> None:
        self.readiness_calls.append(kwargs)

    def search(
        self,
        query: str,
        *,
        include_domains: tuple[str, ...],
        **kwargs: Any,
    ) -> tuple[FirecrawlDocument, ...]:
        self.search_calls.append(
            {"query": query, "include_domains": include_domains, **kwargs}
        )
        if kwargs.get("page", 1) > 1:
            return ()
        domain = include_domains[(len(self.search_calls) - 1) % len(include_domains)]
        number = len(self.search_calls)
        role = "Vorstandsassistenz" if "Vorstandsassistenz" in query else "Executive Assistant"
        valid = FirecrawlDocument(
            url=f"https://{domain}/jobs/{role.casefold().replace(' ', '-')}-{number}",
            markdown=(
                f"# {role} Stuttgart {number}\n\n"
                "Responsibilities and requirements. Apply now."
            ),
            html=None,
            metadata={
                "jobTitle": f"{role} Stuttgart {number}",
                "firecrawlSearchResult": True,
            },
            raw={"search": number},
        )
        unrelated = FirecrawlDocument(
            url=f"https://{domain}/jobs/hr-specialist-{number}",
            markdown="# HR Specialist\n\nResponsibilities and requirements. Apply now.",
            html=None,
            metadata={"jobTitle": "HR Specialist", "firecrawlSearchResult": True},
            raw={"search": f"unrelated-{number}"},
        )
        foreign = FirecrawlDocument(
            url="https://unapproved.example/jobs/foreign",
            markdown="# Foreign job\n\nResponsibilities. Requirements. Apply now.",
            html=None,
            metadata={"jobTitle": "Foreign job", "firecrawlSearchResult": True},
            raw={},
        )
        listing = FirecrawlDocument(
            url=f"https://{domain}/jobs",
            markdown="# Jobs\n\nSearch all jobs.",
            html=None,
            metadata={"jobTitle": "Jobs", "firecrawlSearchResult": True},
            raw={},
        )
        return (valid, unrelated, listing, foreign)

    def scrape(self, url: str, **kwargs: Any) -> FirecrawlDocument:
        self.scrape_calls.append(url)
        if "portal-" in url:
            return FirecrawlDocument(
                url=url,
                markdown="# Job detail\n\nResponsibilities and requirements. Apply now.",
                html=None,
                metadata={"sourceURL": url},
                raw={"detail": url},
            )
        return _heuristic_document()

    def start_crawl(self, url: str, **kwargs: Any) -> FirecrawlJobStatus:
        raise AssertionError(f"unexpected crawl call: {url}, {kwargs}")

    def get_crawl(self, job_id: str, *, page_url: str | None = None) -> FirecrawlJobStatus:
        raise AssertionError(f"unexpected status call: {job_id}, {page_url}")

    def close(self) -> None:
        pass


def test_portal_search_runs_each_role_batch_per_portal_but_direct_targets_only_once() -> None:
    client = PortalSearchClient()
    connector = FirecrawlJobsConnector(
        (
            FirecrawlTarget(
                "https://portal-one.example/search",
                label="Portal one",
                target_kind="job_portal",
            ),
            FirecrawlTarget(
                "https://portal-two.example/search",
                label="Portal two",
                target_kind="job_portal",
            ),
            FirecrawlTarget(
                "https://company.example/careers",
                mode="scrape",
                label="Company",
                target_kind="company_site",
            ),
        ),
        portal_search_enabled=True,
        portal_search_limit=8,
        client=client,
        target_url_preparer=_prepare,
    )

    first = list(
        connector.iter_jobs(
            JobQuery(text='"Executive Assistant"', location="Baden-Württemberg")
        )
    )
    second = list(
        connector.iter_jobs(
            JobQuery(
                text='"Vorstandsassistenz"',
                location="Deutschland",
                filters={"homeoffice": "nv_true"},
            )
        )
    )

    assert len(first) == 3
    assert len(second) == 2
    assert len(client.search_calls) == 8
    assert [call["include_domains"] for call in client.search_calls] == [
        ("portal-one.example",),
        ("portal-one.example",),
        ("portal-two.example",),
        ("portal-two.example",),
        ("portal-one.example",),
        ("portal-one.example",),
        ("portal-two.example",),
        ("portal-two.example",),
    ]
    assert [call["page"] for call in client.search_calls] == [1, 2] * 4
    assert client.search_calls[0]["location"] == "Baden-Württemberg"
    assert client.search_calls[4]["query"].endswith("Remote")
    assert all(call["limit"] == 8 for call in client.search_calls)
    assert client.scrape_calls.count("https://company.example/careers") == 1
    assert "https://unapproved.example/jobs/foreign" not in client.scrape_calls
    assert client.readiness_calls == [{"timeout_seconds": 60.0}]
    assert connector.run_diagnostics["records_emitted"] == 5
    assert connector.run_diagnostics["pages_rejected"] == 6
    assert connector.run_diagnostics["duplicates_skipped"] == 2
    assert connector.run_diagnostics["query_filtered"] == 4
    assert connector.run_diagnostics["search_requests"] == 8
    assert connector.run_diagnostics["search_results_seen"] == 16


class FixedPortalSearchClient(PortalSearchClient):
    def __init__(self, documents: tuple[FirecrawlDocument, ...]) -> None:
        super().__init__()
        self.documents = documents

    def search(
        self,
        query: str,
        *,
        include_domains: tuple[str, ...],
        **kwargs: Any,
    ) -> tuple[FirecrawlDocument, ...]:
        self.search_calls.append(
            {"query": query, "include_domains": include_domains, **kwargs}
        )
        return self.documents

    def scrape(self, url: str, **kwargs: Any) -> FirecrawlDocument:
        self.scrape_calls.append(url)
        assert self.documents
        return self.documents[0]

class ListingPortalSearchClient(FixedPortalSearchClient):
    def __init__(self) -> None:
        listing_url = "https://portal.example/jobs/senior-assistant"
        super().__init__(
            (
                FirecrawlDocument(
                    url=listing_url,
                    markdown="# Senior Assistant jobs",
                    html=None,
                    metadata={"jobTitle": "Senior Assistant jobs"},
                    raw={"kind": "search"},
                ),
            )
        )
        self.listing_url = listing_url
        self.detail_url = "https://portal.example/jobs/view/senior-assistant-1001"

    def scrape(self, url: str, **kwargs: Any) -> FirecrawlDocument:
        self.scrape_calls.append(url)
        if url == self.listing_url:
            return FirecrawlDocument(
                url=url,
                markdown=f"[Senior Assistant]({self.detail_url})",
                html=None,
                metadata={},
                raw={"kind": "listing"},
            )
        assert url == self.detail_url
        return FirecrawlDocument(
            url=url,
            markdown="# Senior Assistant\n\nResponsibilities and requirements. Apply now.",
            html=None,
            metadata={
                "jobTitle": "Senior Assistant - Full-time Job in Mannheim",
                "company": "Example GmbH",
            },
            raw={"kind": "detail"},
        )


@pytest.mark.parametrize(
    ("documents", "expected_status"),
    [
        ((), "empty"),
        (
            (
                FirecrawlDocument(
                    url="https://portal.example/jobs/executive-assistenz",
                    markdown="# Executive Assistenz\n\nAktuelle Suchergebnisse.",
                    html=None,
                    metadata={
                        "jobTitle": "Executive Assistenz",
                        "firecrawlSearchResult": True,
                    },
                    raw={},
                ),
            ),
            "insufficient",
        ),
    ],
)
def test_portal_search_distinguishes_empty_and_insufficient_results(
    documents: tuple[FirecrawlDocument, ...],
    expected_status: str,
) -> None:
    client = FixedPortalSearchClient(documents)
    connector = FirecrawlJobsConnector(
        (
            FirecrawlTarget(
                "https://portal.example/search",
                label="Portal",
                target_kind="job_portal",
            ),
        ),
        portal_search_enabled=True,
        client=client,
        target_url_preparer=_prepare,
    )

    assert list(connector.iter_jobs(JobQuery(text='"Executive Assistenz"'))) == []
    assert connector.run_diagnostics["target_results"][0]["status"] == expected_status

def test_portal_search_reports_detail_scrape_error_as_target_failure() -> None:
    document_url = "https://portal.example/jobs/executive-assistant"

    class FailingPortalSearchClient(FixedPortalSearchClient):
        def scrape(self, url: str, **kwargs: Any) -> FirecrawlDocument:
            self.scrape_calls.append(url)
            raise RuntimeError("Firecrawl detail extraction failed exactly")

    client = FailingPortalSearchClient(
        (
            FirecrawlDocument(
                url=document_url,
                markdown="# Executive Assistant",
                html=None,
                metadata={"jobTitle": "Executive Assistant"},
                raw={},
            ),
        )
    )
    connector = FirecrawlJobsConnector(
        (
            FirecrawlTarget(
                "https://portal.example/search",
                label="Portal",
                target_kind="job_portal",
            ),
        ),
        portal_search_enabled=True,
        client=client,
        target_url_preparer=_prepare,
    )

    assert list(connector.iter_jobs(JobQuery(text='"Executive Assistant"'))) == []
    diagnostics = connector.run_diagnostics
    assert diagnostics["target_results"][0]["status"] == "failed"
    assert diagnostics["target_results"][0]["page_error_count"] == 1
    assert diagnostics["page_errors"] == [
        {
            "document_url": document_url,
            "target": "https://portal.example/search",
            "error": "RuntimeError: Firecrawl detail extraction failed exactly",
        }
    ]


def test_portal_search_follows_job_links_from_listing_results() -> None:
    client = ListingPortalSearchClient()
    connector = FirecrawlJobsConnector(
        (
            FirecrawlTarget(
                "https://portal.example/search",
                label="Portal",
                target_kind="job_portal",
            ),
        ),
        portal_search_enabled=True,
        client=client,
        target_url_preparer=_prepare,
    )

    records = list(connector.iter_jobs(JobQuery(text='"Senior Assistant"')))

    assert len(records) == 1
    assert records[0].canonical_url == client.detail_url
    assert records[0].locations == ("Mannheim",)
    assert client.scrape_calls == [client.listing_url, client.detail_url]
    assert connector.run_diagnostics["target_results"][0]["status"] == "completed"

def test_portal_search_matches_role_title_tokens_independent_of_word_order() -> None:
    document = FirecrawlDocument(
        url="https://portal.example/jobs/view/senior-assistant-to-ceo",
        markdown="# Senior Assistant to CEO\n\nResponsibilities and requirements. Apply now.",
        html=None,
        metadata={
            "jobTitle": "Senior Assistant to CEO",
            "company": "Example GmbH",
            "firecrawlSearchResult": True,
        },
        raw={},
    )
    connector = FirecrawlJobsConnector(
        (
            FirecrawlTarget(
                "https://portal.example/search",
                label="Portal",
                target_kind="job_portal",
            ),
        ),
        portal_search_enabled=True,
        client=FixedPortalSearchClient((document,)),
        target_url_preparer=_prepare,
    )

    records = list(connector.iter_jobs(JobQuery(text='"CEO Assistant"')))

    assert len(records) == 1
    assert records[0].title == "Senior Assistant to CEO"


def test_portal_search_accepts_safe_external_job_results_and_records_origin() -> None:
    external_url = "https://new-portal.example/jobs/executive-assistant-42"
    client = FixedPortalSearchClient(
        (
            FirecrawlDocument(
                url=external_url,
                markdown=(
                    "# Executive Assistant\n\n"
                    "Responsibilities and requirements. Apply now."
                ),
                html=(
                    '<script type="application/ld+json">'
                    '{"@type":"JobPosting","title":"Executive Assistant",'
                    '"url":"http://127.0.0.1/private"}'
                    "</script>"
                ),
                metadata={
                    "jobTitle": "Executive Assistant",
                    "company": "Example GmbH",
                },
                raw={"kind": "external-search-result"},
            ),
        )
    )
    connector = FirecrawlJobsConnector(
        (
            FirecrawlTarget(
                "https://approved.example/search",
                label="Approved",
                target_kind="job_portal",
            ),
        ),
        portal_search_enabled=True,
        portal_search_accept_external_results=True,
        client=client,
        target_url_preparer=_prepare,
    )

    records = list(connector.iter_jobs(JobQuery(text='"Executive Assistant"')))

    assert len(records) == 1
    assert records[0].canonical_url == external_url
    assert records[0].apply_url == external_url
    assert records[0].metadata["firecrawl_discovered_domain"] == "new-portal.example"
    assert (
        records[0].metadata["firecrawl_search_origin_target_url"]
        == "https://approved.example/search"
    )
    assert records[0].metadata["firecrawl_target_url"] == "https://new-portal.example/"
    assert client.search_calls[0]["include_domains"] == ("approved.example",)
    assert client.search_calls[0]["limit"] == 100
    assert connector.run_diagnostics["external_records_emitted"] == 1
    assert connector.run_diagnostics["discovered_portal_domains"] == [
        "new-portal.example"
    ]


def test_portal_search_accepts_external_listing_from_another_domain() -> None:
    approved_listing = "https://approved.example/search/results"
    external_listing = "https://new-portal.example/search/results"
    external_detail = "https://new-portal.example/jobs/executive-assistant-99"

    class ExternalListingClient(FixedPortalSearchClient):
        def __init__(self) -> None:
            super().__init__(
                (
                    FirecrawlDocument(
                        url=approved_listing,
                        markdown=None,
                        html=None,
                        metadata={},
                        raw={"kind": "approved"},
                    ),
                    FirecrawlDocument(
                        url=external_listing,
                        markdown=None,
                        html=None,
                        metadata={},
                        raw={"kind": "external"},
                    ),
                )
            )

        def scrape(self, url: str, **kwargs: Any) -> FirecrawlDocument:
            self.scrape_calls.append(url)
            if url == approved_listing:
                return FirecrawlDocument(
                    url=url,
                    markdown="# Search",
                    html=None,
                    metadata={},
                    raw={},
                )
            if url == external_listing:
                return FirecrawlDocument(
                    url=url,
                    markdown=f"[Executive Assistant]({external_detail})",
                    html=None,
                    metadata={},
                    raw={},
                )
            assert url == external_detail
            return FirecrawlDocument(
                url=url,
                markdown=(
                    "# Executive Assistant\n\n"
                    "Responsibilities and requirements. Apply now."
                ),
                html=None,
                metadata={
                    "jobTitle": "Executive Assistant",
                    "company": "Example GmbH",
                },
                raw={"kind": "detail"},
            )

    client = ExternalListingClient()
    connector = FirecrawlJobsConnector(
        (
            FirecrawlTarget(
                "https://approved.example/search",
                label="Approved",
                target_kind="job_portal",
            ),
        ),
        portal_search_enabled=True,
        portal_search_accept_external_results=True,
        client=client,
        target_url_preparer=_prepare,
    )

    records = list(connector.iter_jobs(JobQuery(text='"Executive Assistant"')))

    assert [record.canonical_url for record in records] == [external_detail]
    assert client.scrape_calls == [
        approved_listing,
        external_listing,
        external_detail,
    ]
    assert connector.run_diagnostics["discovered_portal_domains"] == [
        "new-portal.example"
    ]


def test_portal_search_paginates_until_a_page_has_no_new_urls() -> None:
    def document(number: int) -> FirecrawlDocument:
        return FirecrawlDocument(
            url=f"https://portal.example/jobs/executive-assistant-{number}",
            markdown=(
                f"# Executive Assistant {number}\n\n"
                "Responsibilities and requirements. Apply now."
            ),
            html=None,
            metadata={"jobTitle": f"Executive Assistant {number}"},
            raw={"number": number},
        )

    class PaginatedPortalSearchClient(FixedPortalSearchClient):
        def __init__(self) -> None:
            super().__init__((document(1),))

        def search(
            self,
            query: str,
            *,
            include_domains: tuple[str, ...],
            **kwargs: Any,
        ) -> tuple[FirecrawlDocument, ...]:
            self.search_calls.append(
                {"query": query, "include_domains": include_domains, **kwargs}
            )
            return {
                1: (document(1), document(2)),
                2: (document(2), document(3)),
                3: (document(2), document(3)),
            }[kwargs["page"]]

        def scrape(self, url: str, **kwargs: Any) -> FirecrawlDocument:
            self.scrape_calls.append(url)
            number = int(url.rsplit("-", 1)[-1])
            return document(number)

    client = PaginatedPortalSearchClient()
    connector = FirecrawlJobsConnector(
        (
            FirecrawlTarget(
                "https://portal.example/search",
                label="Portal",
                target_kind="job_portal",
            ),
        ),
        portal_search_enabled=True,
        client=client,
        target_url_preparer=_prepare,
    )

    records = list(connector.iter_jobs(JobQuery(text='"Executive Assistant"')))

    assert [call["page"] for call in client.search_calls] == [1, 2, 3]
    assert len(records) == 3
    assert {record.canonical_url for record in records} == {
        document(number).url for number in range(1, 4)
    }
    assert client.scrape_calls == [document(number).url for number in range(1, 4)]
    assert connector.run_diagnostics["search_requests"] == 3
    assert connector.run_diagnostics["search_results_seen"] == 3


def test_portal_search_has_no_three_candidate_limit() -> None:
    documents = tuple(
        FirecrawlDocument(
            url=f"https://portal.example/jobs/executive-assistant-{number}",
            markdown=(
                f"# Executive Assistant {number}\n\n"
                "Responsibilities and requirements. Apply now."
            ),
            html=None,
            metadata={"jobTitle": f"Executive Assistant {number}"},
            raw={"number": number},
        )
        for number in range(1, 5)
    )
    client = FixedPortalSearchClient(documents)
    connector = FirecrawlJobsConnector(
        (
            FirecrawlTarget(
                "https://portal.example/search",
                label="Portal",
                target_kind="job_portal",
            ),
        ),
        portal_search_enabled=True,
        client=client,
        target_url_preparer=_prepare,
    )

    records = list(connector.iter_jobs(JobQuery(text='"Executive Assistant"')))

    assert len(records) == 4
    assert {record.canonical_url for record in records} == {
        document.url for document in documents
    }
    assert client.scrape_calls == [document.url for document in documents]


def test_from_source_is_lazy_uses_self_host_url_and_optional_key() -> None:
    client = ScrapeClient(_heuristic_document())
    builds: list[dict[str, Any]] = []

    def build_client(**kwargs: Any) -> ScrapeClient:
        builds.append(kwargs)
        return client

    source = SimpleNamespace(
        base_url="http://source-firecrawl:3002",
        metadata_json={
            "firecrawl": {
                "api_key_env": "LOCAL_FIRECRAWL_KEY",
                "targets": [
                    {
                        "url": "https://jobs.example.test/jobs/chief-of-staff",
                        "mode": "scrape",
                    }
                ],
                "portal_search": {
                    "enabled": True,
                    "limit": 8,
                    "accept_external_results": True,
                },
            }
        },
    )
    connector = FirecrawlJobsConnector.from_source(
        source,
        environment={
            "FIRECRAWL_BASE_URL": "http://firecrawl.internal:3002",
            "LOCAL_FIRECRAWL_KEY": "local-secret",
        },
        client_factory=build_client,
        target_url_preparer=_prepare,
    )
    assert connector.portal_search_enabled is True
    assert connector.portal_search_limit == 8
    assert connector.portal_search_accept_external_results is True

    assert builds == []
    assert connector.client_created is False
    records = list(connector.iter_jobs())
    assert client.readiness_calls == [{"timeout_seconds": 60.0}]
    assert len(records) == 1
    assert builds == [
        {
            "api_key": "local-secret",
            "base_url": "http://firecrawl.internal:3002/v2",
        }
    ]
    assert client.scrape_calls[0][0].endswith("/jobs/chief-of-staff")
    assert "local-secret" not in json.dumps(records[0].raw)


def test_no_targets_never_constructs_client() -> None:
    builds = 0

    def fail_if_built(**kwargs: Any):
        nonlocal builds
        builds += 1
        raise AssertionError(f"client constructed without targets: {kwargs}")

    connector = FirecrawlJobsConnector(client_factory=fail_if_built)

    assert list(connector.iter_jobs()) == []
    assert builds == 0
    assert connector.client_created is False
    assert connector.run_diagnostics["skip_reason"] == "no_targets"


class NeverCompletesClient(PollingClient):
    def get_crawl(self, job_id: str, *, page_url: str | None = None) -> FirecrawlJobStatus:
        self.get_calls.append((job_id, page_url))
        return _status("scraping")


def test_crawl_timeout_is_configurable_and_bounded() -> None:
    client = NeverCompletesClient()
    elapsed = [0.0]
    connector = FirecrawlJobsConnector(
        (FirecrawlTarget("https://jobs.example.test/careers"),),
        client=client,
        poll_interval_seconds=1,
        timeout_seconds=2.5,
        continue_on_target_error=False,
        sleeper=lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds),
        monotonic=lambda: elapsed[0],
        target_url_preparer=_prepare,
    )

    with pytest.raises(FirecrawlJobsTimeout, match="exceeded 2.5 seconds"):
        list(connector.iter_jobs())

    assert elapsed[0] == 2.5
    assert len(client.get_calls) == 3


class PartialFailureClient(ScrapeClient):
    def start_crawl(self, url: str, **kwargs: Any) -> FirecrawlJobStatus:
        del url, kwargs
        return _status("failed")


def test_failed_target_does_not_hide_later_configured_target() -> None:
    client = PartialFailureClient(_heuristic_document())
    connector = FirecrawlJobsConnector(
        (
            FirecrawlTarget("https://broken.example.test/jobs"),
            FirecrawlTarget(
                "https://jobs.example.test/jobs/chief-of-staff",
                mode="scrape",
            ),
        ),
        client=client,
        poll_interval_seconds=0.01,
        target_url_preparer=_prepare,
    )

    records = list(connector.iter_jobs())

    assert [record.title for record in records] == ["Chief of Staff"]
    assert connector.run_diagnostics["targets_started"] == 2
    assert connector.run_diagnostics["targets_completed"] == 1
    assert connector.run_diagnostics["target_error_count"] == 1
    assert "FirecrawlJobsError" in connector.run_diagnostics["target_errors"][0]["error"]


class MalformedPageClient(ScrapeClient):
    def scrape(self, url: str, **kwargs: Any) -> FirecrawlDocument:
        if "broken" in url:
            return FirecrawlDocument(
                url="http://[broken",
                markdown="# Broken page\nResponsibilities. Requirements. Apply now.",
                html=None,
                metadata={"title": "Broken page"},
                raw={"broken": True},
            )
        return super().scrape(url, **kwargs)


def test_malformed_page_is_reported_without_hiding_later_page() -> None:
    connector = FirecrawlJobsConnector(
        (
            FirecrawlTarget("https://broken.example.test/job", mode="scrape"),
            FirecrawlTarget(
                "https://jobs.example.test/jobs/chief-of-staff",
                mode="scrape",
            ),
        ),
        client=MalformedPageClient(_heuristic_document()),
        target_url_preparer=_prepare,
    )

    records = list(connector.iter_jobs())

    assert [record.title for record in records] == ["Chief of Staff"]
    assert connector.run_diagnostics["page_error_count"] == 1
    assert connector.run_diagnostics["records_emitted"] == 1


def test_accept_all_pages_relaxes_evidence_but_never_accepts_generic_listing() -> None:
    listing = FirecrawlDocument(
        url="https://jobs.example.test/careers",
        markdown="# Careers\nWelcome to our company.",
        html=None,
        metadata={"title": "Careers"},
        raw={},
    )
    candidate = FirecrawlDocument(
        url="https://jobs.example.test/careers/assistenz",
        markdown="# Teamassistenz\n\nAufgaben im Büro und Unterstützung der Leitung.",
        html=None,
        metadata={"title": "Teamassistenz"},
        raw={},
    )

    def connector(document: FirecrawlDocument, *, accept_all_pages: bool):
        return FirecrawlJobsConnector(
            (
                FirecrawlTarget(
                    document.url or "",
                    mode="scrape",
                    accept_all_pages=accept_all_pages,
                ),
            ),
            client=ScrapeClient(document),
            target_url_preparer=_prepare,
        )

    assert list(connector(listing, accept_all_pages=False).iter_jobs()) == []
    assert list(connector(listing, accept_all_pages=True).iter_jobs()) == []
    assert list(connector(candidate, accept_all_pages=False).iter_jobs()) == []
    records = list(connector(candidate, accept_all_pages=True).iter_jobs())
    assert len(records) == 1
    assert "target_accept_all_pages" in records[0].metadata["normalization_evidence"]


def test_job_portal_listing_pages_are_rejected_despite_job_signals() -> None:
    url = "https://www.stepstone.de/jobs/assistenz/in-deutschland"
    listing = FirecrawlDocument(
        url=f"{url}?page=2",
        markdown=(
            "# Assistenz: 21.788 Jobs & Stellenangebote in Deutschland\n"
            "Aufgaben, Anforderungen, Benefits und jetzt bewerben."
        ),
        html=None,
        metadata={"title": "Assistenz: 21.788 Jobs & Stellenangebote in Deutschland"},
        raw={},
    )
    strict = FirecrawlJobsConnector(
        (FirecrawlTarget(url, mode="scrape", target_kind="job_portal"),),
        client=ScrapeClient(listing),
        target_url_preparer=_prepare,
    )
    structured = FirecrawlJobsConnector(
        (FirecrawlTarget(url, mode="scrape", target_kind="job_portal"),),
        client=ScrapeClient(_json_ld_document(url)),
        target_url_preparer=_prepare,
    )

    assert list(strict.iter_jobs()) == []
    assert strict.run_diagnostics["pages_rejected"] == 1
    records = list(structured.iter_jobs())
    assert [record.title for record in records] == ["Executive Assistant to the CEO"]
    assert records[0].metadata["firecrawl_target_kind"] == "job_portal"


def test_unsafe_target_is_rejected_before_firecrawl_client_construction() -> None:
    builds = 0

    def client_factory(**_kwargs: Any) -> PollingClient:
        nonlocal builds
        builds += 1
        return PollingClient()

    connector = FirecrawlJobsConnector(
        (FirecrawlTarget("http://127.0.0.1/jobs", mode="scrape"),),
        client_factory=client_factory,
    )

    assert list(connector.iter_jobs()) == []
    assert builds == 0
    assert connector.run_diagnostics["target_error_count"] == 1

