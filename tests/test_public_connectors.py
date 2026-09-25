from __future__ import annotations

import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from jobradar.connectors import (  # noqa: E402
    AdzunaConnector,
    BoardTarget,
    ConfiguredJobBoardsConnector,
    JobicyConnector,
    JobicyFeedQuery,
    PersonioJobFeedConnector,
    RemotiveConnector,
    JoobleConnector,
)
from jobradar.connectors.base import JobQuery, RawJobRecord, SourceInfo  # noqa: E402


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _json_response(request: httpx.Request, payload, status: int = 200) -> httpx.Response:
    return httpx.Response(status, request=request, json=payload)


def test_smartrecruiters_fetches_details_and_normalizes_public_postings() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/postings"):
            assert request.url.params["limit"] == "50"
            assert request.url.params["offset"] == "0"
            return _json_response(
                request,
                {
                    "totalFound": 1,
                    "content": [{"id": "sr-1", "name": "Executive Assistant"}],
                },
            )
        return _json_response(
            request,
            {
                "id": "sr-1",
                "name": "Executive Assistant",
                "company": {"name": "Acme GmbH"},
                "location": {
                    "fullLocation": "Stuttgart, Baden-Württemberg, de",
                    "country": "de",
                    "remote": False,
                    "hybrid": True,
                },
                "releasedDate": "2026-07-20T10:00:00Z",
                "postingUrl": "https://jobs.smartrecruiters.com/acme/sr-1",
                "applyUrl": "https://jobs.smartrecruiters.com/acme/sr-1?apply=true",
                "typeOfEmployment": {"label": "Full-time"},
                "jobAd": {
                    "sections": {
                        "jobDescription": {
                            "title": "Job Description",
                            "text": "<p>Support the CEO</p>",
                        }
                    }
                },
            },
        )

    connector = SmartRecruitersJobBoardConnector("acme", client=_client(handler))
    records = list(connector.iter_jobs(JobQuery()))

    assert len(records) == 1
    assert paths == [
        "/v1/companies/acme/postings",
        "/v1/companies/acme/postings/sr-1",
    ]
    assert records[0].company == "Acme GmbH"
    assert records[0].locations == ("Stuttgart, Baden-Württemberg, de",)
    assert records[0].employment_types == ("Full-time",)
    assert records[0].metadata["remote_type"] == "hybrid"
    assert "Support the CEO" in (records[0].description or "")


def test_personio_normalizes_employer_xml_feed() -> None:
    xml = b"""<?xml version="1.0"?>
    <workzag-jobs>
      <position>
        <id>personio-1</id>
        <subcompany>Beispiel GmbH</subcompany>
        <office>Stuttgart</office>
        <additionalOffices><office>Remote</office></additionalOffices>
        <department>Executive Office</department>
        <name>Vorstandsassistenz</name>
        <jobDescriptions>
          <jobDescription>
            <name>Aufgaben</name>
            <value><![CDATA[<p>Unterst\xc3\xbctzung des Vorstands</p>]]></value>
          </jobDescription>
        </jobDescriptions>
        <employmentType>permanent</employmentType>
        <schedule>full-time</schedule>
        <createdAt>2026-07-20T09:00:00Z</createdAt>
      </position>
    </workzag-jobs>"""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://beispiel.jobs.personio.de/xml?language=de"
        return httpx.Response(
            200,
            request=request,
            content=xml,
            headers={"Content-Type": "text/xml"},
        )

    connector = PersonioJobFeedConnector("beispiel", client=_client(handler))
    records = list(connector.iter_jobs())

    assert len(records) == 1
    assert records[0].source_job_id == "personio-1"
    assert records[0].locations == ("Stuttgart", "Remote")
    assert records[0].remote is True
    assert records[0].employment_types == ("permanent", "full-time")
    assert records[0].canonical_url == (
        "https://beispiel.jobs.personio.de/job/personio-1?language=de"
    )
    assert "<position>" in records[0].raw["xml"]


