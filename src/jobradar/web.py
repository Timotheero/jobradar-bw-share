"""Server-rendered, dependency-light web interface for GF-Jobradar.

The module intentionally contains presentation queries only. Crawling, scoring and
OpenAI connections live behind separate services and are never started from here.
"""

from __future__ import annotations

import gzip
import json
import logging
import re
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from sqlalchemy import func, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, selectinload
from starlette.datastructures import UploadFile

from .config import get_settings
from .connectors.public_url import UnsafePublicURLError, validate_public_url
from .db import get_db
from .domain.role_terms import (
    DEFAULT_ADDITIONAL_ROLE_TERMS,
    DEFAULT_PRIMARY_ROLE_TERMS,
    normalized_role_terms,
)
from .i18n import (
    SUPPORTED_LOCALES,
    locale_from_request,
    translate,
    translate_text,
)
from .integrations.codex.client import CodexProtocolError, CodexUnavailableError
from .models import (
    ApplicationDraft,
    ApplicationRecord,
    ApplicationStatus,
    CandidateProfile,
    CrawlRun,
    JobPosting,
    JobScore,
    JobSummary,
    PreferenceProfile,
    Source,
)
from .security import safe_next_path
from .services import application_drafts as draft_service
from .services import jobs as job_service
from .services import profile as profile_service
from .services import settings as setting_service
from .services.availability import (
    HTTPJobAvailabilityChecker,
    JobAvailabilityChecker,
    verify_jobs_for_application,
)
from .services.crawl_reporting import (
    CrawlerRuntimeStatus,
    TargetCrawlReport,
    firecrawl_runtime_status,
    firecrawl_target_reports,
)
from .services.cv_import import MAX_FILE_BYTES, CVImportError, import_cv
from .services.job_summary import (
    JobSummaryNotFoundError,
    current_summary,
    get_or_create_summary,
)
from .services.preference_prompt import (
    PREFERENCE_HISTORY_KEY,
    PreferenceChangePlan,
    compile_preference_prompt,
    coverage_changed,
    preference_snapshot,
)
from .services.rescore import process_pending_rescore
from .services.sync import (
    clean_description,
    query_plan_for_preferences,
    run_all_enabled,
    source_environment_enabled,
)

logger = logging.getLogger(__name__)

PACKAGE_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = PACKAGE_DIR / "templates"
STATIC_DIR = PACKAGE_DIR / "static"

templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
router = APIRouter(include_in_schema=False)


JOBS_BATCH_SIZE = 100


PIPELINE_STATUSES = (
    "Neu",
    "Merkliste",
    "Nicht passend",
    "Bewerbung geplant",
    "Beworben",
    "Gespräch",
    "Angebot",
    "Abgeschlossen",
)



def _render(
    request: Request, template: str, *, status_code: int = 200, **context: Any
) -> HTMLResponse:
    locale = locale_from_request(request)
    next_path = request.url.path + (f"?{request.url.query}" if request.url.query else "")

    def language_url(target_locale: str) -> str:
        return str(
            request.url_for("set_language", locale=target_locale).include_query_params(
                next=safe_next_path(next_path)
            )
        )

    context.setdefault("active_page", "")
    context["request"] = request
    context["locale"] = locale
    context["t"] = lambda message, **values: translate(message, locale, **values)
    context["language_url"] = language_url
    return templates.TemplateResponse(
        request=request, name=template, context=context, status_code=status_code
    )


def _safe_all(session: Session, statement: Any) -> list[Any]:
    """Return query results while allowing a fresh, not-yet-migrated install to render."""

    try:
        return list(session.scalars(statement).all())
    except SQLAlchemyError:
        session.rollback()
        return []


def _safe_one(session: Session, statement: Any) -> Any | None:
    try:
        return session.scalars(statement).first()
    except SQLAlchemyError:
        session.rollback()
        return None


def _score_percent(value: float | int | None) -> int:
    if value is None:
        return 0
    numeric = float(value)
    if 0 <= numeric <= 1:
        numeric *= 100
    return max(0, min(100, round(numeric)))


def _latest_score(job: JobPosting) -> JobScore | None:
    scores = list(getattr(job, "scores", None) or [])
    return max(scores, key=lambda item: _datetime_key(item.created_at), default=None)


def _datetime_key(value: datetime | None) -> float:
    if value is None:
        return float("-inf")
    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return aware.timestamp()


def _is_recent(value: datetime | None, *, days: int = 7) -> bool:
    if value is None:
        return False
    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return (datetime.now(UTC) - aware.astimezone(UTC)).days < days


def _initials(value: str | None, fallback: str = "AG") -> str:
    words = [word for word in (value or "").replace("-", " ").split() if word]
    return "".join(word[0].upper() for word in words[:2]) or fallback


def _normalise_workplace(value: str | None, locale: str) -> tuple[str, str]:
    normalised = (value or "").strip().lower().replace("_", "-")
    if normalised in {"remote", "fully-remote", "full-remote", "homeoffice"}:
        return "remote", translate("Remote", locale)
    if normalised in {"hybrid", "teilweise-remote", "partial-remote", "mixed"}:
        return "hybrid", translate("Hybrid", locale)
    if normalised in {"onsite", "on-site", "praesenz", "präsenz"}:
        return "onsite", translate("Präsenz", locale)
    return "unknown", translate("Nicht angegeben", locale)


def _normalise_employment(value: str | None, locale: str) -> str:
    normalised = (value or "").strip()
    lowered = normalised.casefold()
    if "vollzeit" in lowered or "full-time" in lowered:
        return translate("Vollzeit", locale)
    if "teilzeit" in lowered or "part-time" in lowered:
        return translate("Teilzeit", locale)
    return translate_text(normalised, locale) or translate("Nicht angegeben", locale)


def _normalise_contract(value: str | None, locale: str) -> str:
    normalised = (value or "").strip()
    lowered = normalised.casefold()
    if "unbefristet" in lowered or "permanent" in lowered:
        return translate("Unbefristet", locale)
    if "befristet" in lowered or "fixed-term" in lowered:
        return translate("Befristet", locale)
    return translate_text(normalised, locale) or translate("Vertrag nicht angegeben", locale)


def _format_date(value: datetime | None, locale: str, *, with_time: bool = False) -> str:
    if value is None:
        return translate("Noch nie", locale)
    local = value
    if value.tzinfo is not None:
        local = value.astimezone()
    if locale == "en":
        return local.strftime("%Y-%m-%d · %H:%M" if with_time else "%Y-%m-%d")
    return local.strftime("%d.%m.%Y · %H:%M Uhr" if with_time else "%d.%m.%Y")


def _published_label(value: datetime | None, locale: str) -> str:
    if value is None:
        return translate("Datum unbekannt", locale)
    now = datetime.now(value.tzinfo or UTC)
    days = max(0, (now.date() - value.date()).days)
    if days == 0:
        return translate("Heute veröffentlicht", locale)
    if days == 1:
        return translate("Gestern veröffentlicht", locale)
    if days < 14:
        return translate("Vor {days} Tagen veröffentlicht", locale, days=days)
    return translate("Veröffentlicht am {date}", locale, date=_format_date(value, locale))


def _reason_text(item: Any) -> str:
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        for key in ("reason", "explanation", "label", "text", "title"):
            if item.get(key):
                return str(item[key])
    return ""


def _reason_list(score: JobScore | None, locale: str) -> list[str]:
    if score is None:
        return []
    values = list(score.relevance_reasons or []) + list(score.fit_reasons or [])
    return [
        translate_text(text, locale)
        for text in (_reason_text(item) for item in values)
        if text
    ]


def _original_description(job: JobPosting) -> str:
    snapshots = list(getattr(job, "snapshots", None) or [])
    latest = max(snapshots, key=lambda item: _datetime_key(item.fetched_at), default=None)
    if latest is not None and latest.raw_html_compressed:
        try:
            envelope = json.loads(gzip.decompress(latest.raw_html_compressed))
            value = envelope.get("description") if isinstance(envelope, dict) else None
            description_format = (
                envelope.get("description_format") if isinstance(envelope, dict) else None
            )
            if isinstance(value, str) and value.strip():
                return clean_description(value, description_format)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError):
            logger.warning("Could not decode original description snapshot for job %s", job.id)
    return job.description_text


def _paragraph_chunks(text: str, maximum: int = 520) -> list[str]:
    sentences = re.split(r"(?<=[.!?])\s+(?=[A-ZÄÖÜ0-9])", text)
    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        sentence = sentence.strip()
        while len(sentence) > maximum:
            split_at = sentence.rfind(" ", 0, maximum + 1)
            split_at = split_at if split_at > 0 else maximum
            prefix, sentence = sentence[:split_at].strip(), sentence[split_at:].strip()
            if current:
                chunks.append(current)
                current = ""
            if prefix:
                chunks.append(prefix)
        if not sentence:
            continue
        candidate = f"{current} {sentence}".strip()
        if current and len(candidate) > maximum:
            chunks.append(current)
            current = sentence
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def _description_markup(text: str | None, locale: str) -> Markup:
    prepared = re.sub(r"\s+(?=[•▪◦]\s*)", "\n", (text or "").replace("\r", ""))
    lines = prepared.split("\n")
    if not any(line.strip() for line in lines):
        message = escape(
            translate("Die vollständige Beschreibung ist noch nicht verfügbar.", locale)
        )
        return Markup(f"<p>{message}</p>")

    blocks: list[str] = []
    paragraph: list[str] = []
    bullets: list[str] = []

    def flush_paragraph() -> None:
        if paragraph:
            blocks.extend(
                f"<p>{escape(chunk)}</p>" for chunk in _paragraph_chunks(" ".join(paragraph))
            )
            paragraph.clear()

    def flush_bullets() -> None:
        if bullets:
            items = "".join(f"<li>{escape(item)}</li>" for item in bullets)
            blocks.append(f"<ul>{items}</ul>")
            bullets.clear()

    for raw_line in lines:
        line = re.sub(r"\s+", " ", raw_line).strip()
        if not line:
            flush_paragraph()
            flush_bullets()
            continue
        bullet = re.match(r"^(?:[-*•·▪◦]|\d+[.)])\s+(.+)$", line)
        if bullet:
            flush_paragraph()
            bullets.append(bullet.group(1))
            continue
        flush_bullets()
        heading = re.match(r"^#{1,6}\s+(.+)$", line)
        if heading or (len(line) <= 90 and line.endswith(":")):
            flush_paragraph()
            heading_text = heading.group(1) if heading else line[:-1]
            blocks.append(f"<h3>{escape(heading_text.strip())}</h3>")
            continue
        paragraph.append(line)

    flush_paragraph()
    flush_bullets()
    return Markup("".join(blocks))


def _description_excerpt(text: str | None, maximum: int = 280) -> str:
    compact = re.sub(r"\s+", " ", text or "").strip()
    if len(compact) <= maximum:
        return compact
    shortened = compact[: maximum - 1].rsplit(" ", 1)[0].rstrip(" ,.;:-")
    return f"{shortened}…"


def _safe_external_url(value: str | None) -> str | None:
    """Expose only credential-free absolute HTTP(S) links from source data."""

    if not value or any(ord(character) < 32 for character in value):
        return None
    parsed = urlsplit(value.strip())
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    return value.strip()


def _automatic_summary_view(summary: JobSummary | None, locale: str) -> dict[str, Any]:
    if summary is None:
        return {
            "state": "pending",
            "overview": translate(
                "Die kurze Zusammenfassung wird automatisch erstellt.", locale
            ),
            "key_points": [],
            "missing_information": [],
            "model": "Luna",
            "reasoning_effort": translate("Mittel", locale),
        }
    return {
        "state": "ready",
        "overview": summary.overview,
        "key_points": list(summary.key_points or []),
        "missing_information": list(summary.missing_information or []),
        "model": "Luna" if summary.model.endswith("-luna") else summary.model.title(),
        "reasoning_effort": translate("Mittel", locale)
        if summary.reasoning_effort == "medium"
        else summary.reasoning_effort,
    }


