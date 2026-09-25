from __future__ import annotations

import base64
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from jobradar.connectors import (  # noqa: E402
    AshbyJobBoardConnector,
    BAJobsucheConnector,
    FirecrawlV2Client,
    GreenhouseJobBoardConnector,
    HTTPTransport,
    JobQuery,
    LeverJobBoardConnector,
    PaginationOptions,
    RetryPolicy,
)


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _json_response(request: httpx.Request, payload, status: int = 200) -> httpx.Response:
    return httpx.Response(status, request=request, json=payload)


def test_http_transport_retries_idempotent_gets() -> None:
    calls = 0
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return _json_response(request, {"error": "temporary"}, status=503)
        return _json_response(request, {"ok": True})

    transport = HTTPTransport(
        "https://example.test",
        client=_client(handler),
        retry=RetryPolicy(max_attempts=2, backoff_seconds=0),
        sleeper=delays.append,
    )

    assert transport.request_json("GET", "/jobs") == {"ok": True}
    assert calls == 2
    assert delays == [0]


def test_ba_jobsuche_uses_v6_search_v4_details_and_paginates() -> None:
    search_pages: list[int] = []
    detail_references: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-api-key"] == "jobboerse-jobsuche"
        if request.url.path.endswith("/pc/v6/jobs"):
            page = int(request.url.params["page"])
            search_pages.append(page)
            reference = f"10001-{page}-S"
            key = "refnr" if page == 1 else "referenznummer"
            return _json_response(
                request,
                {
                    "ergebnisliste": [
                        {
                            key: reference,
                            "hauptberuf": "Assistenz der Geschäftsführung",
                            "firma": "Beispiel GmbH",
                        }
                    ],
                    "maxErgebnisse": "2",
                },
            )
        if "/pc/v4/jobdetails/" in request.url.path:
            encoded = request.url.path.rsplit("/", 1)[-1]
            reference = base64.b64decode(encoded).decode("utf-8")
            detail_references.append(reference)
            return _json_response(
                request,
                {
                    "referenznummer": reference,
                    "stellenangebotsTitel": "Assistenz der Geschäftsführung",
                    "stellenangebotsBeschreibung": "Direkte Unterstützung der Geschäftsführung",
                    "firma": "Beispiel GmbH",
                    "stellenlokationen": [
                        {
                            "adresse": {
                                "plz": "70173",
                                "ort": "Stuttgart",
                                "region": "BADEN_WUERTTEMBERG",
                                "land": "DEUTSCHLAND",
                            }
                        }
                    ],
                    "arbeitszeitVollzeit": True,
                    "arbeitszeitTeilzeitFlexibel": True,
                    "homeofficemoeglich": True,
                    "homeofficetyp": "NACH_VEREINBARUNG",
                    "datumErsteVeroeffentlichung": "2026-07-01",
                    "aenderungsdatum": "2026-07-15T10:30:00Z",
                    "verguetungsangabe": "KEINE_ANGABEN",
                    "vertragsdauer": "UNBEFRISTET",
                    "istArbeitnehmerUeberlassung": False,
                },
            )
        raise AssertionError(f"unexpected request: {request.url}")

    connector = BAJobsucheConnector(client=_client(handler))
    records = list(
        connector.iter_jobs(
            JobQuery(
                text="Assistenz der Geschäftsführung",
                location="Baden-Württemberg",
                pagination=PaginationOptions(page_size=1),
            )
        )
    )

    assert search_pages == [1, 2]
    assert detail_references == ["10001-1-S", "10001-2-S"]
    assert [record.source_job_id for record in records] == detail_references
    assert records[0].locations == ("70173 Stuttgart, BADEN_WUERTTEMBERG",)
    assert records[0].company == "Beispiel GmbH"
    assert records[0].employment_types == ("VOLLZEIT", "TEILZEIT")
    assert records[0].remote is True
    assert records[0].salary is None
    assert records[0].metadata["contract_type"] == "UNBEFRISTET"
    assert records[0].metadata["state"] == "Baden-Württemberg"
    assert records[0].metadata["remote_type"] == "hybrid"
    assert records[0].published_at == datetime(2026, 7, 1, tzinfo=UTC)
    assert records[0].updated_at == datetime(2026, 7, 15, 10, 30, tzinfo=UTC)
    assert connector.run_diagnostics["search_requests"] == 2
    assert connector.run_diagnostics["detail_requests"] == 2
    assert BAJobsucheConnector.source_info.experimental is True
    assert BAJobsucheConnector.source_info.default_enabled is True
    assert BAJobsucheConnector.source_info.official is False



