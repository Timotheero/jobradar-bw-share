"""Small, explainable preference signals learned from explicit job decisions.

This is deliberately not a statistical or language model.  The latest explicit
``Merkliste``/``Nicht passend`` decision for a job is compared with visible,
structured attributes of another job.  Each example can move the fit score by
at most two points and the accumulated signal is always capped at +/-10 points.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import ApplicationStatus, FeedbackEvent, JobPosting

LEARNING_VERSION = "feedback-v1"
MAX_LEARNING_ADJUSTMENT = 10.0
MAX_POINTS_PER_EXAMPLE = 2.0
MIN_SIMILARITY = 0.2
MAX_EVIDENCE_ITEMS = 8


@dataclass(frozen=True, slots=True)
class FeatureValue:
    """One normalized, user-understandable job attribute."""

    key: str
    label: str
    value: str
    display_value: str
    weight: float


@dataclass(frozen=True, slots=True)
class FeedbackExample:
    """The latest supported explicit decision for one historical job."""

    job_id: int
    decision: str
    direction: int
    features: tuple[FeatureValue, ...]


@dataclass(frozen=True, slots=True)
class LearningContribution:
    """An auditable contribution from one feedback example."""

    feedback_job_id: int
    decision: str
    similarity: float
    points: float
    matched_features: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PreferenceSignal:
    """Bounded preference adjustment and the evidence used to derive it."""

    adjustment: float
    considered_feedback: int
    matched_feedback: int
    positive_matches: int
    negative_matches: int
    contributions: tuple[LearningContribution, ...]
    version: str = LEARNING_VERSION

    def as_reason_dict(self, *, applied_adjustment: float | None = None) -> dict[str, Any]:
        """Return a reason compatible with the persisted score-reason format."""

        applied = self.adjustment if applied_adjustment is None else applied_adjustment
        if applied > 0:
            effect = "positive"
        elif applied < 0:
            effect = "negative"
        else:
            effect = "neutral"

        ranked = sorted(
            self.contributions,
            key=lambda item: (-abs(item.points), item.feedback_job_id),
        )[:MAX_EVIDENCE_ITEMS]
        evidence = [
            (
                f"{item.decision}: Stelle #{item.feedback_job_id}; "
                f"gemeinsam: {', '.join(item.matched_features)}; {item.points:+.1f} Punkte"
            )
            for item in ranked
        ]
        if self.matched_feedback:
            explanation = (
                f"{self.matched_feedback} von {self.considered_feedback} neuesten Entscheidungen "
                "hatten ausreichend gemeinsame, sichtbare Merkmale. Das Signal ist auf "
                f"+/-{MAX_LEARNING_ADJUSTMENT:.0f} Punkte begrenzt."
            )
            if round(applied, 1) != round(self.adjustment, 1):
                explanation += (
                    f" Wegen der 0-bis-100-Grenze wurden von {self.adjustment:+.1f} "
                    f"nur {applied:+.1f} Punkte angewendet."
                )
        elif self.considered_feedback:
            explanation = (
                "Vorhandene Entscheidungen waren fuer diese Stelle nicht aehnlich genug; "
                "deshalb bleibt das Praeferenzsignal neutral."
            )
        else:
            explanation = "Noch keine unterstuetzten Merken-/Ablehnen-Entscheidungen vorhanden."

        return {
            "code": "feedback_preference",
            "label": "Nachvollziehbares Praeferenzsignal",
            "effect": effect,
            "points": round(applied, 1),
            "possible_points": MAX_LEARNING_ADJUSTMENT,
            "evidence": evidence,
            "explanation": explanation,
            "version": self.version,
        }


_FEATURE_DEFINITIONS: tuple[tuple[str, str, float], ...] = (
    ("remote_type", "Arbeitsmodell", 0.25),
    ("employment_type", "Arbeitszeit", 0.20),
    ("contract_type", "Vertragsart", 0.15),
    ("state", "Bundesland", 0.10),
    ("language", "Sprache", 0.05),
    ("industry", "Branche", 0.10),
    ("company_size", "Unternehmensgroesse", 0.05),
    ("role_family", "Rollenfamilie", 0.10),
)


def collect_feedback_examples(session: Session) -> tuple[FeedbackExample, ...]:
    """Load at most the latest supported decision per job.

    Direction is derived from the whitelisted decision value, never from the
    freely shaped ``weight_delta`` payload.  This prevents arbitrary stored
    magnitudes from bypassing the learning cap.
    """

    saved = ApplicationStatus.SAVED.value
    rejected = ApplicationStatus.NOT_SUITABLE.value
    rows = session.execute(
        select(FeedbackEvent, JobPosting)
        .join(JobPosting, JobPosting.id == FeedbackEvent.job_id)
        .where(
            FeedbackEvent.event_type == "application_status",
            FeedbackEvent.value.in_((saved, rejected)),
        )
        .order_by(
            FeedbackEvent.job_id,
            FeedbackEvent.created_at.desc(),
            FeedbackEvent.id.desc(),
        )
    ).all()

    examples: list[FeedbackExample] = []
    seen_job_ids: set[int] = set()
    for event, job in rows:
        if event.job_id in seen_job_ids:
            continue
        seen_job_ids.add(event.job_id)
        examples.append(
            FeedbackExample(
                job_id=event.job_id,
                decision=event.value or "",
                direction=1 if event.value == saved else -1,
                features=extract_job_features(job),
            )
        )
    return tuple(examples)


def extract_job_features(job: JobPosting) -> tuple[FeatureValue, ...]:
    """Extract only stable, visible attributes used by the preference signal."""

    structured = job.structured_data or {}
    raw_values: Mapping[str, Any] = {
        "remote_type": job.remote_type,
        "employment_type": job.employment_type,
        "contract_type": job.contract_type,
        "state": job.state,
        "language": job.language,
        "industry": structured.get("industry") or structured.get("branche"),
        "company_size": structured.get("company_size") or structured.get("unternehmensgroesse"),
        "role_family": structured.get("role_family") or structured.get("role_category"),
    }
    features: list[FeatureValue] = []
    for key, label, weight in _FEATURE_DEFINITIONS:
        display_value = _as_display_value(raw_values.get(key))
        value = _normalize(display_value)
        if value:
            features.append(
                FeatureValue(
                    key=key,
                    label=label,
                    value=value,
                    display_value=display_value,
                    weight=weight,
                )
            )
    return tuple(features)


def calculate_preference_signal(
    job: JobPosting,
    examples: tuple[FeedbackExample, ...],
    *,
    max_adjustment: float = MAX_LEARNING_ADJUSTMENT,
) -> PreferenceSignal:
    """Calculate a deterministic, similarity-based signal for ``job``.

    The target job's own decision is excluded.  This avoids merely echoing its
    current pipeline status back as an apparently learned score.
    """

    bounded_max = max(0.0, min(MAX_LEARNING_ADJUSTMENT, float(max_adjustment)))
    target_features = {feature.key: feature for feature in extract_job_features(job)}
    contributions: list[LearningContribution] = []
    considered = 0

    for example in examples:
        if example.job_id == job.id:
            continue
        considered += 1
        source_features = {feature.key: feature for feature in example.features}
        matched_labels: list[str] = []
        similarity = 0.0
        for key, target in target_features.items():
            source = source_features.get(key)
            if source and source.value == target.value:
                similarity += target.weight
                matched_labels.append(f"{target.label}={target.display_value}")
        similarity = min(1.0, similarity)
        if similarity + 1e-9 < MIN_SIMILARITY:
            continue
        points = round(example.direction * MAX_POINTS_PER_EXAMPLE * similarity, 2)
        contributions.append(
            LearningContribution(
                feedback_job_id=example.job_id,
                decision=example.decision,
                similarity=round(similarity, 2),
                points=points,
                matched_features=tuple(matched_labels),
            )
        )

    raw_adjustment = sum(item.points for item in contributions)
    adjustment = round(max(-bounded_max, min(bounded_max, raw_adjustment)), 1)
    return PreferenceSignal(
        adjustment=adjustment,
        considered_feedback=considered,
        matched_feedback=len(contributions),
        positive_matches=sum(item.points > 0 for item in contributions),
        negative_matches=sum(item.points < 0 for item in contributions),
        contributions=tuple(contributions),
    )


def calculate_preference_signal_from_session(session: Session, job: JobPosting) -> PreferenceSignal:
    """Convenience wrapper for callers scoring only one job."""

    return calculate_preference_signal(job, collect_feedback_examples(session))


def _as_display_value(value: Any) -> str:
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def _normalize(value: str) -> str:
    return re.sub(r"\s+", " ", value.casefold()).strip()