def _job_view(job: JobPosting, locale: str) -> dict[str, Any]:
    score = _latest_score(job)
    workplace_class, workplace = _normalise_workplace(job.remote_type, locale)
    reasons = _reason_list(score, locale)
    structured = dict(job.structured_data or {})
    application = getattr(job, "application", None)
    source = getattr(job, "source", None)
    role_score = _score_percent(score.relevance_score if score else None)
    fit_score = _score_percent(score.candidate_fit_score if score else None)
    reason = (
        reasons[0]
        if reasons
        else translate("Die ausführliche Begründung folgt nach der OpenAI-Verbindung.", locale)
    )
    searchable = f"{job.title} {job.description_text}".casefold()
    strategic_title = any(
        term in job.title.casefold()
        for term in ("chief of staff", "geschäftsführungsrefer", "ceo office", "vorstandsrefer")
    )
    direct_management = any(
        term in searchable
        for term in ("geschäftsführung", "geschaeftsfuehrung", " ceo ", "vorstand")
    )
    is_strategic = bool(
        structured.get("strategic_adjacent")
        or structured.get("classification") == "strategisch_erweitert"
        or (strategic_title and direct_management)
    )
    if locale == "en":
        summary = (
            structured.get("english_summary")
            or structured.get("summary")
            or structured.get("german_summary")
            or reason
        )
    else:
        summary = structured.get("german_summary") or structured.get("summary") or reason
    return {
        "id": job.id,
        "title": job.title,
        "company": job.employer or translate("Arbeitgeber nicht angegeben", locale),
        "company_initials": _initials(job.employer),
        "location": job.location_text
        or job.city
        or translate("Arbeitsort nicht angegeben", locale),
        "workplace": workplace,
        "workplace_class": workplace_class,
        "employment": _normalise_employment(job.employment_type, locale),
        "contract": _normalise_contract(job.contract_type, locale),
        "role_score": role_score,
        "fit_score": fit_score,
        "reason": reason,
        "reasons": reasons,
        "concerns": [
            _reason_text(item) for item in structured.get("concerns", []) if _reason_text(item)
        ],
        "summary": translate_text(str(summary), locale),
        "description_excerpt": _description_excerpt(job.description_text),
        "description_html": _description_markup(job.description_text, locale),
        "salary": str(structured.get("salary") or "").strip()
        or translate("Nicht angegeben", locale),
        "published_label": _published_label(job.published_at, locale),
        "last_seen_label": _format_date(job.last_seen_at, locale, with_time=True),
        "availability_status": job.availability_status,
        "availability_checked_label": (
            _format_date(job.availability_checked_at, locale, with_time=True)
            if job.availability_checked_at
            else ""
        ),
        "source_label": translate_text(source.name, locale)
        if source
        else translate("Quelle unbekannt", locale),
        "is_new": _is_recent(job.first_seen_at),
        "is_saved": bool(application and application.status == "Merkliste"),
        "application_status": application.status if application else "Neu",
        "application_notes": application.notes if application else "",
        "is_strategic": is_strategic,
        "original_url": _safe_external_url(job.canonical_url),
        "commute_label": translate_text(structured.get("commute_label"), locale)
        or translate("Noch nicht berechnet", locale),
        "sources": (
            [
                {
                    "name": translate_text(source.name, locale),
                    "initial": _initials(source.name, "Q"),
                    "label": translate(
                        "Kostenlose API" if source.free_api else "Gefundene Quelle", locale
                    ),
                }
            ]
            if source
            else []
        ),
    }


def _workplace_preference(preference: PreferenceProfile | None) -> str:
    if preference is None:
        return "onsite"
    extra = dict(preference.extra_preferences or {})
    selected = extra.get("workplace_preference")
    if selected in {"onsite", "hybrid", "remote"}:
        return str(selected)
    return max(
        (
            ("onsite", preference.onsite_weight),
            ("hybrid", preference.hybrid_weight),
            ("remote", preference.remote_weight),
        ),
        key=lambda pair: pair[1],
    )[0]


def _preference_prompt_value(value: Any, locale: str) -> str:
    if value is None or value == "" or value == [] or value == {}:
        return translate("Nicht angegeben", locale)
    if isinstance(value, bool):
        return translate("Ja" if value else "Nein", locale)
    if isinstance(value, dict):
        return "; ".join(
            f"{key}: {_preference_prompt_value(item, locale)}" for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item) for item in value) or translate("Nicht angegeben", locale)
    return str(value)


def _format_role_taxonomy(
    primary_terms: list[str],
    additional_terms: list[str],
    locale: str,
) -> str:
    lines = [f"{translate('Hauptbegriffe', locale)}:"]
    lines.extend(f"- {term}" for term in primary_terms)
    lines.extend(("", f"{translate('Zusätzliche Titel', locale)}:"))
    lines.extend(f"- {term}" for term in additional_terms)
    return "\n".join(lines)


def _role_taxonomy_prompt(
    preference: PreferenceProfile | None,
    locale: str,
) -> str:
    primary_terms = (
        preference.primary_role_terms
        if preference
        else list(DEFAULT_PRIMARY_ROLE_TERMS)
    )
    additional_terms = (
        preference.additional_role_terms
        if preference
        else list(DEFAULT_ADDITIONAL_ROLE_TERMS)
    )
    return _format_role_taxonomy(primary_terms, additional_terms, locale)


def _default_preference_prompt(
    preference: PreferenceProfile | None, locale: str
) -> str:
    employment_types = preference.employment_types if preference else ["Vollzeit"]
    contract_types = (
        preference.contract_types if preference else ["unbefristet", "Direktanstellung"]
    )
    preferred_states = preference.preferred_states if preference else ["Baden-Württemberg"]
    include_remote = preference.include_germany_remote if preference else True
    commute_soft = preference.commute_penalty_minutes if preference else 60
    commute_max = preference.max_commute_minutes if preference else 75
    onsite_weight = preference.onsite_weight if preference else 1.0
    hybrid_weight = preference.hybrid_weight if preference else 0.55
    remote_weight = preference.remote_weight if preference else 0.2
    minimum_salary = preference.min_salary if preference else None
    target_salary = preference.target_salary if preference else None
    languages = preference.languages if preference else ["de", "en"]
    extra_preferences = dict(preference.extra_preferences or {}) if preference else {}
    industries = extra_preferences.get("industries", [])
    company_sizes = extra_preferences.get("company_sizes", [])
    max_travel_percent = extra_preferences.get("max_travel_percent")
    workplace = _workplace_preference(preference)
    workplace_label = {
        "onsite": translate("Präsenz bevorzugt", locale),
        "hybrid": translate("Hybrid", locale),
        "remote": translate("Remote", locale),
    }[workplace]
    lines = [
        translate(
            "Arbeitszeit: {value}",
            locale,
            value=_preference_prompt_value(employment_types, locale),
        ),
        translate(
            "Vertragsarten: {value}",
            locale,
            value=_preference_prompt_value(contract_types, locale),
        ),
        translate(
            "Bevorzugte Regionen: {value}",
            locale,
            value=_preference_prompt_value(preferred_states, locale),
        ),
        translate(
            "Deutschlandweite Remote-Stellen einbeziehen: {value}",
            locale,
            value=_preference_prompt_value(include_remote, locale),
        ),
        translate("Bevorzugtes Arbeitsmodell: {value}", locale, value=workplace_label),
        translate(
            (
                "Gewichtung der Arbeitsmodelle: Präsenz {onsite} %, "
                "Hybrid {hybrid} %, Remote {remote} %"
            ),
            locale,
            onsite=round(onsite_weight * 100),
            hybrid=round(hybrid_weight * 100),
            remote=round(remote_weight * 100),
        ),
        translate(
            "Fahrtzeit: volle Wertung bis {soft} Minuten; maximal {maximum} Minuten einfach.",
            locale,
            soft=commute_soft,
            maximum=commute_max,
        ),
        translate(
            "Mindestgehalt pro Jahr: {value}",
            locale,
            value=_preference_prompt_value(minimum_salary, locale),
        ),
        translate(
            "Zielgehalt pro Jahr: {value}",
            locale,
            value=_preference_prompt_value(target_salary, locale),
        ),
        translate(
            "Sprachen: {value}",
            locale,
            value=_preference_prompt_value(languages, locale),
        ),
        translate(
            "Bevorzugte Branchen: {value}",
            locale,
            value=_preference_prompt_value(industries, locale),
        ),
        translate(
            "Bevorzugte Unternehmensgrößen: {value}",
            locale,
            value=_preference_prompt_value(company_sizes, locale),
        ),
        translate(
            "Maximale Reisetätigkeit: {value}",
            locale,
            value=(
                f"{max_travel_percent} %"
                if max_travel_percent is not None
                else translate("Nicht angegeben", locale)
            ),
        ),
    ]
    extra = dict(preference.extra_preferences or {}) if preference else {}
    for key, value in extra.items():
        if key in {
            "workplace_preference",
            "preference_prompt",
            "preference_prompt_source",
            "industries",
            "company_sizes",
            "max_travel_percent",
        }:
            continue
        lines.append(
            translate(
                "Weitere gespeicherte Präferenz ({key}): {value}",
                locale,
                key=key,
                value=_preference_prompt_value(value, locale),
            )
        )
    return "\n".join(lines)


_PREFERENCE_FIELD_LABELS = {
    "employment_types": "Arbeitszeit",
    "contract_types": "Vertragsarten",
    "preferred_states": "Bevorzugte Regionen",
    "include_germany_remote": "Deutschlandweite Remote-Stellen",
    "workplace_preference": "Bevorzugtes Arbeitsmodell",
    "onsite_weight": "Gewichtung Präsenz",
    "hybrid_weight": "Gewichtung Hybrid",
    "remote_weight": "Gewichtung Remote",
    "commute_penalty_minutes": "Abwertung der Fahrtzeit ab",
    "max_commute_minutes": "Maximale einfache Fahrtzeit",
    "min_salary": "Mindestgehalt pro Jahr",
    "target_salary": "Zielgehalt pro Jahr",
    "languages": "Sprachen",
    "primary_role_terms": "Hauptbegriffe",
    "additional_role_terms": "Zusätzliche Titel",
    "industries": "Bevorzugte Branchen",
    "company_sizes": "Bevorzugte Unternehmensgrößen",
    "max_travel_percent": "Maximale Reisetätigkeit",
}

_PROMPT_ISSUE_MESSAGES = {
    "unknown_instruction": "Diese Zeile wurde nicht verstanden und wird nicht angewendet.",
    "invalid_state": "Mindestens eine Region ist kein unterstütztes deutsches Bundesland.",
    "invalid_boolean": "Bitte Ja oder Nein angeben.",
    "invalid_workplace": "Bitte Präsenz, Hybrid oder Remote angeben.",
    "invalid_weights": "Bitte genau drei Gewichtungen zwischen 0 und 100 Prozent angeben.",
    "invalid_commute": "Die Fahrtzeit braucht zwei gültige, aufsteigende Minutenwerte.",
    "invalid_salary": "Das Gehalt ist ungültig.",
    "invalid_percentage": "Der Prozentwert muss zwischen 0 und 100 liegen.",
    "target_below_minimum": "Das Zielgehalt darf nicht unter dem Mindestgehalt liegen.",
    "commute_order": "Die maximale Fahrtzeit darf nicht unter der Abwertungsgrenze liegen.",
    "list_too_large": "Eine Präferenzliste ist zu lang.",
    "unknown_role_section": "Diese Überschrift für Rollenbegriffe wurde nicht erkannt.",
    "invalid_role_term": "Rollenbegriffe müssen als Aufzählung mit einem Bindestrich beginnen.",
    "missing_primary_role_terms": "Mindestens ein Hauptbegriff ist erforderlich.",
    "role_list_too_large": "Es sind höchstens 10 Hauptbegriffe und 50 zusätzliche Titel erlaubt.",
}


