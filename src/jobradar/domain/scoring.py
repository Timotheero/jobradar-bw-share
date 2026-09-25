"""Explainable, deterministic local job scoring.

This module deliberately does not call an AI service.  Every point can be
traced to an explicit rule and evidence fragment.  A later OpenAI adapter can
add a separate assessment without replacing this audit trail.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from typing import Any, Protocol

SCORING_VERSION = "local-v4"


class JobLike(Protocol):
    title: str
    description_text: str
    state: str | None
    remote_type: str | None
    employment_type: str | None
    contract_type: str | None
    language: str | None
    structured_data: Mapping[str, Any]


class CandidateLike(Protocol):
    structured_data: Mapping[str, Any]
    is_confirmed: bool


class PreferenceLike(Protocol):
    employment_types: list[str]
    contract_types: list[str]
    preferred_states: list[str]
    include_germany_remote: bool
    commute_penalty_minutes: int
    max_commute_minutes: int
    onsite_weight: float
    hybrid_weight: float
    remote_weight: float
    min_salary: int | None
    target_salary: int | None
    languages: list[str]
    primary_role_terms: list[str]
    additional_role_terms: list[str]
    extra_preferences: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ScoreReason:
    code: str
    label: str
    effect: str
    points: float
    possible_points: float
    evidence: tuple[str, ...] = ()
    explanation: str = ""

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["evidence"] = list(self.evidence)
        return payload


@dataclass(frozen=True, slots=True)
class TransparentScore:
    value: float
    reasons: tuple[ScoreReason, ...]
    version: str = SCORING_VERSION

    def reasons_as_dicts(self) -> list[dict[str, Any]]:
        return [reason.as_dict() for reason in self.reasons]


_ROLE_GROUPS: tuple[tuple[str, str, float, tuple[str, ...]], ...] = (
    (
        "executive_support",
        "Direkte Unterstützung der Geschäftsführung",
        25,
        (
            "geschäftsführung",
            "geschäftsleitung",
            "vorstand",
            "ceo",
            "executive support",
            "entlastung der geschäftsführung",
            "office of the ceo",
        ),
    ),
    (
        "calendar_coordination",
        "Termin- und Organisationsmanagement",
        15,
        (
            "terminmanagement",
            "terminkoordination",
            "termine",
            "kalender",
            "calendar management",
            "organisation",
            "wiedervorlage",
        ),
    ),
    (
        "communication",
        "Korrespondenz und Schnittstellenkommunikation",
        15,
        (
            "korrespondenz",
            "kommunikation",
            "ansprechpartner",
            "schnittstelle",
            "stakeholder",
            "correspondence",
        ),
    ),
    (
        "meetings",
        "Vor- und Nachbereitung von Besprechungen",
        12,
        (
            "besprechung",
            "sitzung",
            "protokoll",
            "meeting",
            "agenda",
            "vor- und nachbereitung",
        ),
    ),
    (
        "documents",
        "Präsentationen und Entscheidungsvorlagen",
        10,
        (
            "präsentation",
            "entscheidungsvorlage",
            "recherche",
            "reporting",
            "powerpoint",
            "unterlagen aufbereiten",
        ),
    ),
    (
        "travel_events",
        "Reise- und Veranstaltungsorganisation",
        8,
        (
            "reisemanagement",
            "reiseplanung",
            "gesch\u00e4ftsreise",
            "travel management",
            "veranstaltung",
            "eventorganisation",
        ),
    ),
)

_TITLE_TERMS = (
    "assistenz der geschäftsführung",
    "geschäftsführungsassistenz",
    "executive assistant",
    "management assistenz",
    "vorstandsassistenz",
    "referent der geschäftsführung",
    "referentin der geschäftsführung",
    "chief of staff",
)

_FALSE_FRIENDS = (
    "verkaufsassistenz",
    "pflegeassistenz",
    "sozialassistenz",
    "zahnmedizinische assistenz",
    "laborassistenz",
)


def _normalized(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "").casefold()).strip()


def _matches(text: str, terms: Iterable[str]) -> list[str]:
    return [term for term in terms if _normalized(term) in text]


def _clamp(value: float) -> float:
    return round(max(0.0, min(100.0, value)), 1)


def score_role_relevance(
    job: JobLike, *, additional_role_terms: Iterable[str] = ()
) -> TransparentScore:
    """Score similarity to executive-assistance work, independently of a CV."""

    title = _normalized(job.title)
    body = _normalized(job.description_text)
    combined = f"{title} {body}"
    reasons: list[ScoreReason] = []
    earned = 0.0

    for code, label, possible, terms in _ROLE_GROUPS:
        evidence = _matches(body, terms)
        if evidence:
            # The first match establishes the task; extra matches add confidence.
            points = possible * min(1.0, 0.65 + 0.12 * (len(evidence) - 1))
            effect = "positive"
            explanation = "Die Tätigkeitsbeschreibung enthält passende Aufgaben."
        else:
            points = 0.0
            effect = "neutral"
            explanation = "Für diesen Aufgabenbereich wurde kein eindeutiger Hinweis gefunden."
        earned += points
        reasons.append(
            ScoreReason(
                code=code,
                label=label,
                effect=effect,
                points=round(points, 1),
                possible_points=possible,
                evidence=tuple(evidence[:5]),
                explanation=explanation,
            )
        )

    title_evidence = _matches(title, (*_TITLE_TERMS, *additional_role_terms))
    title_points = 15.0 if title_evidence else 0.0
    earned += title_points
    reasons.append(
        ScoreReason(
            code="role_title",
            label="Passende Rollenbezeichnung",
            effect="positive" if title_evidence else "neutral",
            points=title_points,
            possible_points=15,
            evidence=tuple(title_evidence[:3]),
            explanation=(
                "Der Titel unterstützt die Einordnung; Aufgaben bleiben stärker gewichtet."
                if title_evidence
                else "Der Titel allein liefert keinen Treffer."
            ),
        )
    )

    false_friends = _matches(combined, _FALSE_FRIENDS)
    if false_friends and not _matches(body, _ROLE_GROUPS[0][3]):
        penalty = -20.0
        earned += penalty
        reasons.append(
            ScoreReason(
                code="unrelated_assistance",
                label="Andere Assistenzfunktion",
                effect="negative",
                points=penalty,
                possible_points=0,
                evidence=tuple(false_friends),
                explanation="Die Anzeige scheint eine fachfremde Assistenzrolle zu beschreiben.",
            )
        )

    return TransparentScore(value=_clamp(earned), reasons=tuple(reasons))


def _as_strings(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        return [str(key) for key, enabled in value.items() if enabled]
    if isinstance(value, Iterable):
        result: list[str] = []
        for item in value:
            if isinstance(item, Mapping):
                item = item.get("name") or item.get("label")
            if item is not None:
                result.append(str(item))
        return result
    return [str(value)]


def _norm_set(values: Iterable[str] | None) -> set[str]:
    if values is None:
        return set()
    return {_normalized(value) for value in values if _normalized(value)}


def _salary_from_job(job: JobLike) -> int | None:
    data = job.structured_data or {}
    values = (
        data.get("salary_yearly"),
        data.get("salary_max"),
        data.get("salary_min"),
    )
    for value in values:
        try:
            if value is not None:
                return int(float(value))
        except (TypeError, ValueError):
            continue
    return None


def score_candidate_fit(
    job: JobLike,
    candidate: CandidateLike | None,
    preferences: PreferenceLike | None,
) -> TransparentScore:
    """Score personal fit while treating unavailable salary as neutral.

    Only dimensions with enough information enter the denominator.  This makes
    missing salary or commute data genuinely neutral instead of a hidden penalty.
    """

    reasons: list[ScoreReason] = []
    earned = 0.0
    possible = 0.0
    data = job.structured_data or {}
    candidate_data = (candidate.structured_data or {}) if candidate else {}

    required_skills = _norm_set(_as_strings(data.get("required_skills") or data.get("skills")))
    candidate_skills = _norm_set(
        _as_strings(candidate_data.get("skills") or candidate_data.get("competencies"))
    )
    if required_skills and candidate_skills:
        overlap = sorted(required_skills & candidate_skills)
        ratio = len(overlap) / len(required_skills)
        points = 35 * ratio
        earned += points
        possible += 35
        reasons.append(
            ScoreReason(
                code="skills",
                label="Qualifikationsüberschneidung",
                effect="positive" if overlap else "negative",
                points=round(points, 1),
                possible_points=35,
                evidence=tuple(overlap[:8]),
                explanation=(
                    f"{len(overlap)} von {len(required_skills)} erkannten "
                    "Anforderungen passen."
                ),
            )
        )
    else:
        reasons.append(
            ScoreReason(
                code="skills",
                label="Qualifikationsüberschneidung",
                effect="neutral",
                points=0,
                possible_points=0,
                explanation="Anforderungen oder bestätigte Profilkompetenzen fehlen.",
            )
        )

    preferred_employment = _norm_set(preferences.employment_types if preferences else [])
    employment = _normalized(job.employment_type)
    if employment and preferred_employment:
        matched = any(item in employment or employment in item for item in preferred_employment)
        points = 15.0 if matched else 0.0
        earned += points
        possible += 15
        reasons.append(
            ScoreReason(
                code="employment_type",
                label="Arbeitszeitmodell",
                effect="positive" if matched else "negative",
                points=points,
                possible_points=15,
                evidence=(job.employment_type or "",),
                explanation="Die Arbeitszeit passt." if matched else "Die Arbeitszeit weicht ab.",
            )
        )

    preferred_contracts = _norm_set(preferences.contract_types if preferences else [])
    contract = _normalized(job.contract_type)
    if contract and preferred_contracts:
        matched = any(item in contract or contract in item for item in preferred_contracts)
        points = 10.0 if matched else 0.0
        earned += points
        possible += 10
        reasons.append(
            ScoreReason(
                code="contract_type",
                label="Vertragsart",
                effect="positive" if matched else "negative",
                points=points,
                possible_points=10,
                evidence=(job.contract_type or "",),
                explanation="Die Vertragsart passt." if matched else "Die Vertragsart weicht ab.",
            )
        )

    languages = _norm_set(preferences.languages if preferences else [])
    language = _normalized(job.language)
    if language and languages:
        aliases = {"deutsch": "de", "german": "de", "englisch": "en", "english": "en"}
        language_alias = aliases.get(language, language)
        matched = language in languages or language_alias in languages
        points = 10.0 if matched else 0.0
        earned += points
        possible += 10
        reasons.append(
            ScoreReason(
                code="language",
                label="Anzeigensprache",
                effect="positive" if matched else "negative",
                points=points,
                possible_points=10,
                evidence=(job.language or "",),
                explanation="Die Sprache ist im Profil vorhanden."
                if matched
                else "Die Sprache fehlt im Profil.",
            )
        )

    if preferences:
        mode = _normalized(job.remote_type or "onsite")
        mode_weight = (
            (preferences.remote_weight if preferences.remote_weight is not None else 0.2)
            if mode in {"remote", "fully remote", "vollständig remote"}
            else (preferences.hybrid_weight if preferences.hybrid_weight is not None else 0.55)
            if mode == "hybrid"
            else (preferences.onsite_weight if preferences.onsite_weight is not None else 1.0)
        )
        mode_weight = max(0.0, min(1.0, mode_weight))
        points = 20 * mode_weight
        earned += points
        possible += 20
        reasons.append(
            ScoreReason(
                code="work_mode",
                label="Arbeitsortmodell",
                effect="positive"
                if mode_weight >= 0.75
                else "negative"
                if mode_weight < 0.4
                else "neutral",
                points=round(points, 1),
                possible_points=20,
                evidence=(job.remote_type or "Präsenz",),
                explanation="Die Punktzahl folgt den sichtbaren Präsenz-/Hybrid-/Remote-Gewichten.",
            )
        )

        commute_value = data.get("commute_minutes")
        try:
            commute = int(commute_value) if commute_value is not None else None
        except (TypeError, ValueError):
            commute = None
        if commute is not None and mode not in {"remote", "fully remote", "vollständig remote"}:
            penalty_from = preferences.commute_penalty_minutes or 60
            maximum = max(penalty_from, preferences.max_commute_minutes or 75)
            possible += 10
            if commute <= penalty_from:
                points = 10.0
                effect = "positive"
            elif commute >= maximum:
                points = 0.0
                effect = "negative"
            else:
                span = max(1, maximum - penalty_from)
                points = 10 * (maximum - commute) / span
                effect = "neutral"
            earned += points
            reasons.append(
                ScoreReason(
                    code="commute",
                    label="Einfache Fahrtzeit",
                    effect=effect,
                    points=round(points, 1),
                    possible_points=10,
                    evidence=(f"{commute} Minuten",),
                    explanation="Abwertung beginnt am eingestellten Schwellenwert.",
                )
            )
        else:
            reasons.append(
                ScoreReason(
                    code="commute",
                    label="Einfache Fahrtzeit",
                    effect="neutral",
                    points=0,
                    possible_points=0,
                    explanation="Keine lokale Fahrtzeit vorhanden; daher keine Abwertung.",
                )
            )

    salary = _salary_from_job(job)
    if preferences and salary is not None and preferences.min_salary is not None:
        possible += 10
        if salary >= (preferences.target_salary or preferences.min_salary):
            points, effect = 10.0, "positive"
        elif salary >= preferences.min_salary:
            target = preferences.target_salary or preferences.min_salary
            span = max(1, target - preferences.min_salary)
            points = 5 + 5 * (salary - preferences.min_salary) / span
            effect = "neutral"
        else:
            points, effect = 0.0, "negative"
        earned += points
        reasons.append(
            ScoreReason(
                code="salary",
                label="Gehalt",
                effect=effect,
                points=round(points, 1),
                possible_points=10,
                evidence=(f"{salary:,} EUR/Jahr".replace(",", "."),),
                explanation="Bewertung anhand der eingestellten Mindest- und Zielwerte.",
            )
        )
    else:
        reasons.append(
            ScoreReason(
                code="salary",
                label="Gehalt",
                effect="neutral",
                points=0,
                possible_points=0,
                explanation="Keine belastbare Gehaltsangabe; deshalb keine Abwertung.",
            )
        )

    extra_preferences = (
        preferences.extra_preferences
        if preferences and isinstance(preferences.extra_preferences, Mapping)
        else {}
    )
    preferred_industries = _norm_set(_as_strings(extra_preferences.get("industries")))
    job_industries = _norm_set(
        _as_strings(data.get("industry") or data.get("branche") or data.get("industries"))
    )
    if preferred_industries and job_industries:
        matched = sorted(
            preferred
            for preferred in preferred_industries
            if any(
                preferred in industry or industry in preferred
                for industry in job_industries
            )
        )
        points = 10.0 if matched else 0.0
        possible += 10
        earned += points
        reasons.append(
            ScoreReason(
                code="industry",
                label="Branchenpräferenz",
                effect="positive" if matched else "negative",
                points=points,
                possible_points=10,
                evidence=tuple(sorted(job_industries)),
                explanation=(
                    "Die Branche entspricht deiner Präferenz."
                    if matched
                    else "Die Branche weicht von deiner Präferenz ab."
                ),
            )
        )

    preferred_sizes = _norm_set(_as_strings(extra_preferences.get("company_sizes")))
    job_sizes = _norm_set(
        _as_strings(
            data.get("company_size")
            or data.get("unternehmensgroesse")
            or data.get("company_sizes")
        )
    )
    if preferred_sizes and job_sizes:
        matched = sorted(
            preferred
            for preferred in preferred_sizes
            if any(preferred in size or size in preferred for size in job_sizes)
        )
        points = 5.0 if matched else 0.0
        possible += 5
        earned += points
        reasons.append(
            ScoreReason(
                code="company_size",
                label="Unternehmensgröße",
                effect="positive" if matched else "negative",
                points=points,
                possible_points=5,
                evidence=tuple(sorted(job_sizes)),
                explanation=(
                    "Die Unternehmensgröße entspricht deiner Präferenz."
                    if matched
                    else "Die Unternehmensgröße weicht von deiner Präferenz ab."
                ),
            )
        )

    maximum_travel = extra_preferences.get("max_travel_percent")
    try:
        maximum_travel_percent = (
            int(maximum_travel) if maximum_travel is not None else None
        )
    except (TypeError, ValueError):
        maximum_travel_percent = None
    travel_value = data.get("travel_percent")
    if travel_value is None:
        travel_value = data.get("travel_required")
    travel_percent: int | None
    if isinstance(travel_value, bool):
        travel_percent = 100 if travel_value else 0
    else:
        try:
            travel_percent = (
                int(str(travel_value).strip().removesuffix("%"))
                if travel_value is not None
                else None
            )
        except (TypeError, ValueError):
            travel_percent = None
    if maximum_travel_percent is not None and travel_percent is not None:
        matched = travel_percent <= maximum_travel_percent
        points = 5.0 if matched else 0.0
        possible += 5
        earned += points
        reasons.append(
            ScoreReason(
                code="travel",
                label="Reisetätigkeit",
                effect="positive" if matched else "negative",
                points=points,
                possible_points=5,
                evidence=(f"{travel_percent} %",),
                explanation=(
                    "Die Reisetätigkeit liegt innerhalb deiner Grenze."
                    if matched
                    else "Die Reisetätigkeit überschreitet deine Grenze."
                ),
            )
        )

    if possible == 0:
        reasons.append(
            ScoreReason(
                code="insufficient_profile_data",
                label="Noch zu wenig Profildaten",
                effect="neutral",
                points=0,
                possible_points=0,
                explanation="Der neutrale Startwert bleibt sichtbar, bis Angaben vorliegen.",
            )
        )
        value = 50.0
    else:
        value = 100 * earned / possible

    return TransparentScore(value=_clamp(value), reasons=tuple(reasons))


def evaluate_job(
    job: JobLike,
    candidate: CandidateLike | None = None,
    preferences: PreferenceLike | None = None,
) -> tuple[TransparentScore, TransparentScore]:
    extra_terms = (
        (*preferences.primary_role_terms, *preferences.additional_role_terms)
        if preferences
        else ()
    )
    return (
        score_role_relevance(job, additional_role_terms=extra_terms),
        score_candidate_fit(job, candidate, preferences),
    )
