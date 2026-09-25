"""Experimental connector for the BA Jobsuche application's internal API.

This is not an officially documented public read API. The connector is marked
experimental on purpose. A global crawl/source policy must be checked by the
application before calling :meth:`iter_jobs`; this module does not own that
policy switch and does not perform work at import or construction time.
"""

from __future__ import annotations

import base64
from collections.abc import Iterator, Mapping
from typing import Any
from urllib.parse import quote

import httpx

from .base import JobQuery, RawJobRecord, SourceInfo, parse_datetime, string_tuple
from .http import HTTPTransport, RetryPolicy, TimeoutOptions


class BAJobsucheConnector:
    """Read search results from v6 and individual details from v4."""

    BASE_URL = "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service"
    PUBLIC_CLIENT_ID = "jobboerse-jobsuche"
    MAX_PAGE_SIZE = 100
    MAX_RESULT_WINDOW = 10_000

    source_info = SourceInfo(
        key="ba_jobsuche",
        name="Bundesagentur für Arbeit – Jobsuche",
        acquisition="undocumented_internal_api",
        official=False,
        default_enabled=True,
        experimental=True,
        documentation_url="https://github.com/bundesAPI/jobsuche-api",
        terms_url="https://www.arbeitsagentur.de/nutzungsbedingungen",
        notes=(
            "Technically public application endpoint, but not an official public read API. "
            "The application must enforce its global crawl/source policy before use."
        ),
    )

    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        timeout: TimeoutOptions | None = None,
        retry: RetryPolicy | None = None,
        public_client_id: str = PUBLIC_CLIENT_ID,
        include_details: bool = True,
        skip_missing_details: bool = True,
    ) -> None:
        self.include_details = include_details
        self.skip_missing_details = skip_missing_details
        self._seen_references: set[str] = set()
        self._diagnostics = {
            "search_requests": 0,
            "detail_requests": 0,
            "duplicate_summaries": 0,
            "missing_references": 0,
            "missing_details": 0,
            "records_emitted": 0,
        }
        self._http = HTTPTransport(
            self.BASE_URL,
            client=client,
            timeout=timeout,
            retry=retry,
            default_headers={
                "Accept": "application/json",
                "X-API-Key": public_client_id,
            },
        )

    def close(self) -> None:
        self._http.close()

    @property
    def run_diagnostics(self) -> Mapping[str, int]:
        return dict(self._diagnostics)

    def iter_jobs(self, query: JobQuery | None = None) -> Iterator[RawJobRecord]:
        query = query or JobQuery()
        pagination = query.pagination
        if pagination.page_size > self.MAX_PAGE_SIZE:
            raise ValueError(f"BA Jobsuche page_size must not exceed {self.MAX_PAGE_SIZE}")
        page = pagination.start_page
        pages_read = 0

        while pagination.max_pages is None or pages_read < pagination.max_pages:
            params = self._search_params(query, page)
            payload = self._http.request_json("GET", "/pc/v6/jobs", params=params)
            self._diagnostics["search_requests"] += 1
            summaries = _extract_search_items(payload)
            if not summaries:
                return

            total = _as_int(payload.get("maxErgebnisse")) if isinstance(payload, Mapping) else None
            if (
                total is not None
                and total > self.MAX_RESULT_WINDOW
                and pagination.max_pages is None
            ):
                raise RuntimeError(
                    "BA Jobsuche query exceeds the 10,000-result API window; "
                    "split it into narrower queries to avoid incomplete coverage"
                )

            for summary in summaries:
                reference = _first_text(summary, "referenznummer", "refnr")
                if not reference:
                    self._diagnostics["missing_references"] += 1
                    continue
                if reference in self._seen_references:
                    self._diagnostics["duplicate_summaries"] += 1
                    continue
                self._seen_references.add(reference)
                detail: Mapping[str, Any] = {}
                if self.include_details:
                    try:
                        detail = self.fetch_detail(reference)
                    except httpx.HTTPStatusError as exc:
                        if not (self.skip_missing_details and exc.response.status_code == 404):
                            raise
                        self._diagnostics["missing_details"] += 1
                record = _normalize_ba_job(reference, summary, detail)
                if record is not None:
                    self._diagnostics["records_emitted"] += 1
                    yield record

            pages_read += 1
            if total is not None and page * pagination.page_size >= total:
                return
            if len(summaries) < pagination.page_size:
                return
            page += 1

    def fetch_detail(self, reference_number: str) -> Mapping[str, Any]:
        """Fetch one job by the Base64-encoded BA reference number."""

        self._diagnostics["detail_requests"] += 1
        encoded = base64.b64encode(reference_number.encode("utf-8")).decode("ascii")
        payload = self._http.request_json("GET", f"/pc/v4/jobdetails/{quote(encoded, safe='')}")
        if not isinstance(payload, Mapping):
            raise TypeError("BA job detail response must be a JSON object")
        return payload

    @staticmethod
    def _search_params(query: JobQuery, page: int) -> dict[str, Any]:
        params: dict[str, Any] = {
            "angebotsart": 1,
            "pav": "false",
            "zeitarbeit": "true",
        }
        params.update(query.filters)
        if query.text:
            params["was"] = query.text
        if query.location:
            params["wo"] = query.location
        if query.radius_km is not None:
            params["umkreis"] = query.radius_km
        if query.published_since_days is not None:
            params["veroeffentlichtseit"] = query.published_since_days
        params["page"] = page
        params["size"] = query.pagination.page_size
        return params


