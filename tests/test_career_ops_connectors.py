from __future__ import annotations

from jobradar.connectors.beesite import _normalize as normalize_beesite
from jobradar.connectors.deutsche_bahn import _normalize_job as normalize_deutsche_bahn
from jobradar.connectors.join import _normalize_join
from jobradar.connectors.oracle import _normalize_oracle
from jobradar.connectors.phenom import _normalize_phenom
from jobradar.connectors.radancy import _normalize as normalize_radancy
from jobradar.connectors.recruitee import _normalize_recruitee
from jobradar.connectors.successfactors import _normalize_successfactors
from jobradar.connectors.tkms import _normalize as normalize_tkms
from jobradar.connectors.workable import _normalize_workable
from jobradar.connectors.workday import _normalize_workday


def test_deutsche_bahn_and_join_normalize_public_records() -> None:
    db_record = normalize_deutsche_bahn(
        {
            "id": "db-1",
            "title": "Vorstandsassistenz",
            "href": "/job/db-1",
            "company": "Deutsche Bahn AG",
            "locations": ["Stuttgart"],
        }
    )
    join_record = _normalize_join(
        "join:acme",
        "acme",
        "Acme GmbH",
        {
            "id": "join-1",
            "idParam": "join-1-vorstandsassistenz",
            "title": "Vorstandsassistenz",
            "city": {"cityName": "Stuttgart", "countryName": "Deutschland"},
            "workplaceType": "remote",
        },
    )

    assert db_record is not None
    assert db_record.canonical_url == "https://db.jobs/job/db-1"
    assert join_record is not None
    assert join_record.company == "Acme GmbH"
    assert join_record.remote is True


def test_enterprise_apis_normalize_public_records() -> None:
    successfactors = _normalize_successfactors(
        "successfactors:acme",
        "https://jobs.acme.example",
        "Acme GmbH",
        {
            "jobId": "sf-1",
            "jobTitle": "Executive Assistant",
            "jobUrl": "/job/sf-1",
            "location": "Stuttgart",
        },
    )
    workday = _normalize_workday(
        "workday:acme:External",
        "https://acme.wd5.myworkdayjobs.com/de-DE/External",
        "External",
        "Acme GmbH",
        {
            "title": "Executive Assistant",
            "externalPath": "/job/Stuttgart/Executive-Assistant_WD-1",
            "locationsText": "Stuttgart",
            "bulletFields": ["Vollzeit", "WD-1"],
        },
    )
    workable = _normalize_workable(
        "workable:acme",
        "acme",
        "Acme GmbH",
        {
            "shortcode": "WB-1",
            "title": "Executive Assistant",
            "location": {"city": "Stuttgart", "country": "Germany"},
            "remote": False,
        },
    )

    assert successfactors is not None
    assert successfactors.source_job_id == "sf-1"
    assert workday is not None
    assert workday.source_job_id.endswith("Executive-Assistant_WD-1")
    assert workable is not None
    assert workable.canonical_url == "https://apply.workable.com/acme/j/WB-1/"


def test_feed_and_widget_sources_preserve_provider_payload() -> None:
    recruitee = _normalize_recruitee(
        "recruitee:acme",
        "Acme GmbH",
        "https://acme.recruitee.com",
        {
            "id": 42,
            "slug": "executive-assistant",
            "title": "Executive Assistant",
            "location": "Stuttgart",
            "careers_url": "https://acme.recruitee.com/o/executive-assistant",
        },
    )
    oracle = _normalize_oracle(
        "https://acme.fa.eu2.oraclecloud.com",
        "Acme GmbH",
        {
            "Id": "ORC-1",
            "Title": "Executive Assistant",
            "PrimaryLocation": "Stuttgart",
            "JobDetailUrl": "https://acme.fa.eu2.oraclecloud.com/job/ORC-1",
        },
    )
    phenom = _normalize_phenom(
        "https://careers.acme.example/jobs",
        "Acme GmbH",
        {
            "jobId": "PH-1",
            "title": "Executive Assistant",
            "location": "Stuttgart",
            "jobUrl": "/job/PH-1",
        },
    )

    assert recruitee is not None and recruitee.raw["id"] == 42
    assert oracle is not None and oracle.raw["Id"] == "ORC-1"
    assert phenom is not None and phenom.raw["jobId"] == "PH-1"


def test_company_site_sources_normalize_records() -> None:
    radancy = normalize_radancy(
        "radancy:acme",
        "https://careers.acme.example",
        "Acme GmbH",
        {"id": "RA-1", "title": "Executive Assistant", "url": "/job/RA-1"},
    )
    beesite = normalize_beesite(
        "beesite:acme",
        "https://jobs.acme.example",
        "Acme GmbH",
        {"id": "BEE-1", "title": "Executive Assistant", "url": "/job/BEE-1"},
    )
    tkms = normalize_tkms(
        "de",
        {
            "id": "TK-1",
            "title": "Assistenz der Geschäftsführung",
            "city": "Kiel",
            "company": "TKMS GmbH",
        },
        {"data": "provider-envelope"},
    )

    assert radancy is not None and radancy.canonical_url.endswith("/job/RA-1")
    assert beesite is not None and beesite.canonical_url.endswith("/job/BEE-1")
    assert tkms is not None and tkms.raw["data"]["id"] == "TK-1"