def test_ba_jobsuche_deduplicates_overlapping_queries_before_detail_fetch() -> None:
    detail_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal detail_calls
        if request.url.path.endswith("/pc/v6/jobs"):
            return _json_response(
                request,
                {
                    "ergebnisliste": [
                        {
                            "referenznummer": "10001-overlap-S",
                            "stellenangebotsTitel": "Executive Assistant",
                        }
                    ],
                    "maxErgebnisse": 1,
                },
            )
        detail_calls += 1
        return _json_response(
            request,
            {
                "referenznummer": "10001-overlap-S",
                "stellenangebotsTitel": "Executive Assistant",
            },
        )

    connector = BAJobsucheConnector(client=_client(handler))

    first = list(connector.iter_jobs(JobQuery(text="Assistenz")))
    second = list(connector.iter_jobs(JobQuery(text="Geschäftsführung")))

    assert len(first) == 1
    assert second == []
    assert detail_calls == 1
    assert connector.run_diagnostics["duplicate_summaries"] == 1


def test_ba_jobsuche_rejects_incomplete_result_windows_and_oversized_pages() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(
            request,
            {
                "ergebnisliste": [
                    {
                        "referenznummer": "10001-window-S",
                        "stellenangebotsTitel": "Assistenz",
                    }
                ],
                "maxErgebnisse": 10_001,
            },
        )

    connector = BAJobsucheConnector(client=_client(handler), include_details=False)

    with pytest.raises(RuntimeError, match="10,000-result API window"):
        list(connector.iter_jobs(JobQuery()))
    with pytest.raises(ValueError, match="page_size must not exceed 100"):
        list(
            connector.iter_jobs(
                JobQuery(pagination=PaginationOptions(page_size=101, max_pages=1))
            )
        )


def test_firecrawl_v2_explicit_calls_and_result_pagination() -> None:
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, str(request.url)))
        assert request.headers["authorization"] == "Bearer test-key"
        if request.method == "POST" and request.url.path.endswith("/v2/scrape"):
            body = json.loads(request.content)
            assert body["url"] == "https://example.test/jobs/1"
            return _json_response(
                request,
                {
                    "success": True,
                    "data": {
                        "markdown": "# Job",
                        "metadata": {"sourceURL": body["url"]},
                    },
                },
            )
        if request.method == "POST" and request.url.path.endswith("/v2/crawl"):
            return _json_response(request, {"success": True, "id": "crawl-1", "status": "queued"})
        if request.method == "GET" and request.url.path.endswith("/v2/crawl/crawl-1"):
            if request.url.params.get("skip") == "1":
                return _json_response(
                    request,
                    {
                        "status": "completed",
                        "data": [
                            {
                                "markdown": "second",
                                "metadata": {"sourceURL": "https://example.test/2"},
                            }
                        ],
                        "completed": 2,
                        "total": 2,
                    },
                )
            return _json_response(
                request,
                {
                    "status": "completed",
                    "data": [
                        {
                            "markdown": "first",
                            "metadata": {"sourceURL": "https://example.test/1"},
                        }
                    ],
                    "next": "https://api.firecrawl.dev/v2/crawl/crawl-1?skip=1",
                    "completed": 1,
                    "total": 2,
                },
            )
        raise AssertionError(f"unexpected request: {request.url}")

    firecrawl = FirecrawlV2Client("test-key", client=_client(handler))
    scraped = firecrawl.scrape("https://example.test/jobs/1")
    started = firecrawl.start_crawl("https://example.test/jobs", limit=10)
    documents = list(firecrawl.iter_crawl_documents(started.job_id))

    assert scraped.markdown == "# Job"
    assert started.job_id == "crawl-1"
    assert [document.markdown for document in documents] == ["first", "second"]
    assert [method for method, _ in calls] == ["POST", "POST", "GET", "GET"]


def test_firecrawl_self_hosted_custom_url_needs_no_api_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "firecrawl.internal"
        assert request.url.path == "/v2/scrape"
        assert "authorization" not in request.headers
        return _json_response(
            request,
            {
                "success": True,
                "data": {
                    "markdown": "self-hosted",
                    "metadata": {"sourceURL": "https://example.test/job"},
                },
            },
        )

    firecrawl = FirecrawlV2Client(
        base_url="http://firecrawl.internal:3002/v2",
        client=_client(handler),
    )

    document = firecrawl.scrape("https://example.test/job")
    assert document.markdown == "self-hosted"