def _preference_change_value(field: str, value: Any, locale: str) -> str:
    if field == "workplace_preference":
        return {
            "onsite": translate("Präsenz bevorzugt", locale),
            "hybrid": translate("Hybrid", locale),
            "remote": translate("Remote", locale),
        }.get(str(value), str(value))
    if field in {"onsite_weight", "hybrid_weight", "remote_weight"}:
        return f"{round(float(value) * 100)} %"
    if field in {"commute_penalty_minutes", "max_commute_minutes"}:
        return translate("{minutes} Minuten", locale, minutes=value)
    if field == "max_travel_percent" and value is not None:
        return f"{value} %"
    return _preference_prompt_value(value, locale)


def _preference_plan_view(
    plan: PreferenceChangePlan, locale: str, *, crawling_enabled: bool
) -> dict[str, Any]:
    issues = [
        {
            "line_number": issue.line_number,
            "line": issue.line,
            "source": translate(
                "Rollenbegriffe" if issue.source == "role_taxonomy" else "Stellenwünsche",
                locale,
            ),
            "source_key": issue.source,
            "message": translate(_PROMPT_ISSUE_MESSAGES[issue.code], locale),
        }
        for issue in plan.issues
    ]
    return {
        "prompt": plan.prompt,
        "valid": plan.valid,
        "coverage_changed": plan.coverage_changed,
        "crawling_enabled": crawling_enabled,
        "changes": [
            {
                "field": change.field,
                "label": translate(_PREFERENCE_FIELD_LABELS[change.field], locale),
                "before": _preference_change_value(change.field, change.before, locale),
                "after": _preference_change_value(change.field, change.after, locale),
                "requires_crawl": change.requires_crawl,
            }
            for change in plan.changes
        ],
        "issues": issues,
        "role_issues": [issue for issue in issues if issue["source_key"] == "role_taxonomy"],
        "prompt_issues": [
            issue for issue in issues if issue["source_key"] == "preference_prompt"
        ],
    }


def _profile_views(session: Session, locale: str) -> tuple[dict[str, Any], dict[str, Any]]:
    candidate = _safe_one(
        session, select(CandidateProfile).order_by(CandidateProfile.updated_at.desc())
    )
    preference = _safe_one(
        session, select(PreferenceProfile).order_by(PreferenceProfile.updated_at.desc())
    )
    structured = dict(candidate.structured_data or {}) if candidate else {}
    full_name = (candidate.full_name or "") if candidate else ""
    cv_import = dict(structured.get("cv_import") or {})
    field_labels = {
        "full_name": translate("Name", locale),
        "email": translate("E-Mail", locale),
        "phone": translate("Telefon", locale),
        "current_title": translate("Aktuelle Tätigkeit", locale),
        "experience_years": translate("Berufserfahrung", locale),
        "summary": translate("Profilzusammenfassung", locale),
        "languages": translate("Sprachen", locale),
        "skills": translate("Wichtige Fähigkeiten", locale),
        "work_experience": translate("Berufserfahrung", locale),
        "education": translate("Ausbildung", locale),
        "certifications": translate("Zertifikate", locale),
    }
    grouped_evidence: dict[tuple[str, str], list[str]] = {}
    for item in cv_import.get("evidence", []):
        if not isinstance(item, dict):
            continue
        field = str(item.get("field") or "").strip()
        source_text = str(item.get("source_text") or "").strip()
        value = str(item.get("value") or "").strip()
        if not field or not source_text or not value:
            continue
        target = grouped_evidence.setdefault((field, source_text), [])
        if value.casefold() not in {existing.casefold() for existing in target}:
            target.append(value)
    evidence = []
    for (field, source_text), values in grouped_evidence.items():
        rendered_value = ", ".join(values)
        normalized_value = re.sub(r"[\W_]+", "", rendered_value.casefold())
        normalized_source = re.sub(r"[\W_]+", "", source_text.casefold())
        source_is_redundant = normalized_source == normalized_value or (
            field in {"full_name", "email", "phone", "current_title"}
            and normalized_value in normalized_source
        )
        evidence.append(
            {
                "field": field_labels.get(field, field),
                "value": rendered_value,
                "source_text": "" if source_is_redundant else source_text,
            }
        )
    filled = [
        full_name,
        structured.get("current_title"),
        structured.get("experience_years"),
        structured.get("skills"),
        candidate.cv_filename if candidate else None,
    ]
    completion = round(sum(bool(value) for value in filled) / len(filled) * 100)
    languages = (preference.languages if preference else []) or structured.get("languages", [])
    profile = {
        "full_name": full_name,
        "email": (candidate.email or "") if candidate else "",
        "phone": (candidate.phone or "") if candidate else "",
        "home_location": (candidate.home_location or "") if candidate else "",
        "addresses": (candidate.address_values if candidate else []) or [""],
        "max_addresses": profile_service.MAX_CANDIDATE_ADDRESSES,
        "current_title": structured.get("current_title", ""),
        "experience_years": structured.get("experience_years", ""),
        "languages": ", ".join(languages) if isinstance(languages, list) else str(languages or ""),
        "skills": ", ".join(structured.get("skills", []))
        if isinstance(structured.get("skills", []), list)
        else structured.get("skills", ""),
        "completion": completion,
        "cv_filename": candidate.cv_filename if candidate else None,
        "is_confirmed": bool(candidate and candidate.is_confirmed),
        "cv_warnings": [
            translate_text(str(item), locale) for item in cv_import.get("warnings", [])
        ],
        "cv_evidence": evidence,
        "work_experience": structured.get("work_experience", []),
    }
    extra = dict(preference.extra_preferences or {}) if preference else {}
    approved_preference_prompt = _default_preference_prompt(preference, locale)
    stored_prompt = extra.get("preference_prompt_source")
    preference_prompt = (
        stored_prompt.strip()
        if isinstance(stored_prompt, str) and stored_prompt.strip()
        else approved_preference_prompt
    )
    approved_role_taxonomy = _role_taxonomy_prompt(preference, locale)
    preferences = {
        "preference_prompt": preference_prompt,
        "approved_preference_prompt": approved_preference_prompt,
        "role_taxonomy": approved_role_taxonomy,
        "approved_role_taxonomy": approved_role_taxonomy,
        "can_undo": isinstance(
            setting_service.get_setting(session, PREFERENCE_HISTORY_KEY), dict
        ),
    }
    return profile, preferences


def _job_statement(*, active_only: bool = True) -> Any:
    statement = select(JobPosting)
    if active_only:
        statement = statement.where(JobPosting.is_active.is_(True))
    return statement.options(
        selectinload(JobPosting.source),
        selectinload(JobPosting.scores),
        selectinload(JobPosting.application),
    ).order_by(JobPosting.published_at.desc().nullslast(), JobPosting.first_seen_at.desc())


def _current_preferences(session: Session) -> PreferenceProfile | None:
    return _safe_one(
        session, select(PreferenceProfile).order_by(PreferenceProfile.updated_at.desc())
    )


def _priority_job_condition(preference: PreferenceProfile | None) -> Any:
    states = (
        preference.preferred_states
        if preference is not None
        else ["Baden-Württemberg"]
    )
    conditions = [
        JobPosting.state.ilike("Baden%")
        if state.casefold().startswith("baden")
        else JobPosting.state.ilike(state)
        for state in states
        if state.strip()
    ]
    include_remote = preference.include_germany_remote if preference else True
    if include_remote:
        conditions.append(JobPosting.remote_type == "remote")
    return or_(*conditions) if conditions else JobPosting.id == -1


def _workplace_sections(
    preference: PreferenceProfile | None, locale: str
) -> tuple[tuple[str, str, str], ...]:
    states = (
        preference.preferred_states
        if preference and preference.preferred_states
        else ["Baden-Württemberg"]
    )
    regions = ", ".join(states)
    return (
        (
            "onsite",
            translate("Präsenz in {regions}", locale, regions=regions),
            translate("Deine bevorzugten Stellen vor Ort.", locale),
        ),
        (
            "hybrid",
            translate("Hybrid in {regions}", locale, regions=regions),
            translate("Eine Mischung aus Büro und mobilem Arbeiten.", locale),
        ),
        (
            "remote",
            translate("Deutschlandweit Remote", locale),
            translate(
                "Vollständig entfernte Stellen, bewusst getrennt dargestellt.", locale
            ),
        ),
        (
            "unknown",
            translate("Arbeitsmodell noch nicht angegeben", locale),
            translate("Diese Angabe fehlt in der Originalanzeige.", locale),
        ),
    )


def _role_title_matches(job: JobPosting, preference: PreferenceProfile | None) -> bool:
    title = re.sub(r"\s+", " ", job.title).casefold().strip()
    return any(
        re.sub(r"\s+", " ", term).casefold().strip() in title
        for term in _title_match_terms(preference)
    )


def _workplace_condition(section: str) -> Any:
    normalised = func.lower(func.replace(func.coalesce(JobPosting.remote_type, ""), "_", "-"))
    known = {
        "onsite": ("onsite", "on-site", "praesenz", "präsenz"),
        "hybrid": ("hybrid", "teilweise-remote", "partial-remote", "mixed"),
        "remote": ("remote", "fully-remote", "full-remote", "homeoffice"),
    }
    if section == "unknown":
        return ~normalised.in_(tuple(value for values in known.values() for value in values))
    return normalised.in_(known[section])


def _latest_score_value(column: Any) -> Any:
    return (
        select(column)
        .where(JobScore.job_id == JobPosting.id)
        .order_by(JobScore.created_at.desc(), JobScore.id.desc())
        .limit(1)
        .correlate(JobPosting)
        .scalar_subquery()
    )


def _title_match_terms(preference: PreferenceProfile | None) -> tuple[str, ...]:
    return normalized_role_terms(
        (
            preference.primary_role_terms
            if preference is not None
            else list(DEFAULT_PRIMARY_ROLE_TERMS)
        ),
        (
            preference.additional_role_terms
            if preference is not None
            else list(DEFAULT_ADDITIONAL_ROLE_TERMS)
        ),
    )


def _job_query_filters(
    preference: PreferenceProfile | None,
    *,
    q: str,
    min_score: int | None,
    view: str,
) -> tuple[list[Any], int, str]:
    selected_view = view if view in {"all", "matches", "title_matches"} else "all"
    effective_min_score = min_score if min_score is not None else (
        60 if selected_view == "matches" else 0
    )
    conditions: list[Any] = []
    if selected_view != "all":
        conditions.append(JobPosting.is_active.is_(True))
    if selected_view == "matches":
        conditions.append(_priority_job_condition(preference))
    elif selected_view == "title_matches":
        terms = _title_match_terms(preference)
        conditions.append(
            or_(*(JobPosting.title.ilike(f"%{term}%") for term in terms))
            if terms
            else JobPosting.id == -1
        )
    if q.strip():
        pattern = f"%{q.strip()}%"
        conditions.append(
            or_(
                JobPosting.title.ilike(pattern),
                JobPosting.employer.ilike(pattern),
                JobPosting.description_text.ilike(pattern),
            )
        )
    if effective_min_score:
        conditions.append(_latest_score_value(JobScore.relevance_score) >= effective_min_score)
    return conditions, effective_min_score, selected_view


