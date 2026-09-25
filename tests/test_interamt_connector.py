from __future__ import annotations

import httpx

from jobradar.connectors.base import JobQuery, PaginationOptions
from jobradar.connectors.interamt import InteramtConnector


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


def test_interamt_submits_public_search_and_loads_bounded_ajax_results() -> None:
    requests: list[httpx.Request] = []
    home = """
    <form class="ia-m-form ia-m-form--stage-quicksearch" action="./?submit-search">
      <input name="idOrSuchtext">
    </form>
    """
    first = """
    <script>Wicket.Ajax.baseUrl="koop/app/trefferliste?2";</script>
    <script>Wicket.Ajax.ajax({"u":"./trefferliste?2-loadMoreLink","c":"id1a"});</script>
    <table><tbody>
      <tr class="ia-e-table__row">
        <td data-field="StellenangebotId">1480001</td>
        <td data-field="Behoerde">Land Baden-Württemberg</td>
        <td data-field="Stellenbezeichnung">Vorstandsassistenz</td>
        <td data-field="PLZOrte">70173 Stuttgart</td>
        <td data-field="Dienstort">Hybrid</td>
        <td data-field="Von">12.08.2026</td>
        <td data-field="Bewerbungsfrist">01.09.2026</td>
        <td data-field="TarifEbeneDisplayString">TV-L E 9b</td>
      </tr>
    </tbody></table>
    """
    second = """
    <ajax-response><component><![CDATA[
      <tbody><tr class="ia-e-table__row">
        <td data-field="StellenangebotId">1480002</td>
        <td data-field="Behoerde">Stadt Stuttgart</td>
        <td data-field="Stellenbezeichnung">Assistenz der Amtsleitung</td>
        <td data-field="PLZOrte">70182 Stuttgart</td>
        <td data-field="Dienstort">Vor Ort</td>
      </tr></tbody>
    ]]></component></ajax-response>
    """

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(200, request=request, text=home)
        if len(requests) == 2:
            assert request.method == "POST"
            assert b"idOrSuchtext=Vorstandsassistenz" in request.content
            assert b"PLZ=Stuttgart" in request.content
            return httpx.Response(200, request=request, text=first)
        assert request.headers["wicket-ajax"] == "true"
        return httpx.Response(200, request=request, text=second)

    connector = InteramtConnector(client=_client(handler))
    records = list(
        connector.iter_jobs(
            JobQuery(
                text="Vorstandsassistenz",
                location="Stuttgart",
                pagination=PaginationOptions(max_pages=3),
            )
        )
    )

    assert len(requests) == 3
    assert [record.source_job_id for record in records] == ["1480001", "1480002"]
    assert records[0].company == "Land Baden-Württemberg"
    assert records[0].locations == ("70173 Stuttgart",)
    assert records[0].remote is True
    assert records[0].salary == "TV-L E 9b"
    assert records[0].canonical_url == "https://interamt.de/koop/app/stelle?id=1480001"
    assert records[1].remote is False


def test_interamt_rejects_unexpected_home_page_without_posting() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, request=request, text="<html>maintenance</html>")

    connector = InteramtConnector(client=_client(handler))

    try:
        list(connector.iter_jobs(JobQuery(text="Assistenz")))
    except ValueError as exc:
        assert "quick-search form" in str(exc)
    else:
        raise AssertionError("expected malformed search page to fail")
    assert calls == 1