def test_greenhouse_normalizes_public_board_jobs() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/v1/boards/acme/jobs")
        assert request.url.params["content"] == "true"
        return _json_response(
            request,
            {
                "name": "Acme GmbH",
                "jobs": [
                    {
                        "id": 42,
                        "title": "Executive Assistant",
                        "location": {"name": "Stuttgart / Remote"},
                        "content": "<p>Support the CEO</p>",
                        "absolute_url": "https://boards.greenhouse.io/acme/jobs/42",
                        "updated_at": "2026-07-15T11:00:00Z",
                        "metadata": [{"name": "Employment Type", "value": "Full-time"}],
                    }
                ],
            },
        )

    connector = GreenhouseJobBoardConnector("acme", client=_client(handler))
    records = list(connector.iter_jobs(JobQuery(text="Executive Assistant")))

    assert len(records) == 1
    assert records[0].company == "Acme GmbH"
    assert records[0].employment_types == ("Full-time",)
    assert records[0].remote is True
    assert records[0].raw["id"] == 42


def test_lever_paginates_and_normalizes_public_postings() -> None:
    skips: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        skip = int(request.url.params["skip"])
        skips.append(skip)
        number = skip + 1
        return _json_response(
            request,
            [
                {
                    "id": f"lever-{number}",
                    "text": f"Executive Assistant {number}",
                    "categories": {
                        "location": "Karlsruhe",
                        "commitment": "Full-time",
                        "department": "Operations",
                    },
                    "descriptionPlain": "Support management",
                    "hostedUrl": f"https://jobs.lever.co/acme/{number}",
                    "applyUrl": f"https://jobs.lever.co/acme/{number}/apply",
                    "createdAt": 1784116800000,
                    "workplaceType": "hybrid",
                }
            ],
        )

    connector = LeverJobBoardConnector("acme", company="Acme GmbH", client=_client(handler))
    records = list(
        connector.iter_jobs(
            JobQuery(
                text="Executive Assistant",
                pagination=PaginationOptions(page_size=1, max_pages=2),
            )
        )
    )

    assert skips == [0, 1]
    assert [record.source_job_id for record in records] == ["lever-1", "lever-2"]
    assert records[0].locations == ("Karlsruhe",)
    assert records[0].employment_types == ("Full-time",)


def test_ashby_normalizes_public_board_jobs() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/posting-api/job-board/acme")
        assert request.url.params["includeCompensation"] == "true"
        return _json_response(
            request,
            {
                "jobs": [
                    {
                        "title": "Assistenz der Geschäftsführung",
                        "department": "Executive Office",
                        "employmentType": "FullTime",
                        "location": "Mannheim",
                        "secondaryLocations": [{"name": "Heidelberg"}],
                        "publishedAt": "2026-07-10T08:00:00Z",
                        "isRemote": False,
                        "descriptionPlain": "Unterstützung der Geschäftsführung",
                        "jobUrl": "https://jobs.ashbyhq.com/acme/ashby-1",
                        "applyUrl": "https://jobs.ashbyhq.com/acme/ashby-1/application",
                        "compensation": {
                            "scrapeableCompensationSalarySummary": "50.000–60.000 EUR"
                        },
                    }
                ]
            },
        )

    connector = AshbyJobBoardConnector("acme", company="Acme GmbH", client=_client(handler))
    records = list(connector.iter_jobs(JobQuery(location="Mannheim")))

    assert len(records) == 1
    assert records[0].source_job_id == "ashby-1"
    assert records[0].locations == ("Mannheim", "Heidelberg")
    assert records[0].employment_types == ("FullTime",)
    assert records[0].remote is False
    assert records[0].salary == "50.000–60.000 EUR"


def test_construction_never_starts_network_activity() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise AssertionError(f"construction triggered a request: {request.url}")

    client = _client(handler)
    connectors = [
        BAJobsucheConnector(client=client),
        FirecrawlV2Client(client=client),
        GreenhouseJobBoardConnector("board", client=client),
        LeverJobBoardConnector("site", client=client),
        AshbyJobBoardConnector("board", client=client),
    ]

    assert connectors
    assert calls == 0
