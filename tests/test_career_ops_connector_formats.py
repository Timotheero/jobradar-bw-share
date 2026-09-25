from __future__ import annotations

from xml.etree import ElementTree

import httpx

from jobradar.connectors.base import JobQuery, PaginationOptions
from jobradar.connectors.icims import _normalize_icims
from jobradar.connectors.rheinmetall import RheinmetallConnector
from jobradar.connectors.softgarden import SoftgardenConnector
from jobradar.connectors.teamtailor import _normalize_teamtailor


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_softgarden_parses_server_rendered_vacancy() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            headers={"content-type": "text/html"},
            text="""
            <div id="jobSearchCss"></div>
            <a class="brand">Acme GmbH</a>
            <article class="matchElement" id="job_id_123">
              <a href="/de/job/123/executive-assistant">Executive Assistant</a>
              <span class="ProjectGeoLocationCity">Stuttgart</span>
            </article>
            """,
        )

    connector = SoftgardenConnector("acme", client=_client(handler))
    try:
        records = list(connector.iter_jobs())
    finally:
        connector.close()

    assert len(records) == 1
    assert records[0].source_job_id == "123"
    assert records[0].company == "Acme GmbH"


def test_teamtailor_and_icims_normalize_provider_formats() -> None:
    item = ElementTree.fromstring(
        """
        <item>
          <title>Executive Assistant</title>
          <link>https://jobs.acme.example/jobs/tt-1</link>
          <guid>tt-1</guid>
          <description><![CDATA[<p>Assist the board.</p>]]></description>
          <location>Stuttgart</location>
        </item>
        """
    )
    teamtailor = _normalize_teamtailor("teamtailor:acme", "Acme GmbH", item)
    icims = _normalize_icims(
        "icims:acme",
        "Acme GmbH",
        "icims-1",
        "https://acme.icims.com/jobs/1/job",
        None,
        "<a>Executive Assistant</a>",
        "<script type='application/ld+json'></script>",
        {
            "title": "Executive Assistant",
            "description": "<p>Assist the board.</p>",
            "url": "https://acme.icims.com/jobs/1/job",
            "jobLocation": {"address": {"addressLocality": "Stuttgart"}},
        },
    )

    assert teamtailor is not None and teamtailor.raw["xml"].startswith("<item>")
    assert icims is not None and icims.locations == ("Stuttgart",)
    assert icims.raw["json_ld"]["title"] == "Executive Assistant"


def test_rheinmetall_stops_after_one_bounded_html_page() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            request=request,
            headers={"content-type": "text/html"},
            text="""
            <section id="anchor-jobsuche">
              <a href="/de/job/12345">
                Executive Assistant | Rheinmetall AG | Stuttgart
              </a>
            </section>
            """,
        )

    connector = RheinmetallConnector(client=_client(handler))
    try:
        records = list(
            connector.iter_jobs(JobQuery(pagination=PaginationOptions(max_pages=1)))
        )
    finally:
        connector.close()

    assert len(requests) == 1
    assert len(records) == 1
    assert records[0].source_job_id == "12345"