def _paged_job_sections(
    db: Session,
    locale: str,
    preference: PreferenceProfile | None,
    *,
    conditions: list[Any],
    selected_view: str,
    offset: int = 0,
    only_section: str | None = None,
) -> tuple[list[dict[str, Any]], int]:
    sections: list[dict[str, Any]] = []
    total = 0
    score_order = (
        _latest_score_value(JobScore.relevance_score).desc(),
        _latest_score_value(JobScore.candidate_fit_score).desc(),
    )
    for key, label, description in _workplace_sections(preference, locale):
        if only_section is not None and key != only_section:
            continue
        section_condition = _workplace_condition(key)
        section_total = db.scalar(
            select(func.count(JobPosting.id)).where(*conditions, section_condition)
        ) or 0
        total += section_total
        if not section_total:
            continue
        statement = _job_statement(active_only=False).where(*conditions, section_condition)
        if selected_view == "matches":
            statement = statement.order_by(None).order_by(
                *score_order,
                JobPosting.published_at.desc().nullslast(),
                JobPosting.first_seen_at.desc(),
            )
        records = _safe_all(
            db,
            statement.offset(offset).limit(JOBS_BATCH_SIZE),
        )
        sections.append(
            {
                "key": key,
                "label": label,
                "description": description,
                "jobs": [_job_view(record, locale) for record in records],
                "total": section_total,
                "has_more": offset + len(records) < section_total,
            }
        )
    return sections, total


def _filtered_job_views(
    db: Session,
    locale: str,
    *,
    q: str,
    min_score: int | None,
    view: str,
    role_scope: str,
) -> tuple[PreferenceProfile | None, list[dict[str, Any]], int, str]:
    selected_view = view if view in {"all", "matches", "title_matches"} else "all"
    show_all = selected_view == "all"
    effective_min_score = min_score if min_score is not None else (
        60 if selected_view == "matches" else 0
    )
    preference = _current_preferences(db)
    statement = _job_statement(active_only=not show_all)
    if selected_view == "matches":
        statement = statement.where(_priority_job_condition(preference))
    if q.strip():
        pattern = f"%{q.strip()}%"
        statement = statement.where(
            or_(
                JobPosting.title.ilike(pattern),
                JobPosting.employer.ilike(pattern),
                JobPosting.description_text.ilike(pattern),
            )
        )
    records = _safe_all(db, statement)
    if selected_view == "title_matches":
        records = [record for record in records if _role_title_matches(record, preference)]
    views = [_job_view(record, locale) for record in records]
    if effective_min_score:
        views = [job for job in views if job["role_score"] >= effective_min_score]
    if role_scope == "core":
        views = [job for job in views if not job["is_strategic"]]
    elif role_scope == "strategic":
        views = [job for job in views if job["is_strategic"]]
    if selected_view == "matches":
        views.sort(key=lambda item: (item["role_score"], item["fit_score"]), reverse=True)
    return preference, views, effective_min_score, selected_view


def _group_job_views(
    views: list[dict[str, Any]],
) -> defaultdict[str, list[dict[str, Any]]]:
    grouped: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for view in views:
        grouped[view["workplace_class"]].append(view)
    return grouped


async def _form_values(request: Request) -> dict[str, str]:
    content_type = request.headers.get("content-type", "")
    if "application/x-www-form-urlencoded" not in content_type:
        return {}
    parsed = parse_qs(
        (await request.body()).decode("utf-8", errors="replace"), keep_blank_values=True
    )
    return {key: values[-1] for key, values in parsed.items() if values}


@router.get("/static/{path:path}", name="jobradar_static")
def static_asset(path: str) -> FileResponse:
    requested = (STATIC_DIR / path).resolve()
    if not requested.is_relative_to(STATIC_DIR.resolve()) or not requested.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(requested)


@router.get("/language/{locale}", name="set_language")
def set_language(request: Request, locale: str, next: str = "/") -> RedirectResponse:
    """Persist one supported interface language and return to a local path."""

    selected = locale.strip().casefold()
    if selected not in SUPPORTED_LOCALES:
        raise HTTPException(status_code=404)
    session = request.scope.get("session")
    if isinstance(session, dict):
        session["locale"] = selected
    return RedirectResponse(safe_next_path(next), status_code=303)