def test_arbeitnow_stops_incremental_pagination_at_overlap_cutoff() -> None:
    pages: list[int] = []
    new_timestamp = int(datetime(2026, 7, 20, tzinfo=UTC).timestamp())
    old_timestamp = int(datetime(2026, 6, 1, tzinfo=UTC).timestamp())

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        pages.append(page)
        return _json_response(
            request,
            {
                "data": [
                    {
                        "slug": "new-1",
                        "title": "Assistenz der Geschäftsführung",
                        "company_name": "Acme GmbH",
                        "description": "<p>Direkte Unterstützung</p>",
                        "remote": False,
                        "url": "https://www.arbeitnow.com/jobs/new-1",
                        "tags": ["Assistant"],
                        "job_types": ["full-time"],
                        "location": "Stuttgart",
                        "created_at": new_timestamp,
                    },
                    {
                        "slug": "old-1",
                        "title": "Alte Stelle",
                        "created_at": old_timestamp,
                    },
                ],
                "links": {"next": "https://www.arbeitnow.com/api/job-board-api?page=2"},
            },
        )

    connector = ArbeitnowConnector(
        stop_before=datetime(2026, 7, 1, tzinfo=UTC),
        client=_client(handler),
    )
    records = list(connector.iter_jobs())

    assert pages == [1]
    assert [record.source_job_id for record in records] == ["new-1"]
    assert records[0].locations == ("Stuttgart",)
    assert records[0].metadata["country"] == "DE"
    assert connector.run_diagnostics["cutoff_reached"] is True


def test_jobicy_deduplicates_and_excludes_jobs_unavailable_in_germany() -> None:
    requests: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(dict(request.url.params))
        return _json_response(
            request,
            {
                "jobs": [
                    {
                        "id": 1,
                        "jobTitle": "Executive Assistant",
                        "companyName": "Remote GmbH",
                        "jobGeo": "Europe",
                        "jobDescription": "<p>Support the CEO</p>",
                        "url": "https://jobicy.com/jobs/1",
                        "pubDate": "2026-07-20T08:00:00Z",
                        "jobType": ["Full-Time"],
                        "salaryMin": 55000,
                        "salaryMax": 65000,
                        "salaryCurrency": "EUR",
                        "salaryPeriod": "year",
                    },
                    {
                        "id": 2,
                        "jobTitle": "US Assistant",
                        "jobGeo": "USA only",
                        "url": "https://jobicy.com/jobs/2",
                    },
                ]
            },
        )

    connector = JobicyConnector(
        feed_queries=(
            JobicyFeedQuery(industry="admin-support"),
            JobicyFeedQuery(tag="chief of staff"),
        ),
        client=_client(handler),
    )
    records = list(connector.iter_jobs())

    assert requests == [
        {"count": "100", "industry": "admin-support"},
        {"count": "100", "tag": "chief of staff"},
    ]
    assert [record.source_job_id for record in records] == ["1"]
    assert records[0].salary == "55000–65000 EUR year"
    assert records[0].metadata["country"] == "DE"
    assert connector.run_diagnostics["duplicates_skipped"] == 1
    assert connector.run_diagnostics["location_filtered"] == 2


def test_remotive_keeps_only_remote_jobs_available_in_germany() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/remote-jobs"
        return _json_response(
            request,
            {
                "jobs": [
                    {
                        "id": 10,
                        "title": "Chief of Staff",
                        "company_name": "Global GmbH",
                        "candidate_required_location": "Worldwide",
                        "description": "<p>Executive team</p>",
                        "url": "https://remotive.com/remote-jobs/10",
                        "publication_date": "2026-07-19T08:00:00",
                        "job_type": "full_time",
                    },
                    {
                        "id": 11,
                        "title": "US only",
                        "candidate_required_location": "USA",
                        "url": "https://remotive.com/remote-jobs/11",
                    },
                ]
            },
        )

    connector = RemotiveConnector(client=_client(handler))
    records = list(connector.iter_jobs())

    assert [record.source_job_id for record in records] == ["10"]
    assert records[0].remote is True
    assert records[0].metadata["country"] == "DE"
    assert connector.run_diagnostics["location_filtered"] == 1


def test_adzuna_paginates_through_results() -> None:
    pages = []
    def handler(request):
        pages.append(dict(request.url.params))
        page = int(dict(request.url.params).get('page', '1'))
        return _json_response(request, {
            'results': [
                {'id': 100+page, 'title': f'Assistenz Page {page}', 'company': {'display_name': 'Firma'},
                 'location': {'display_name': 'Stuttgart'}, 'description': 'Beschreibung',
                 'redirect_url': 'https://example.com/job/100',
                 'created': '2026-08-01T10:00:00Z',
                 'salary_min': 30000, 'salary_max': 45000,
                 'contract_type': 'permanent', 'contract_time': 'full_time'}
                for _ in range(2)
            ] if page <= 2 else []
        })
    connector = AdzunaConnector(
        app_id='test', api_key='test', max_pages=2,
        client=_client(handler),
    )
    records = list(connector.iter_jobs())
    assert len(records) == 4
    assert all(r.source == 'adzuna' for r in records)
    assert all(r.title.startswith('Assistenz') for r in records)
    assert connector.run_diagnostics['pages_read'] == 2


