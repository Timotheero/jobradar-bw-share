"""Deterministic local demonstration data; never contacts external services."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import CandidateProfile, PreferenceProfile, Source
from .services.jobs import score_job, upsert_job

DEMO_JOBS = (
    {
        "external_id": "demo-1",
        "title": "Assistenz der Geschaeftsfuehrung (m/w/d)",
        "employer": "Beispiel Maschinenbau GmbH",
        "description_text": (
            "Sie unterstuetzen die Geschaeftsfuehrung im Tagesgeschaeft, koordinieren Termine, "
            "bereiten Entscheidungen und Praesentationen vor und steuern eigene Projekte."
        ),
        "location_text": "Stuttgart",
        "city": "Stuttgart",
        "state": "Baden-Wuerttemberg",
        "remote_type": "onsite",
        "employment_type": "Vollzeit",
        "contract_type": "unbefristete Direktanstellung",
        "canonical_url": "https://example.invalid/demo-1",
        "structured_data": {"demo": True},
    },
    {
        "external_id": "demo-2",
        "title": "Executive Assistant to CEO",
        "employer": "Demo Technologie AG",
        "description_text": (
            "Direct support to the CEO, board meeting preparation, stakeholder coordination "
            "and ownership of cross-functional strategic initiatives."
        ),
        "location_text": "Karlsruhe",
        "city": "Karlsruhe",
        "state": "Baden-Wuerttemberg",
        "remote_type": "hybrid",
        "employment_type": "Vollzeit",
        "contract_type": "unbefristete Direktanstellung",
        "canonical_url": "https://example.invalid/demo-2",
        "language": "en",
        "structured_data": {"demo": True},
    },
    {
        "external_id": "demo-3",
        "title": "Chief of Staff – CEO Office",
        "employer": "Remote Beispiel SE",
        "description_text": (
            "Direkte Zusammenarbeit mit der Geschaeftsfuehrung, Aufbereitung von "
            "Entscheidungsvorlagen und Koordination strategischer Initiativen."
        ),
        "location_text": "Deutschland",
        "state": None,
        "remote_type": "remote",
        "employment_type": "Vollzeit",
        "contract_type": "unbefristete Direktanstellung",
        "canonical_url": "https://example.invalid/demo-3",
        "structured_data": {"demo": True, "strategic_adjacent": True},
    },
)


def seed_demo(session: Session) -> int:
    source = session.scalar(select(Source).where(Source.slug == "demo"))
    if source is None:
        source = Source(
            name="Lokale Beispieldaten",
            slug="demo",
            kind="demo",
            enabled=False,
            is_official=False,
            free_api=True,
            status="ready",
            metadata_json={"network": False},
        )
        session.add(source)
        session.flush()

    candidate = session.scalar(select(CandidateProfile).order_by(CandidateProfile.id).limit(1))
    preferences = session.scalar(select(PreferenceProfile).order_by(PreferenceProfile.id).limit(1))
    changed = 0
    for item in DEMO_JOBS:
        external_id = str(item["external_id"])
        values = {key: value for key, value in item.items() if key != "external_id"}
        job, created, updated = upsert_job(
            session,
            source_id=source.id,
            external_id=external_id,
            values=values,
            commit=False,
        )
        if created or updated:
            score_job(
                session,
                job,
                candidate=candidate,
                preferences=preferences,
                commit=False,
            )
            changed += 1
    session.commit()
    return changed