def _extract_search_items(payload: Any) -> list[Mapping[str, Any]]:
    if not isinstance(payload, Mapping):
        raise TypeError("BA search response must be a JSON object")
    for key in ("ergebnisliste", "stellenangebote", "jobs", "items", "results"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, Mapping)]
    return []


def _normalize_ba_job(
    reference: str,
    summary: Mapping[str, Any],
    detail: Mapping[str, Any],
) -> RawJobRecord | None:
    title = _first_text(detail, "stellenangebotsTitel", "titel") or _first_text(
        summary, "stellenangebotsTitel", "titel", "beruf"
    )
    if not title:
        return None

    location_value = detail.get("stellenlokationen") or detail.get("arbeitsorte")
    if not location_value:
        location_value = (
            summary.get("stellenlokationen")
            or summary.get("arbeitsorte")
            or summary.get("arbeitsort")
        )
    locations = _ba_locations(location_value)

    employment_types = _employment_types(detail, summary)
    external_url = _first_text(
        detail, "externeUrl", "externeURL", "bewerbungsUrl", "bewerbungsURL"
    ) or _first_text(summary, "externeUrl", "externeURL")
    canonical_url = f"https://www.arbeitsagentur.de/jobsuche/jobdetail/{quote(reference, safe='')}"
    remote = _remote_value(employment_types, detail, summary)
    homeoffice_type = _first_text(detail, "homeofficetyp") or _first_text(
        summary, "homeofficetyp"
    )

    metadata = {
        "reference_number": reference,
        "offer_type": (
            detail.get("stellenangebotsart")
            or detail.get("angebotsart")
            or summary.get("stellenangebotsart")
            or summary.get("angebotsart")
        ),
        "occupation": (
            detail.get("hauptberuf")
            or detail.get("beruf")
            or summary.get("hauptberuf")
            or summary.get("beruf")
        ),
        "contract_type": _ba_optional_text(detail, summary, "vertragsdauer", "befristung"),
        "industry": detail.get("branche") or summary.get("branche"),
        "main_dkz": detail.get("hauptDkz"),
        "employer_hash": (
            detail.get("arbeitgeberKundennummerHash")
            or detail.get("arbeitgeberHashId")
            or summary.get("arbeitgeberKundennummerHash")
        ),
        "remote_type": _ba_remote_type(remote, homeoffice_type),
        "homeoffice_type": homeoffice_type,
        "temporary_agency_work": detail.get("istArbeitnehmerUeberlassung"),
        "private_placement": detail.get("istPrivateArbeitsvermittlung"),
        "experimental": True,
        **_primary_location_metadata(location_value),
    }

    return RawJobRecord(
        source="ba_jobsuche",
        source_job_id=reference,
        title=title,
        company=_first_text(detail, "firma", "arbeitgeber")
        or _first_text(summary, "firma", "arbeitgeber"),
        locations=locations,
        description=_first_text(detail, "stellenangebotsBeschreibung", "stellenbeschreibung"),
        description_format="text",
        canonical_url=canonical_url,
        apply_url=external_url or canonical_url,
        published_at=parse_datetime(
            detail.get("datumErsteVeroeffentlichung")
            or detail.get("ersteVeroeffentlichungsdatum")
            or detail.get("aktuelleVeroeffentlichungsdatum")
            or summary.get("datumErsteVeroeffentlichung")
            or summary.get("aktuelleVeroeffentlichungsdatum")
        ),
        updated_at=parse_datetime(
            detail.get("aenderungsdatum")
            or detail.get("modifikationsTimestamp")
            or summary.get("aenderungsdatum")
            or summary.get("modifikationsTimestamp")
        ),
        employment_types=employment_types,
        remote=remote,
        salary=_ba_optional_text(detail, summary, "verguetungsangabe", "verguetung"),
        metadata={key: value for key, value in metadata.items() if value is not None},
        raw={"summary": dict(summary), "detail": dict(detail)},
    )