def test_adzuna_stops_at_overlap_cutoff() -> None:
    from datetime import UTC, datetime, timedelta
    def handler(request):
        return _json_response(request, {
            'results': [
                {'id': i, 'title': 'Job', 'company': {'display_name': 'F'},
                 'location': {'display_name': 'L'}, 'redirect_url': f'https://ex.com/{i}',
                 'created': '2026-07-15T10:00:00Z'}
                for i in range(2)
            ]
        })
    stop = datetime(2026, 8, 1, tzinfo=UTC)
    connector = AdzunaConnector(
        app_id='test', api_key='test', max_pages=3,
        client=_client(handler), stop_before=stop,
    )
    records = list(connector.iter_jobs())
    assert len(records) == 0
    assert connector.run_diagnostics['cutoff_reached'] is True


def test_adzuna_formats_salary_range() -> None:
    def handler(request):
        return _json_response(request, {
            'results': [
                {'id': 1, 'title': 'Job', 'company': {'display_name': 'F'},
                 'location': {'display_name': 'L'}, 'redirect_url': 'https://ex.com/1',
                 'created': '2026-08-01T10:00:00Z',
                 'salary_min': 35000, 'salary_max': 50000}
            ]
        })
    connector = AdzunaConnector(
        app_id='test', api_key='test', max_pages=1, client=_client(handler),
    )
    records = list(connector.iter_jobs())
    assert '35.000' in (records[0].salary or '')
    assert '50.000' in (records[0].salary or '')


def test_jooble_paginates_and_normalizes() -> None:
    pages = []
    def handler(request):
        pages.append(1)
        body = None
        return _json_response(request, {
            'totalCount': 4,
            'jobs': [
                {'title': 'Assistenz', 'location': 'Stuttgart',
                 'snippet': 'Beschreibung...', 'salary': '3000-4000 EUR',
                 'type': 'Full-time', 'source': 'Indeed',
                 'link': f'https://ex.com/job/{i}',
                 'updated': '2026-08-01T10:00:00Z'}
                for i in range(2)
            ] if len(pages) <= 2 else []
        })
    connector = JoobleConnector(
        api_key='test', max_pages=1,
        client=_client(handler),
    )
    records = list(connector.iter_jobs())
    assert len(records) == 2
    assert records[0].source == 'jooble'
    assert records[0].company is None  # Jooble doesn't have company field
    assert '3000' in (records[0].salary or '')


def test_jooble_handles_missing_id() -> None:
    def handler(request):
        return _json_response(request, {
            'totalCount': 1, 'jobs': [
                {'title': 'Job', 'snippet': 'desc',
                 'link': 'https://ex.com/job/1', 'updated': '2026-08-01T10:00:00Z'}
            ]
        })
    connector = JoobleConnector(api_key='test', client=_client(handler))
    records = list(connector.iter_jobs())
    assert len(records) == 1
    assert len(records[0].source_job_id) == 24

class _FakeBoardConnector:
    source_info = SourceInfo(
        key="fake", name="Fake", acquisition="test", official=True
    )

    def __init__(self, target: BoardTarget) -> None:
        self.target = target

    def iter_jobs(self, query: JobQuery | None = None) -> Iterator[RawJobRecord]:
        del query
        if self.target.identifier == "broken":
            raise RuntimeError("board unavailable")
        yield RawJobRecord(
            source=f"fake:{self.target.identifier}",
            source_job_id="tenant-local-1",
            title="Executive Assistant",
        )


def test_configured_boards_namespace_ids_and_isolate_target_failures() -> None:
    connector = ConfiguredJobBoardsConnector(
        source_key="fake_boards",
        source_name="Fake boards",
        documentation_url="https://example.test/docs",
        targets=(BoardTarget("acme"), BoardTarget("broken"), BoardTarget("example")),
        builder=_FakeBoardConnector,
    )

    records = list(connector.iter_jobs())

    assert [record.source_job_id for record in records] == [
        "acme:tenant-local-1",
        "example:tenant-local-1",
    ]
    assert connector.run_diagnostics["targets_completed"] == 2
    assert connector.run_diagnostics["target_error_count"] == 1
    assert connector.run_diagnostics["target_errors"][0]["target"] == "broken"