@router.get("/", response_class=HTMLResponse, name="dashboard")
def dashboard(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    locale = locale_from_request(request)
    preference = _current_preferences(db)
    jobs = _safe_all(db, _job_statement().where(_priority_job_condition(preference)))
    job_views = sorted(
        (_job_view(job, locale) for job in jobs),
        key=lambda item: (item["role_score"], item["fit_score"]),
        reverse=True,
    )
    applications = _safe_all(db, select(ApplicationRecord))
    profile, _ = _profile_views(db, locale)
    active_statuses = {"Bewerbung geplant", "Beworben", "Gespräch", "Angebot"}
    stats = {
        "new_jobs": sum(job["is_new"] for job in job_views),
        "strong_matches": sum(job["role_score"] >= 80 for job in job_views),
        "saved_jobs": sum(record.status == "Merkliste" for record in applications),
        "active_applications": sum(record.status in active_statuses for record in applications),
    }
    return _render(
        request,
        "dashboard.html",
        active_page="dashboard",
        profile=profile,
        stats=stats,
        best_jobs=job_views[:4],
    )


@router.get("/jobs", response_class=HTMLResponse, name="jobs")
def jobs(
    request: Request,
    q: str = "",
    min_score: int | None = Query(default=None, ge=0, le=100),
    view: str = "all",
    role_scope: str = "all",
    db: Session = Depends(get_db),
) -> HTMLResponse:
    locale = locale_from_request(request)
    preference = _current_preferences(db)
    conditions, effective_min_score, selected_view = _job_query_filters(
        preference,
        q=q,
        min_score=min_score,
        view=view,
    )
    if role_scope == "all":
        sections, total = _paged_job_sections(
            db,
            locale,
            preference,
            conditions=conditions,
            selected_view=selected_view,
        )
    else:
        _, views, effective_min_score, selected_view = _filtered_job_views(
            db,
            locale,
            q=q,
            min_score=min_score,
            view=view,
            role_scope=role_scope,
        )
        grouped = _group_job_views(views)
        sections = [
            {
                "key": key,
                "label": label,
                "description": description,
                "jobs": grouped[key][:JOBS_BATCH_SIZE],
                "total": len(grouped[key]),
                "has_more": len(grouped[key]) > JOBS_BATCH_SIZE,
            }
            for key, label, description in _workplace_sections(preference, locale)
            if grouped[key]
        ]
        total = len(views)
    filters = {
        "q": q,
        "min_score": effective_min_score,
        "view": selected_view,
        "role_scope": role_scope,
    }
    default_min_score = 60 if selected_view == "matches" else 0
    return _render(
        request,
        "jobs.html",
        active_page="jobs",
        jobs_by_workplace=sections,
        total=total,
        filters=filters,
        filters_active=bool(
            q
            or role_scope != "all"
            or effective_min_score != default_min_score
        ),
    )


@router.get("/jobs/more", response_class=HTMLResponse, name="jobs_more")
def jobs_more(
    request: Request,
    section: str,
    offset: int = Query(default=0, ge=0),
    q: str = "",
    min_score: int | None = Query(default=None, ge=0, le=100),
    view: str = "all",
    role_scope: str = "all",
    db: Session = Depends(get_db),
) -> HTMLResponse:
    if section not in {"onsite", "hybrid", "remote", "unknown"}:
        raise HTTPException(status_code=404)
    locale = locale_from_request(request)
    preference = _current_preferences(db)
    if role_scope == "all":
        conditions, _, selected_view = _job_query_filters(
            preference,
            q=q,
            min_score=min_score,
            view=view,
        )
        sections, _ = _paged_job_sections(
            db,
            locale,
            preference,
            conditions=conditions,
            selected_view=selected_view,
            offset=offset,
            only_section=section,
        )
        batch = sections[0]["jobs"] if sections else []
        has_more = sections[0]["has_more"] if sections else False
    else:
        _, views, _, _ = _filtered_job_views(
            db,
            locale,
            q=q,
            min_score=min_score,
            view=view,
            role_scope=role_scope,
        )
        section_jobs = _group_job_views(views)[section]
        batch = section_jobs[offset : offset + JOBS_BATCH_SIZE]
        has_more = offset + len(batch) < len(section_jobs)
    response = _render(
        request,
        "job_cards.html",
        jobs=batch,
        show_match_reason=view != "all",
    )
    next_offset = offset + len(batch)
    response.headers["X-Next-Offset"] = str(next_offset)
    response.headers["X-Has-More"] = str(has_more).lower()
    return response


@router.get("/jobs/{job_id}", response_class=HTMLResponse, name="web_job_detail")
def job_detail(request: Request, job_id: int, db: Session = Depends(get_db)) -> HTMLResponse:
    locale = locale_from_request(request)
    statement = (
        select(JobPosting)
        .where(JobPosting.id == job_id)
        .options(
            selectinload(JobPosting.source),
            selectinload(JobPosting.scores),
            selectinload(JobPosting.application),
            selectinload(JobPosting.snapshots),
            selectinload(JobPosting.ai_summary),
        )
    )
    record = _safe_one(db, statement)
    if record is None:
        return _render(request, "not_found.html", active_page="jobs", status_code=404)
    job_view = _job_view(record, locale)
    job_view["description_html"] = _description_markup(_original_description(record), locale)
    return _render(
        request,
        "job_detail.html",
        active_page="jobs",
        job=job_view,
        pipeline_statuses=PIPELINE_STATUSES,
        automatic_summary=_automatic_summary_view(
            current_summary(db, record, locale=locale), locale
        ),
    )


@router.post("/jobs/{job_id}/summary", name="job_summary_generate")
async def job_summary_generate(
    request: Request, job_id: int, db: Session = Depends(get_db)
) -> JSONResponse:
    locale = locale_from_request(request)
    provider = getattr(request.app.state, "codex_provider", None)
    if provider is None:
        return JSONResponse(
            {
                "detail": translate(
                    "Die automatische Zusammenfassung ist derzeit nicht verfügbar.", locale
                )
            },
            status_code=503,
        )
    try:
        summary = await get_or_create_summary(db, job_id, provider, locale=locale)
    except JobSummaryNotFoundError:
        return JSONResponse({"detail": translate("Stelle nicht gefunden", locale)}, status_code=404)
    except (CodexProtocolError, CodexUnavailableError, ValueError) as exc:
        return JSONResponse(
            {"detail": translate_text(str(exc), locale)},
            status_code=503,
        )
    return JSONResponse(_automatic_summary_view(summary, locale))


@router.post("/jobs/{job_id}/status", name="job_status_save")
async def job_status_save(
    request: Request, job_id: int, db: Session = Depends(get_db)
) -> Response:
    values = await _form_values(request)
    requested_status = values.get("status", ApplicationStatus.NEW.value)
    if requested_status == ApplicationStatus.PLANNED.value:
        job = db.scalar(
            select(JobPosting)
            .where(JobPosting.id == job_id)
            .options(selectinload(JobPosting.source))
        )
        if job is None:
            raise HTTPException(status_code=404, detail="Stelle nicht gefunden")
        allow_unverified = values.get("confirm_unverified", "").casefold() == "true"
        with _job_availability_checker(request) as checker:
            availability = verify_jobs_for_application(
                db,
                (job,),
                checker,
                get_settings(),
                allow_unverified=allow_unverified,
            )
        if availability.inactive or (availability.unverified and not allow_unverified):
            db.commit()
            return _render(
                request,
                "availability_confirmation.html",
                active_page="jobs",
                status_code=409,
                flow="status",
                unverified=availability.unverified,
                inactive=availability.inactive,
                action_url=request.url_for("job_status_save", job_id=job_id),
                back_url=request.url_for("web_job_detail", job_id=job_id),
                requested_status=requested_status,
                requested_notes=values.get("notes", ""),
            )
    try:
        job_service.update_application_status(
            db,
            job_id,
            requested_status,
            notes=values.get("notes") or None,
        )
    except (job_service.JobNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return RedirectResponse(request.url_for("web_job_detail", job_id=job_id), status_code=303)


def _render_preference_review(
    request: Request,
    db: Session,
    locale: str,
    plan: PreferenceChangePlan,
    status_code: int = 200,
    role_generation_message: str | None = None,
    role_generation_error: str | None = None,
) -> HTMLResponse:
    profile_view, preference_view = _profile_views(db, locale)
    preference_view["preference_prompt"] = plan.prompt
    preference_view["role_taxonomy"] = plan.role_taxonomy
    return _render(
        request,
        "profile.html",
        active_page="profile",
        profile=profile_view,
        preferences=preference_view,
        preferences_open=True,
        preference_plan=_preference_plan_view(
            plan,
            locale,
            crawling_enabled=setting_service.is_crawling_enabled(db),
        ),
        status_code=status_code,
        role_generation_message=role_generation_message,
        role_generation_error=role_generation_error,
    )


@router.get("/profile", response_class=HTMLResponse, name="profile")
def profile(
    request: Request,
    saved: bool = False,
    cv_uploaded: bool = False,
    confirmed: bool = False,
    preferences_updated: bool = False,
    preferences_undone: bool = False,
    sources_refreshed: bool = False,
    crawl_blocked: bool = False,
    coverage_pending: bool = False,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    locale = locale_from_request(request)
    profile_view, preference_view = _profile_views(db, locale)
    preference_flash_message = (
        translate(
            "Präferenzen angewendet, Ergebnisse neu bewertet und Quellen aktualisiert.",
            locale,
        )
        if preferences_updated and sources_refreshed
        else translate(
            "Präferenzen angewendet. Der Quellenabruf blieb durch die Crawling-Sperre blockiert.",
            locale,
        )
        if preferences_updated and crawl_blocked
        else translate(
            "Präferenzen angewendet und die Neubewertung der vorhandenen Ergebnisse angefordert.",
            locale,
        )
        if preferences_updated
        else translate(
            "Letzte Präferenzänderung rückgängig gemacht. Für vollständige Abdeckung ist ein "
            "neuer Quellenabruf nötig.",
            locale,
        )
        if preferences_undone and coverage_pending
        else translate("Letzte Präferenzänderung rückgängig gemacht.", locale)
        if preferences_undone
        else None
    )
    return _render(
        request,
        "profile.html",
        active_page="profile",
        profile=profile_view,
        preferences=preference_view,
        preferences_open=bool(
            preferences_updated
            or preferences_undone
            or sources_refreshed
            or crawl_blocked
            or coverage_pending
        ),
        preference_flash_message=preference_flash_message,
        flash_message=(
            translate(
                "Lebenslauf lokal eingelesen. Bitte prüfe und bestätige die Angaben.", locale
            )
            if cv_uploaded
            else translate("Profil bestätigt und zur Bewertung freigegeben.", locale)
            if confirmed
            else translate("Profil gespeichert.", locale)
            if saved
            else None
        ),
    )


@router.post(
    "/profile/preferences/roles/generate",
    name="profile_role_terms_generate",
)
async def profile_role_terms_generate(
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    locale = locale_from_request(request)
    form = await request.form()
    prompt = str(form.get("preference_prompt") or "").strip()
    preference = profile_service.get_or_create_preference_profile(db)
    submitted_taxonomy = form.get("role_taxonomy")
    role_taxonomy = (
        str(submitted_taxonomy).strip()
        if submitted_taxonomy is not None
        else _role_taxonomy_prompt(preference, locale)
    )
    role_plan = compile_preference_prompt(
        preference,
        _default_preference_prompt(preference, locale),
        role_taxonomy,
    )
    role_issues = tuple(
        issue for issue in role_plan.issues if issue.source == "role_taxonomy"
    )
    if role_issues:
        plan = compile_preference_prompt(preference, prompt, role_taxonomy)
        return _render_preference_review(
            request,
            db,
            locale,
            plan,
            status_code=422,
            role_generation_error=translate(
                "Die Rollenbegriffe müssen vor der KI-Erweiterung korrigiert werden.",
                locale,
            ),
        )

    provider = getattr(request.app.state, "codex_provider", None)
    if provider is None:
        plan = compile_preference_prompt(preference, prompt, role_taxonomy)
        return _render_preference_review(
            request,
            db,
            locale,
            plan,
            status_code=503,
            role_generation_error=translate(
                "Die KI-Erweiterung ist derzeit nicht verfügbar.",
                locale,
            ),
        )

    primary_terms = list(role_plan.values["primary_role_terms"])
    additional_terms = list(role_plan.values["additional_role_terms"])
    try:
        groups = await provider.generate_similar_role_titles(
            primary_terms,
            list(additional_terms),
            locale=locale,
        )
    except (CodexProtocolError, CodexUnavailableError, ValueError) as exc:
        plan = compile_preference_prompt(preference, prompt, role_taxonomy)
        return _render_preference_review(
            request,
            db,
            locale,
            plan,
            status_code=503,
            role_generation_error=translate_text(str(exc), locale),
        )

    existing = {term.casefold() for term in (*primary_terms, *additional_terms)}
    for group in groups:
        for title in group.titles:
            if len(additional_terms) >= 50:
                break
            if title.casefold() not in existing:
                additional_terms.append(title)
                existing.add(title.casefold())
    generated_count = len(additional_terms) - len(role_plan.values["additional_role_terms"])
    generated_taxonomy = _format_role_taxonomy(
        primary_terms,
        additional_terms,
        locale,
    )
    plan = compile_preference_prompt(preference, prompt, generated_taxonomy)
    message = (
        translate(
            "{count} ähnliche Rollentitel wurden ergänzt. Prüfe sie und wende die Änderung "
            "anschließend an.",
            locale,
            count=generated_count,
        )
        if generated_count
        else translate(
            "Die KI hat keine neuen Rollentitel außerhalb der bereits gespeicherten Begriffe "
            "gefunden.",
            locale,
        )
    )
    return _render_preference_review(
        request,
        db,
        locale,
        plan,
        role_generation_message=message,
    )



@router.post("/profile/preferences/review", name="profile_preferences_review")
async def profile_preferences_review(
    request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    locale = locale_from_request(request)
    form = await request.form()
    prompt = str(form.get("preference_prompt") or "").strip()
    preference = profile_service.get_or_create_preference_profile(db)
    submitted_taxonomy = form.get("role_taxonomy")
    role_taxonomy = (
        str(submitted_taxonomy).strip()
        if submitted_taxonomy is not None
        else _role_taxonomy_prompt(preference, locale)
    )
    plan = compile_preference_prompt(preference, prompt, role_taxonomy)
    return _render_preference_review(
        request,
        db,
        locale,
        plan,
    )


@router.post("/profile/preferences/apply", name="profile_preferences_apply")
async def profile_preferences_apply(
    request: Request, db: Session = Depends(get_db)
) -> Response:
    locale = locale_from_request(request)
    form = await request.form()
    prompt = str(form.get("preference_prompt") or "").strip()
    action = str(form.get("preference_action") or "apply")
    preference = profile_service.get_or_create_preference_profile(db)
    submitted_taxonomy = form.get("role_taxonomy")
    role_taxonomy = (
        str(submitted_taxonomy).strip()
        if submitted_taxonomy is not None
        else _role_taxonomy_prompt(preference, locale)
    )
    plan = compile_preference_prompt(preference, prompt, role_taxonomy)
    if not plan.valid or not plan.changes:
        return _render_preference_review(request, db, locale, plan, status_code=422)

    setting_service.set_setting(
        db,
        PREFERENCE_HISTORY_KEY,
        preference_snapshot(preference),
        description="Previous structured preference snapshot for one-step undo.",
        commit=False,
    )
    updated = profile_service.update_preference_profile(db, plan.values)
    process_pending_rescore(db)

    sources_refreshed = False
    crawl_blocked = False
    if action == "apply_refresh" and plan.coverage_changed:
        result = run_all_enabled(
            db,
            settings=get_settings(),
            query_plan=query_plan_for_preferences(updated),
            run_type="preference_change",
        )
        sources_refreshed = not result.blocked
        crawl_blocked = result.blocked

    target = request.url_for("profile").include_query_params(
        preferences_updated="true",
        **({"sources_refreshed": "true"} if sources_refreshed else {}),
        **({"crawl_blocked": "true"} if crawl_blocked else {}),
        **(
            {"coverage_pending": "true"}
            if plan.coverage_changed and action != "apply_refresh"
            else {}
        ),
    )
    return RedirectResponse(f"{target}#preferences", status_code=303)


@router.post("/profile/preferences/undo", name="profile_preferences_undo")
def profile_preferences_undo(
    request: Request, db: Session = Depends(get_db)
) -> RedirectResponse:
    preference = profile_service.get_or_create_preference_profile(db)
    previous = setting_service.get_setting(db, PREFERENCE_HISTORY_KEY)
    if not isinstance(previous, dict):
        return RedirectResponse(f"{request.url_for('profile')}#preferences", status_code=303)
    current = preference_snapshot(preference)
    undo_values = {
        field: previous[field]
        for field in current
        if field in previous
    }
    needs_refresh = coverage_changed(current, undo_values)
    setting_service.set_setting(
        db,
        PREFERENCE_HISTORY_KEY,
        current,
        description="Previous structured preference snapshot for one-step undo.",
        commit=False,
    )
    profile_service.update_preference_profile(db, undo_values)
    process_pending_rescore(db)
    target = request.url_for("profile").include_query_params(
        preferences_undone="true",
        **({"coverage_pending": "true"} if needs_refresh else {}),
    )
    return RedirectResponse(f"{target}#preferences", status_code=303)


@router.post("/profile", name="profile_save")
async def profile_save(request: Request, db: Session = Depends(get_db)) -> Response:
    locale = locale_from_request(request)
    form = await request.form()
    values = {key: value for key, value in form.items() if isinstance(value, str)}
    addresses = [value for value in form.getlist("addresses") if isinstance(value, str)]
    candidate = profile_service.get_or_create_candidate_profile(db)
    structured = dict(candidate.structured_data or {})
    structured.update(
        {
            "current_title": values.get("current_title", "").strip(),
            "experience_years": values.get("experience_years", "").strip(),
            "skills": [
                item.strip() for item in values.get("skills", "").split(",") if item.strip()
            ],
            "languages": [
                item.strip()
                for item in values.get("languages", "").split(",")
                if item.strip()
            ],
        }
    )
    candidate_values: dict[str, Any] = {
        "full_name": values.get("full_name", "").strip() or None,
        "email": values.get("email", "").strip() or None,
        "phone": values.get("phone", "").strip() or None,
        "addresses": addresses,
        "structured_data": structured,
        "is_confirmed": candidate.is_confirmed,
    }
    upload = form.get("cv")
    cv_uploaded = False
    if isinstance(upload, UploadFile) and upload.filename:
        content = await upload.read(MAX_FILE_BYTES + 1)
        try:
            imported = import_cv(upload.filename, content, upload.content_type or "")
        except CVImportError as exc:
            profile_view, preference_view = _profile_views(db, locale)
            return _render(
                request,
                "profile.html",
                active_page="profile",
                profile=profile_view,
                preferences=preference_view,
                error_message=translate_text(str(exc), locale),
                status_code=400,
            )
        suggestions = imported.profile
        if not structured.get("current_title") and suggestions.current_title:
            structured["current_title"] = suggestions.current_title
        if not structured.get("experience_years") and suggestions.experience_years is not None:
            structured["experience_years"] = suggestions.experience_years
        structured["summary"] = suggestions.summary or ""
        structured["education"] = list(suggestions.education)
        structured["certifications"] = list(suggestions.certifications)
        existing_skills = list(structured.get("skills", []))
        structured["skills"] = list(dict.fromkeys([*existing_skills, *suggestions.skills]))
        structured["work_experience"] = [
            asdict(experience) for experience in suggestions.work_experience
        ]
        structured["cv_import"] = {
            "document_type": imported.document_type,
            "content_type": imported.content_type,
            "page_count": imported.page_count,
            "warnings": list(imported.warnings),
            "evidence": [asdict(evidence) for evidence in suggestions.evidence],
        }
        candidate_values.update(
            {
                "full_name": candidate_values["full_name"] or suggestions.full_name,
                "email": candidate_values["email"] or suggestions.email,
                "phone": candidate_values["phone"] or suggestions.phone,
                "cv_filename": imported.filename,
                "cv_text": imported.text,
                "structured_data": structured,
                "is_confirmed": False,
            }
        )
        if suggestions.languages and not structured["languages"]:
            structured["languages"] = list(suggestions.languages)
        cv_uploaded = True
    try:
        profile_service.update_candidate_profile(db, candidate_values)
    except ValueError as exc:
        profile_view, preference_view = _profile_views(db, locale)
        profile_view["addresses"] = addresses or [""]
        return _render(
            request,
            "profile.html",
            active_page="profile",
            profile=profile_view,
            preferences=preference_view,
            error_message=translate_text(str(exc), locale),
            status_code=400,
        )
    target = request.url_for("profile").include_query_params(
        **({"cv_uploaded": "true"} if cv_uploaded else {"saved": "true"})
    )
    return RedirectResponse(target, status_code=303)


@router.post("/profile/confirm", name="profile_confirm")
def profile_confirm(request: Request, db: Session = Depends(get_db)) -> RedirectResponse:
    profile_service.get_or_create_candidate_profile(db)
    profile_service.update_candidate_profile(db, {"is_confirmed": True})
    return RedirectResponse(
        request.url_for("profile").include_query_params(confirmed="true"), status_code=303
    )


def _bounded_int(value: str | None, *, default: int, low: int, high: int) -> int:
    try:
        parsed = int(value or default)
    except ValueError:
        parsed = default
    return min(high, max(low, parsed))
_PUBLIC_BOARD_HOSTS = {
    "greenhouse": ("greenhouse.io",),
    "lever": ("lever.co",),
    "ashby": ("jobs.ashbyhq.com",),
    "smartrecruiters": ("jobs.smartrecruiters.com",),
    "personio": ("jobs.personio.de",),
    "join": ("join.com",),
    "softgarden": ("softgarden.io",),
    "successfactors": (),
    "workday": ("myworkdayjobs.com", "workdayjobs.com"),
    "workable": ("workable.com",),
    "recruitee": ("recruitee.com",),
    "teamtailor": ("teamtailor.com",),
    "icims": ("icims.com",),
    "oracle": ("oraclecloud.com",),
    "phenom": (),
    "radancy": (),
    "beesite": (),
}

_FIRECRAWL_TARGET_KINDS = frozenset({"job_portal", "company_site"})
_FIRECRAWL_JOB_PORTALS = {
    "adzuna.de": "Adzuna",
    "glassdoor.de": "Glassdoor",
    "indeed.com": "Indeed",
    "jobware.de": "Jobware",
    "jooble.org": "Jooble",
    "kimeta.de": "Kimeta",
    "linkedin.com": "LinkedIn Jobs",
    "meinestadt.de": "meinestadt.de Jobs",
    "monster.de": "Monster",
    "stellenanzeigen.de": "stellenanzeigen.de",
    "stepstone.de": "StepStone",
    "talent.com": "Talent.com",
    "xing.com": "XING Jobs",
}


_URL_BOARD_PROVIDERS = frozenset(
    {"successfactors", "workday", "oracle", "phenom", "radancy", "beesite"}
)


def _public_board_identifier(provider: str, value: str) -> str:
    raw_value = value.strip()
    parsed = urlsplit(raw_value)
    identifier = raw_value
    if parsed.scheme or parsed.netloc:
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
            raise ValueError
        if parsed.scheme != "https" and provider in _URL_BOARD_PROVIDERS:
            raise ValueError
        hostname = parsed.hostname.casefold()
        if provider in _URL_BOARD_PROVIDERS:
            normalized_url = validate_public_url(raw_value, resolve_dns=False).rstrip("/")
            if provider == "workday" and not any(
                hostname == allowed or hostname.endswith(f".{allowed}")
                for allowed in _PUBLIC_BOARD_HOSTS[provider]
            ):
                raise ValueError
            if provider == "oracle" and not hostname.endswith(".oraclecloud.com"):
                raise ValueError
            if provider == "phenom" and not (
                hostname == "phenompeople.com"
                or hostname.endswith(".phenompeople.com")
                or hostname.split(".", 1)[0] in {"career", "careers", "job", "jobs"}
            ):
                raise ValueError
            return normalized_url
        allowed_hosts = _PUBLIC_BOARD_HOSTS[provider]
        if not allowed_hosts or not any(
            hostname == allowed or hostname.endswith(f".{allowed}")
            for allowed in allowed_hosts
        ):
            raise ValueError
        path_parts = [unquote(part) for part in parsed.path.split("/") if part]
        if provider == "personio":
            suffix = ".jobs.personio.de"
            if not hostname.endswith(suffix):
                raise ValueError
            identifier = hostname[: -len(suffix)]
        elif provider in {"softgarden", "recruitee", "teamtailor", "icims"}:
            suffix = f".{_PUBLIC_BOARD_HOSTS[provider][0]}"
            if not hostname.endswith(suffix):
                raise ValueError
            identifier = hostname[: -len(suffix)]
        elif provider == "join":
            identifier = (
                path_parts[1]
                if len(path_parts) >= 2 and path_parts[0].casefold() == "companies"
                else ""
            )
        elif provider == "greenhouse":
            identifier = (
                parse_qs(parsed.query).get("for", [None])[0]
                or (path_parts[0] if path_parts else "")
            )
        else:
            identifier = path_parts[0] if path_parts else ""
    identifier = identifier.strip().strip("/")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", identifier):
        raise ValueError
    return identifier.casefold() if provider == "personio" else identifier


def _public_board_views(metadata: dict[str, Any]) -> list[dict[str, str]]:
    raw_boards = metadata.get("boards", ())
    if not isinstance(raw_boards, list):
        return []
    result: list[dict[str, str]] = []
    for item in raw_boards:
        if isinstance(item, str):
            identifier = item.strip()
            company = ""
        elif isinstance(item, dict):
            identifier = str(item.get("identifier") or "").strip()
            company = str(item.get("company") or "").strip()
        else:
            continue
        if identifier:
            result.append({"identifier": identifier, "company": company})
    return result


def _firecrawl_target_views(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    firecrawl = metadata.get("firecrawl")
    nested = firecrawl if isinstance(firecrawl, dict) else {}
    raw_targets = nested.get("targets", metadata.get("targets", ()))
    if not isinstance(raw_targets, list):
        return []

    result: list[dict[str, str]] = []
    for item in raw_targets:
        if isinstance(item, str):
            url = item.strip()
            target_kind = ""
            label = ""
        elif isinstance(item, dict):
            url = str(item.get("url") or "").strip()
            target_kind = str(
                item.get("target_kind") or item.get("category") or ""
            ).strip()
            label = str(item.get("label") or item.get("company") or "").strip()
        else:
            continue
        if not url:
            continue

        hostname = (urlsplit(url).hostname or "").casefold()
        portal_name = next(
            (
                name
                for domain, name in _FIRECRAWL_JOB_PORTALS.items()
                if hostname == domain or hostname.endswith(f".{domain}")
            ),
            "",
        )
        if target_kind not in _FIRECRAWL_TARGET_KINDS:
            target_kind = "job_portal" if portal_name else "company_site"
        result.append(
            {
                "url": url,
                "hostname": hostname,
                "label": label or portal_name or hostname or url,
                "target_kind": target_kind,
            }
        )
    return result




def _crawler_runtime_detail(
    runtime: CrawlerRuntimeStatus | None,
    fallback_error: str | None,
    locale: str,
) -> str:
    if runtime is None:
        return fallback_error or translate("Kein Laufzeitstatus gespeichert.", locale)
    if runtime.error_message:
        return runtime.error_message
    if runtime.state == "running":
        return translate("Crawlerlauf wird gerade ausgeführt.", locale)
    if runtime.state == "ready":
        return translate("Letzter Crawlerlauf erfolgreich abgeschlossen.", locale)
    if runtime.state == "paused":
        return translate("Crawler ist deaktiviert.", locale)
    if runtime.state == "pending":
        return translate("Noch kein abgeschlossener Crawlerlauf gespeichert.", locale)
    return fallback_error or translate("Keine genaue Fehlermeldung gespeichert.", locale)


def _target_report_detail(report: TargetCrawlReport | None, locale: str) -> str:
    if report is None:
        return translate("Noch kein abgeschlossener Crawl für dieses Ziel gespeichert.", locale)
    return translate(
        (
            "Dokumente geprüft: {documents} · Stellen übernommen: {records} · "
            "Seiten verworfen: {rejected} · Duplikate übersprungen: {duplicates} · "
            "Durch Suchfilter ausgeschlossen: {filtered} · Technische Fehler: {errors}"
        ),
        locale,
        documents=report.documents_seen,
        records=report.found_count,
        rejected=report.pages_rejected,
        duplicates=report.duplicates_skipped,
        filtered=report.query_filtered,
        errors=report.page_error_count,
    )


@router.get("/sources", response_class=HTMLResponse, name="sources")
def sources(
    request: Request,
    synced: bool = False,
    target_added: bool = False,
    board_added: bool = False,
    crawler_updated: bool = False,
    crawl_tab: bool = False,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    locale = locale_from_request(request)
    records = _safe_all(db, select(Source).order_by(Source.name))
    try:
        counts = dict(
            db.execute(
                select(JobPosting.source_id, func.count(JobPosting.id)).group_by(
                    JobPosting.source_id
                )
            ).all()
        )
    except SQLAlchemyError:
        db.rollback()
        counts = {}
    last_run = _safe_one(
        db,
        select(CrawlRun).order_by(
            CrawlRun.started_at.desc().nullslast(), CrawlRun.created_at.desc()
        ),
    )
    settings = get_settings()
    effective_api_access = setting_service.is_crawling_enabled(db)
    effective_firecrawl = setting_service.is_firecrawl_enabled(db)
    source_views = []
    for record in records:
        metadata = dict(record.metadata_json or {})
        targets = _firecrawl_target_views(metadata)
        reports_by_url = (
            {report.url: report for report in firecrawl_target_reports(db, record)}
            if record.kind == "crawler"
            else {}
        )
        runtime_status = (
            firecrawl_runtime_status(db, record, enabled=effective_firecrawl)
            if record.kind == "crawler"
            else None
        )
        for target in targets:
            report = reports_by_url.get(target["url"])
            target["last_crawled_label"] = _format_date(
                report.last_crawled_at if report else None,
                locale,
                with_time=True,
            )
            target["found_count"] = report.found_count if report else 0
            target["stored_job_count"] = report.stored_job_count if report else 0
            report_status = (report.status or "").lower() if report else ""
            target["status_detail"] = _target_report_detail(report, locale)
            target["errors"] = (
                [
                    {
                        "document_url": error.document_url,
                        "message": error.message,
                    }
                    for error in report.errors
                ]
                if report
                else []
            )
            if report_status == "completed":
                target["status"] = translate("Erfolgreich", locale)
                target["status_class"] = "ready"
            elif report_status == "partial":
                target["status"] = translate("Teilerfolg", locale)
                target["status_class"] = "paused"
            elif report_status == "insufficient":
                target["status"] = translate("0 Stellen übernommen", locale)
                target["status_class"] = "paused"
            elif report_status == "empty":
                target["status"] = translate("0 Stellen gefunden", locale)
                target["status_class"] = "paused"
            elif report_status == "blocked":
                target["status"] = translate("Blockiert", locale)
                target["status_class"] = "error"
            elif report_status == "rate_limited":
                target["status"] = translate("Rate-Limit", locale)
                target["status_class"] = "error"
            elif report_status == "failed":
                target["status"] = translate("Technischer Fehler", locale)
                target["status_class"] = "error"
            else:
                target["status"] = translate("Kein Crawl gespeichert", locale)
                target["status_class"] = "paused"
        boards = _public_board_views(metadata)
        configuration_missing = bool(
            (metadata.get("requires_targets") and not targets)
            or (metadata.get("requires_boards") and not boards)
        )
        status_key = (record.status or "").lower()
        environment_enabled = source_environment_enabled(record.slug, settings)
        permission_enabled = (
            effective_firecrawl if record.kind == "crawler" else effective_api_access
        )
        configured_active = bool(
            record.enabled
            and permission_enabled
            and environment_enabled
            and not configuration_missing
        )
        if runtime_status is not None:
            active = runtime_status.state in {"ready", "running"}
            checked_at = runtime_status.last_run_at
            status_detail = _crawler_runtime_detail(runtime_status, record.last_error, locale)
            if configuration_missing:
                status, status_class = translate("Vorbereitet", locale), "paused"
            elif not configured_active:
                status, status_class = translate("Pausiert", locale), "paused"
            elif runtime_status.state == "running":
                status, status_class = translate("Läuft", locale), "ready"
            elif runtime_status.state == "ready":
                status, status_class = translate("Bereit", locale), "ready"
            elif runtime_status.state == "limited":
                status, status_class = translate("Eingeschränkt", locale), "paused"
            elif runtime_status.state == "error":
                status, status_class = translate("Fehler", locale), "error"
            else:
                status, status_class = translate("Noch nicht geprüft", locale), "paused"
        else:
            active = configured_active
            checked_at = record.last_success_at
            status_detail = record.last_error
            if configuration_missing:
                status, status_class = translate("Vorbereitet", locale), "paused"
            elif not active:
                status, status_class = translate("Pausiert", locale), "paused"
            elif status_key in {"error", "failed", "unhealthy"}:
                status, status_class = translate("Fehler", locale), "error"
            else:
                status, status_class = translate("Aktiv", locale), "ready"
        source_views.append(
            {
                "initial": _initials(record.name, "Q"),
                "name": translate_text(record.name, locale),
                "description": translate(
                    "Offizielle Quelle" if record.is_official else "Zusätzliche Quelle", locale
                ),
                "method": (
                    "Firecrawl"
                    if record.kind == "crawler"
                    else translate("Kostenlose API", locale)
                    if record.free_api
                    else (record.kind or "API").upper()
                ),
                "status": status,
                "status_class": status_class,
                "checked_label": _format_date(checked_at, locale, with_time=True),
                "job_count": counts.get(record.id, 0),
                "id": record.id,
                "slug": record.slug,
                "enabled": record.enabled,
                "active": active,
                "runtime_state": runtime_status.state if runtime_status else None,
                "status_detail": status_detail,
                "kind": record.kind,
                "targets": targets,
                "boards": boards,
            }
        )
    api_sources = [source for source in source_views if source["kind"] != "crawler"]
    crawl_sources = [source for source in source_views if source["kind"] == "crawler"]
    crawler_control = max(
        crawl_sources,
        key=lambda source: {
            "paused": 0,
            "pending": 1,
            "ready": 2,
            "limited": 3,
            "error": 4,
            "running": 5,
        }.get(source["runtime_state"], 0),
        default={
            "status": translate("Pausiert", locale),
            "status_class": "paused",
            "runtime_state": "paused",
            "status_detail": None,
        },
    )
    crawl_targets = [
        target for source in crawl_sources for target in source["targets"]
    ]
    crawl_target_counts = {
        "all": len(crawl_targets),
        "job_portal": sum(
            target["target_kind"] == "job_portal" for target in crawl_targets
        ),
        "company_site": sum(
            target["target_kind"] == "company_site" for target in crawl_targets
        ),
    }
    stats = {
        "active": sum(source["active"] for source in source_views),
        "total": len(records),
        "last_run": _format_date(last_run.started_at, locale, with_time=True)
        if last_run
        else translate("Noch nicht gestartet", locale),
        "last_run_detail": translate("API-Abrufe deaktiviert", locale)
        if not effective_api_access
        else translate("Automatische API-Abrufe aktiv", locale),
        "jobs": sum(counts.values()),
    }
    return _render(
        request,
        "sources.html",
        active_page="sources",
        sources=source_views,
        api_sources=api_sources,
        crawl_sources=crawl_sources,
        crawl_targets=crawl_targets,
        crawl_target_counts=crawl_target_counts,
        stats=stats,
        deployment_crawling_allowed=settings.crawling_enabled,
        deployment_firecrawl_allowed=settings.firecrawl_enabled,
        effective_api_access=effective_api_access,
        effective_firecrawl=effective_firecrawl,
        crawler_switch_enabled=setting_service.database_firecrawl_switch(db),
        crawler_control=crawler_control,
        crawl_tab_active=bool(target_added or crawler_updated or crawl_tab),
        flash_message=(
            translate("Manueller Suchlauf wurde abgeschlossen.", locale)
            if synced
            else translate("Crawler-Einstellung wurde gespeichert.", locale)
            if crawler_updated
            else translate("Firecrawl-Ziel wurde gespeichert.", locale)
            if target_added
            else translate("Arbeitgeber-Feed wurde gespeichert.", locale)
            if board_added
            else None
        ),
    )


@router.post("/sources/firecrawl-toggle", name="firecrawl_toggle")
async def firecrawl_toggle(
    request: Request,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    locale = locale_from_request(request)
    values = await _form_values(request)
    try:
        setting_service.set_firecrawl_enabled(db, "crawler_enabled" in values)
    except setting_service.CrawlingLockedError as exc:
        raise HTTPException(status_code=409, detail=translate(str(exc), locale)) from exc
    return RedirectResponse(
        request.url_for("sources").include_query_params(crawler_updated="true"),
        status_code=303,
    )


@router.post("/sources/sync", name="sources_sync")
def sources_sync(request: Request, db: Session = Depends(get_db)) -> RedirectResponse:

    result = run_all_enabled(db, settings=get_settings(), run_type="manual")
    target = request.url_for("sources")
    if result.blocked:
        target = target.include_query_params(blocked="true")
    else:
        target = target.include_query_params(synced="true")
    return RedirectResponse(target, status_code=303)


@router.post("/sources/public-board", name="public_board_add")
async def public_board_add(request: Request, db: Session = Depends(get_db)) -> RedirectResponse:
    locale = locale_from_request(request)
    values = await _form_values(request)
    provider = values.get("provider", "").strip().casefold()
    if provider not in _PUBLIC_BOARD_HOSTS:
        raise HTTPException(
            status_code=422, detail=translate("Unbekannter Feed-Anbieter.", locale)
        )
    try:
        identifier = _public_board_identifier(provider, values.get("board", ""))
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=translate(
                "Bitte eine gültige Board-Kennung oder Anbieter-URL angeben.", locale
            ),
        ) from exc
    company = values.get("company", "").strip()
    if len(company) > 200:
        raise HTTPException(
            status_code=422, detail=translate("Der Arbeitgebername ist zu lang.", locale)
        )

    source = db.scalar(select(Source).where(Source.slug == provider))
    if source is None:
        try:
            from .services.sync import ensure_default_sources

            ensure_default_sources(db)
        except ImportError as exc:
            raise HTTPException(
                status_code=503, detail=translate("Quellenmodul fehlt.", locale)
            ) from exc
        source = db.scalar(select(Source).where(Source.slug == provider))
    if source is None:
        raise HTTPException(
            status_code=503, detail=translate("Feed-Quelle fehlt.", locale)
        )

    metadata = dict(source.metadata_json or {})
    boards = list(metadata.get("boards", []))
    existing_identifiers = {
        str(item.get("identifier") if isinstance(item, dict) else item).casefold()
        for item in boards
    }
    if identifier.casefold() not in existing_identifiers:
        if len(boards) >= 500:
            raise HTTPException(
                status_code=422, detail=translate("Zu viele Arbeitgeber-Feeds.", locale)
            )
        target: dict[str, Any] = {"identifier": identifier}
        if company:
            target["company"] = company
        if provider == "personio":
            target["language"] = "de"
        elif provider == "teamtailor":
            target["locale"] = "de"
        elif provider == "successfactors":
            target.update(
                {
                    "careers_url": identifier,
                    "site": (urlsplit(identifier).hostname or "successfactors").split(
                        ".", 1
                    )[0],
                    "response_format": "html",
                }
            )
        elif provider == "workday":
            path_parts = [part for part in urlsplit(identifier).path.split("/") if part]
            if not path_parts:
                raise HTTPException(
                    status_code=422,
                    detail=translate(
                        "Bitte eine Workday-Karriere-URL mit Tenant und Site angeben.",
                        locale,
                    ),
                )
            target.update(
                {
                    "careers_url": identifier,
                    "tenant": (urlsplit(identifier).hostname or "").split(".", 1)[0],
                    "site": path_parts[-1],
                }
            )
        elif provider == "oracle":
            target["base_url"] = identifier
        elif provider in {"phenom", "radancy", "beesite"}:
            target["careers_url"] = identifier
            target["site" if provider == "radancy" else "brand"] = (
                urlsplit(identifier).hostname or provider
            ).split(".", 1)[0]
        boards.append(target)
    metadata["boards"] = boards
    source.metadata_json = metadata
    source.enabled = True
    db.commit()
    return RedirectResponse(
        request.url_for("sources").include_query_params(board_added="true"),
        status_code=303,
    )


@router.post("/sources/firecrawl-target", name="firecrawl_target_add")
async def firecrawl_target_add(request: Request, db: Session = Depends(get_db)) -> RedirectResponse:
    locale = locale_from_request(request)
    values = await _form_values(request)
    value = values.get("url", "").strip()
    target_kind = values.get("target_kind", "company_site").strip()
    if target_kind not in _FIRECRAWL_TARGET_KINDS:
        raise HTTPException(
            status_code=422, detail=translate("Unbekannte Crawl-Zielart.", locale)
        )
    try:
        value = validate_public_url(value, resolve_dns=False)
    except UnsafePublicURLError as exc:
        raise HTTPException(status_code=422, detail=translate(str(exc), locale)) from exc
    source = db.scalar(select(Source).where(Source.slug == "firecrawl_self_hosted"))
    if source is None:
        try:
            from .services.sync import ensure_default_sources

            ensure_default_sources(db)
        except ImportError as exc:
            raise HTTPException(
                status_code=503, detail=translate("Quellenmodul fehlt.", locale)
            ) from exc
        source = db.scalar(select(Source).where(Source.slug == "firecrawl_self_hosted"))
    if source is None:
        raise HTTPException(
            status_code=503, detail=translate("Firecrawl-Quelle fehlt.", locale)
        )
    metadata = dict(source.metadata_json or {})
    raw_firecrawl = metadata.get("firecrawl")
    firecrawl = dict(raw_firecrawl) if isinstance(raw_firecrawl, dict) else {}
    raw_targets = firecrawl.get("targets", metadata.get("targets", ()))
    targets = list(raw_targets) if isinstance(raw_targets, list) else []
    existing_index = next(
        (
            index
            for index, item in enumerate(targets)
            if (
                item.strip() == value
                if isinstance(item, str)
                else isinstance(item, dict)
                and str(item.get("url") or "").strip() == value
            )
        ),
        None,
    )
    if existing_index is None:
        if len(targets) >= 1000:
            raise HTTPException(
                status_code=422, detail=translate("Zu viele Firecrawl-Ziele.", locale)
            )
        targets.append({"url": value, "target_kind": target_kind})
    else:
        existing = targets[existing_index]
        target = dict(existing) if isinstance(existing, dict) else {"url": value}
        target["target_kind"] = target_kind
        targets[existing_index] = target
    firecrawl["targets"] = targets
    metadata["firecrawl"] = firecrawl
    metadata.pop("targets", None)
    metadata.pop("target_urls", None)
    source.metadata_json = metadata
    source.enabled = True
    db.commit()
    return RedirectResponse(
        request.url_for("sources").include_query_params(target_added="true"), status_code=303
    )


@router.get("/settings", response_class=HTMLResponse, name="settings")
async def settings(
    request: Request, saved: bool = False, db: Session = Depends(get_db)
) -> HTMLResponse:
    locale = locale_from_request(request)
    runtime = get_settings()
    deployment_codex_allowed = bool(getattr(runtime, "codex_enabled", False))
    codex_status: dict[str, Any] = {
        "enabled": deployment_codex_allowed,
        "connected": False,
    }
    provider = getattr(request.app.state, "codex_provider", None)
    if deployment_codex_allowed and provider is not None:
        try:
            codex_status = await provider.status()
        except Exception:
            logger.warning("Codex status check failed", exc_info=True)
            codex_status["error"] = True
    session_data = request.scope.get("session", {})
    device_login = session_data.pop("codex_device_login", None)
    values = {
        "deployment_crawling_allowed": runtime.crawling_enabled,
        "deployment_firecrawl_allowed": runtime.firecrawl_enabled,
        "api_access_enabled": setting_service.is_crawling_enabled(db),
        "firecrawl_enabled": setting_service.is_firecrawl_enabled(db),
        "notifications_enabled": bool(
            setting_service.get_setting(db, "notifications.enabled", False)
        ),
    }
    return _render(
        request,
        "settings.html",
        active_page="settings",
        settings=values,
        codex=codex_status,
        device_login=device_login,
        flash_message=translate("Einstellungen gespeichert.", locale) if saved else None,
    )


@router.post("/settings", name="settings_save")
async def settings_save(request: Request, db: Session = Depends(get_db)) -> RedirectResponse:
    values = await _form_values(request)
    runtime = get_settings()
    if runtime.crawling_enabled:
        setting_service.set_crawling_enabled(db, "api_access_enabled" in values)
        if runtime.firecrawl_enabled:
            setting_service.set_firecrawl_enabled(db, "firecrawl_enabled" in values)
    setting_service.set_setting(db, "notifications.enabled", "notifications_enabled" in values)
    return RedirectResponse(
        request.url_for("settings").include_query_params(saved="true"), status_code=303
    )


@router.post("/settings/codex-login", name="settings_codex_login")
async def settings_codex_login(request: Request) -> RedirectResponse:
    locale = locale_from_request(request)
    provider = getattr(request.app.state, "codex_provider", None)
    if provider is None:
        raise HTTPException(status_code=503, detail=translate("Codex-Adapter fehlt.", locale))
    try:
        result = await provider.begin_login()
    except Exception as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    request.session["codex_device_login"] = {
        "verification_url": result.get("verificationUrl"),
        "user_code": result.get("userCode"),
    }
    return RedirectResponse(request.url_for("settings"), status_code=303)


def _set_draft_flash(request: Request, kind: str, message: str) -> None:
    session = request.scope.get("session")
    if isinstance(session, dict):
        session["application_draft_flash"] = {
            "kind": kind,
            "message": message[:500],
        }


def _pop_draft_flash(request: Request) -> dict[str, str] | None:
    session = request.scope.get("session")
    if not isinstance(session, dict):
        return None
    value = session.pop("application_draft_flash", None)
    return value if isinstance(value, dict) else None


@contextmanager
def _job_availability_checker(request: Request) -> Iterator[JobAvailabilityChecker]:
    configured = getattr(request.app.state, "availability_checker", None)
    if configured is not None:
        yield configured
        return
    checker = HTTPJobAvailabilityChecker()
    try:
        yield checker
    finally:
        checker.close()


@router.post("/application-drafts/generate", name="application_drafts_generate")
async def application_drafts_generate(
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    form = await request.form()
    locale = locale_from_request(request)
    allow_unverified = str(form.get("confirm_unverified") or "").casefold() == "true"
    mode = str(form.get("mode") or "selected")
    job_ids = tuple(
        int(value)
        for value in form.getlist("job_ids")
        if isinstance(value, str) and value.isdigit()
    )
    min_role_score = (
        _bounded_int(str(form.get("min_role_score") or ""), default=70, low=0, high=100)
        if mode == "threshold"
        else None
    )
    min_fit_score = (
        _bounded_int(str(form.get("min_fit_score") or ""), default=75, low=0, high=100)
        if mode == "threshold"
        else None
    )
    limit = _bounded_int(
        str(form.get("limit") or ""),
        default=draft_service.MAX_DRAFT_JOBS_PER_RUN,
        low=1,
        high=draft_service.MAX_DRAFT_JOBS_PER_RUN,
    )
    provider = getattr(request.app.state, "codex_provider", None)
    if provider is None:
        _set_draft_flash(request, "danger", "Codex ist für Bewerbungsentwürfe nicht eingerichtet.")
        return RedirectResponse(request.url_for("applications"), status_code=303)
    try:
        with _job_availability_checker(request) as checker:
            result = await draft_service.generate_drafts(
                db,
                provider,
                availability_checker=checker,
                settings=get_settings(),
                job_ids=job_ids,
                min_role_score=min_role_score,
                min_fit_score=min_fit_score,
                limit=limit,
                allow_unverified=allow_unverified,
            )
    except draft_service.AvailabilityConfirmationRequired as exc:
        continue_ids = tuple(
            dict.fromkeys(
                [job.id for job in exc.selection.eligible_jobs]
                + [concern.job_id for concern in exc.selection.unverified]
            )
        )
        return _render(
            request,
            "availability_confirmation.html",
            active_page="jobs",
            status_code=409,
            flow="drafts",
            unverified=exc.selection.unverified,
            inactive=exc.selection.inactive,
            continue_job_ids=continue_ids,
            action_url=request.url_for("application_drafts_generate"),
            back_url=request.url_for("jobs"),
            locale=locale,
        )
    except (
        draft_service.DraftPipelineError,
        CodexProtocolError,
        CodexUnavailableError,
    ) as exc:
        _set_draft_flash(request, "danger", str(exc))
        return RedirectResponse(request.url_for("applications"), status_code=303)
    except Exception:
        logger.exception("Application draft generation failed")
        _set_draft_flash(
            request,
            "danger",
            "Der Codex-Lauf für die Bewerbungsentwürfe ist fehlgeschlagen.",
        )
        return RedirectResponse(request.url_for("applications"), status_code=303)

    skipped = len(result.skipped_inactive)
    message = f"{len(result.draft_ids)} Bewerbungsentwurf/-entwürfe erstellt und geprüft."
    if skipped:
        message += f" {skipped} nicht mehr verfügbare Stelle(n) wurden ausgelassen."
    _set_draft_flash(request, "success", message)
    if len(result.draft_ids) == 1:
        return RedirectResponse(
            request.url_for("application_draft_detail", draft_id=result.draft_ids[0]),
            status_code=303,
        )
    return RedirectResponse(request.url_for("applications"), status_code=303)


@router.get("/applications", response_class=HTMLResponse, name="applications")
def applications(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    locale = locale_from_request(request)
    records = _safe_all(
        db,
        select(ApplicationRecord)
        .options(selectinload(ApplicationRecord.job).selectinload(JobPosting.scores))
        .order_by(ApplicationRecord.updated_at.desc()),
    )
    grouped: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        if not record.job:
            continue
        job = _job_view(record.job, locale)
        grouped[record.status].append(
            {
                "job_id": job["id"],
                "title": job["title"],
                "company": job["company"],
                "workplace": job["workplace"],
                "workplace_class": job["workplace_class"],
                "role_score": job["role_score"],
                "updated_label": _format_date(record.updated_at, locale),
            }
        )
    pipeline = [
        {"label": translate(label, locale), "items": grouped[label]}
        for label in PIPELINE_STATUSES
    ]
    drafts = _safe_all(
        db,
        select(ApplicationDraft)
        .options(selectinload(ApplicationDraft.job))
        .order_by(ApplicationDraft.updated_at.desc())
        .limit(50),
    )
    draft_views = [
        {
            "id": draft.id,
            "title": draft.job.title if draft.job else "",
            "company": draft.job.employer if draft.job else "",
            "revision": draft.revision,
            "status": draft.status,
            "status_label": translate(
                {
                    "reviewed": "HR-Prüfung bestanden",
                    "needs_revision": "Überarbeitung empfohlen",
                    "edited": "Erneute HR-Prüfung erforderlich",
                }.get(draft.status, "Entwurf"),
                locale,
            ),
            "updated_label": _format_date(draft.updated_at, locale),
        }
        for draft in drafts
    ]
    return _render(
        request,
        "applications.html",
        active_page="applications",
        pipeline=pipeline,
        drafts=draft_views,
        draft_flash=_pop_draft_flash(request),
        max_draft_jobs=draft_service.MAX_DRAFT_JOBS_PER_RUN,
    )


@router.get(
    "/application-drafts/{draft_id}",
    response_class=HTMLResponse,
    name="application_draft_detail",
)
def application_draft_detail(
    request: Request,
    draft_id: int,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    record = draft_service.get_draft(db, draft_id)
    if record is None or record.job is None:
        return _render(request, "not_found.html", active_page="applications", status_code=404)
    return _render(
        request,
        "application_draft.html",
        active_page="applications",
        draft=record,
        job=record.job,
        review=dict(record.review or {}),
        draft_flash=_pop_draft_flash(request),
    )


@router.post("/application-drafts/{draft_id}", name="application_draft_save")
async def application_draft_save(
    request: Request,
    draft_id: int,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    values = await _form_values(request)
    target_draft_id = draft_id
    try:
        saved = draft_service.save_draft(
            db,
            draft_id,
            cv_draft=values.get("cv_draft", ""),
            cover_letter=values.get("cover_letter", ""),
        )
        target_draft_id = saved.id
    except draft_service.DraftPipelineError as exc:
        _set_draft_flash(request, "danger", str(exc))
    else:
        message = (
            "Neue Entwurfsversion gespeichert. Die bisherige HR-Prüfung wurde zurückgesetzt."
            if target_draft_id != draft_id
            else "Keine Änderungen am Entwurf."
        )
        _set_draft_flash(request, "success", message)
    return RedirectResponse(
        request.url_for("application_draft_detail", draft_id=target_draft_id),
        status_code=303,
    )


@router.post(
    "/application-drafts/{draft_id}/review",
    name="application_draft_review",
)
async def application_draft_review(
    request: Request,
    draft_id: int,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    provider = getattr(request.app.state, "codex_provider", None)
    if provider is None:
        _set_draft_flash(request, "danger", "Codex ist für die HR-Prüfung nicht eingerichtet.")
    else:
        try:
            await draft_service.review_draft(db, provider, draft_id)
        except (
            draft_service.DraftPipelineError,
            CodexProtocolError,
            CodexUnavailableError,
        ) as exc:
            _set_draft_flash(request, "danger", str(exc))
        except Exception:
            logger.exception("Application draft review failed")
            _set_draft_flash(request, "danger", "Die HR-Prüfung ist fehlgeschlagen.")
        else:
            _set_draft_flash(request, "success", "Die unabhängige HR-Prüfung wurde aktualisiert.")
    return RedirectResponse(
        request.url_for("application_draft_detail", draft_id=draft_id),
        status_code=303,
    )


__all__ = ["router", "templates"]