def _ba_locations(value: Any) -> tuple[str, ...]:
    values = value if isinstance(value, list) else [value]
    result: list[str] = []
    for item in values:
        if isinstance(item, str):
            rendered = item.strip()
        elif isinstance(item, Mapping):
            nested_address = item.get("adresse")
            address = nested_address if isinstance(nested_address, Mapping) else item
            postcode = str(address.get("plz") or "").strip()
            city = str(address.get("ort") or address.get("name") or "").strip()
            region = str(address.get("region") or "").strip()
            country = str(address.get("land") or "").strip()
            locality = " ".join(part for part in (postcode, city) if part)
            parts = [locality] if locality else []
            if region and region.casefold() != city.casefold():
                parts.append(region)
            if country and country.casefold() not in {"deutschland", "germany"}:
                parts.append(country)
            rendered = ", ".join(parts)
        else:
            rendered = ""
        if rendered and rendered not in result:
            result.append(rendered)
    return tuple(result)


def _employment_types(
    detail: Mapping[str, Any], summary: Mapping[str, Any]
) -> tuple[str, ...]:
    values = string_tuple(
        detail.get("arbeitszeitmodelle") or summary.get("arbeitszeitmodelle")
    )
    if values:
        return values

    result: list[str] = []
    boolean_models = (
        ("arbeitszeitVollzeit", "VOLLZEIT"),
        ("arbeitszeitTeilzeitFlexibel", "TEILZEIT"),
        ("arbeitszeitTeilzeitVormittag", "TEILZEIT"),
        ("arbeitszeitTeilzeitNachmittag", "TEILZEIT"),
        ("arbeitszeitTeilzeitAbend", "TEILZEIT"),
        ("arbeitszeitSchichtNachtWochenende", "SCHICHT_NACHTARBEIT_WOCHENENDE"),
        ("istGeringfuegigeBeschaeftigung", "MINIJOB"),
    )
    for key, model in boolean_models:
        value = detail.get(key)
        if value is None:
            value = summary.get(key)
        if _as_bool(value) is True and model not in result:
            result.append(model)
    return tuple(result)


def _remote_value(
    employment_types: tuple[str, ...],
    detail: Mapping[str, Any],
    summary: Mapping[str, Any],
) -> bool | None:
    joined = " ".join(employment_types).casefold()
    if any(token in joined for token in ("heim", "tele", "remote")):
        return True
    for key in ("homeofficemoeglich", "remote"):
        value = detail.get(key)
        if value is None:
            value = summary.get(key)
        parsed = _as_bool(value)
        if parsed is not None:
            return parsed
    return None


def _ba_remote_type(remote: bool | None, homeoffice_type: str | None) -> str | None:
    if remote is not True:
        return None
    normalized = (homeoffice_type or "").casefold()
    if normalized in {"vollstaendig", "vollständig", "ausschliesslich", "ausschließlich"}:
        return "remote"
    return "hybrid"


def _primary_location_metadata(value: Any) -> dict[str, str]:
    values = value if isinstance(value, list) else [value]
    for item in values:
        if not isinstance(item, Mapping):
            continue
        nested_address = item.get("adresse")
        address = nested_address if isinstance(nested_address, Mapping) else item
        metadata = {
            "postcode": str(address.get("plz") or "").strip(),
            "city": str(address.get("ort") or address.get("name") or "").strip(),
            "state": _normalized_region(address.get("region")),
            "country": str(address.get("land") or "").strip(),
        }
        return {key: item for key, item in metadata.items() if item}
    return {}


def _normalized_region(value: Any) -> str:
    text = str(value or "").strip()
    if text.casefold() == "baden_wuerttemberg":
        return "Baden-Württemberg"
    return text


def _ba_optional_text(
    detail: Mapping[str, Any],
    summary: Mapping[str, Any],
    *keys: str,
) -> str | None:
    value = _first_text(detail, *keys) or _first_text(summary, *keys)
    if value is None:
        return None
    if value.casefold() in {"keine_angabe", "keine_angaben", "nicht_bekannt"}:
        return None
    return value


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.casefold().strip()
        if normalized == "true":
            return True
        if normalized == "false":
            return False
    return None


def _first_text(data: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = data.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
