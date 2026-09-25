"""Compile an editable preference prompt into validated structured settings."""

from __future__ import annotations

import copy
import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from ..models import PreferenceProfile

PREFERENCE_HISTORY_KEY = "preferences.last_applied_snapshot"

PREFERENCE_FIELDS = (
    "employment_types",
    "contract_types",
    "preferred_states",
    "include_germany_remote",
    "commute_penalty_minutes",
    "max_commute_minutes",
    "onsite_weight",
    "hybrid_weight",
    "remote_weight",
    "min_salary",
    "target_salary",
    "languages",
    "primary_role_terms",
    "additional_role_terms",
    "extra_preferences",
)

COVERAGE_FIELDS = frozenset(
    {
        "preferred_states",
        "include_germany_remote",
        "primary_role_terms",
        "additional_role_terms",
    }
)

_WORKPLACE_WEIGHTS = {
    "onsite": (1.0, 0.55, 0.2),
    "hybrid": (0.75, 1.0, 0.35),
    "remote": (0.4, 0.7, 1.0),
}

_FIELD_LABELS = {
    "arbeitszeit": "employment_types",
    "employment scope": "employment_types",
    "vertragsarten": "contract_types",
    "contract types": "contract_types",
    "bevorzugte regionen": "preferred_states",
    "preferred regions": "preferred_states",
    "deutschlandweite remote-stellen einbeziehen": "include_germany_remote",
    "include germany-wide remote jobs": "include_germany_remote",
    "bevorzugtes arbeitsmodell": "workplace_preference",
    "preferred work model": "workplace_preference",
    "gewichtung der arbeitsmodelle": "workplace_weights",
    "work-model weights": "workplace_weights",
    "fahrtzeit": "commute",
    "commute": "commute",
    "mindestgehalt pro jahr": "min_salary",
    "minimum annual salary": "min_salary",
    "zielgehalt pro jahr": "target_salary",
    "target annual salary": "target_salary",
    "sprachen": "languages",
    "languages": "languages",
    "bevorzugte branchen": "industries",
    "preferred industries": "industries",
    "bevorzugte unternehmensgrossen": "company_sizes",
    "preferred company sizes": "company_sizes",
    "maximale reisetatigkeit": "max_travel_percent",
    "maximum travel": "max_travel_percent",
}

_ROLE_SECTION_LABELS = {
    "hauptbegriffe": "primary_role_terms",
    "main role terms": "primary_role_terms",
    "primary role terms": "primary_role_terms",
    "zusatzliche titel": "additional_role_terms",
    "additional titles": "additional_role_terms",
    "similar titles": "additional_role_terms",
}

_NOT_SPECIFIED = {
    "",
    "nicht angegeben",
    "nicht festgelegt",
    "keine",
    "none",
    "not provided",
    "not specified",
}

_TRUE_VALUES = {"ja", "yes", "true", "ein", "include", "included"}
_FALSE_VALUES = {"nein", "no", "false", "aus", "exclude", "excluded"}

_STATE_ALIASES = {
    "baden-wurttemberg": "Baden-Württemberg",
    "baden-wuerttemberg": "Baden-Württemberg",
    "bavaria": "Bayern",
    "bayern": "Bayern",
    "berlin": "Berlin",
    "brandenburg": "Brandenburg",
    "bremen": "Bremen",
    "hamburg": "Hamburg",
    "hesse": "Hessen",
    "hessen": "Hessen",
    "lower saxony": "Niedersachsen",
    "niedersachsen": "Niedersachsen",
    "mecklenburg-western pomerania": "Mecklenburg-Vorpommern",
    "mecklenburg-vorpommern": "Mecklenburg-Vorpommern",
    "north rhine-westphalia": "Nordrhein-Westfalen",
    "nordrhein-westfalen": "Nordrhein-Westfalen",
    "rhineland-palatinate": "Rheinland-Pfalz",
    "rheinland-pfalz": "Rheinland-Pfalz",
    "saarland": "Saarland",
    "saxony": "Sachsen",
    "sachsen": "Sachsen",
    "saxony-anhalt": "Sachsen-Anhalt",
    "sachsen-anhalt": "Sachsen-Anhalt",
    "schleswig-holstein": "Schleswig-Holstein",
    "thuringia": "Thüringen",
    "thuringen": "Thüringen",
}


@dataclass(frozen=True, slots=True)
class PromptIssue:
    line_number: int
    line: str
    code: str
    source: str = "preference_prompt"

@dataclass(frozen=True, slots=True)
class PreferenceChange:
    field: str
    before: Any
    after: Any
    requires_crawl: bool = False


@dataclass(frozen=True, slots=True)
class PreferenceChangePlan:
    prompt: str
    role_taxonomy: str
    values: dict[str, Any]
    changes: tuple[PreferenceChange, ...]
    issues: tuple[PromptIssue, ...]
    @property
    def valid(self) -> bool:
        return not self.issues

    @property
    def coverage_changed(self) -> bool:
        return any(change.requires_crawl for change in self.changes)


def _key(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.casefold().strip())
    return "".join(character for character in decomposed if not unicodedata.combining(character))


def _copy_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item) for item in value if str(item).strip()]


def preference_snapshot(preference: PreferenceProfile) -> dict[str, Any]:
    return {
        field: copy.deepcopy(getattr(preference, field))
        for field in PREFERENCE_FIELDS
    }


def _workplace(values: dict[str, Any]) -> str:
    extra = values.get("extra_preferences") or {}
    selected = extra.get("workplace_preference") if isinstance(extra, dict) else None
    if selected in _WORKPLACE_WEIGHTS:
        return str(selected)
    weights = (
        ("onsite", float(values.get("onsite_weight") or 0)),
        ("hybrid", float(values.get("hybrid_weight") or 0)),
        ("remote", float(values.get("remote_weight") or 0)),
    )
    return max(weights, key=lambda item: item[1])[0]


def _semantic_values(values: dict[str, Any]) -> dict[str, Any]:
    extra = copy.deepcopy(values.get("extra_preferences") or {})
    return {
        "employment_types": _copy_list(values.get("employment_types")),
        "contract_types": _copy_list(values.get("contract_types")),
        "preferred_states": _copy_list(values.get("preferred_states")),
        "include_germany_remote": bool(values.get("include_germany_remote")),
        "workplace_preference": _workplace(values),
        "onsite_weight": float(values.get("onsite_weight") or 0),
        "hybrid_weight": float(values.get("hybrid_weight") or 0),
        "remote_weight": float(values.get("remote_weight") or 0),
        "commute_penalty_minutes": int(values.get("commute_penalty_minutes") or 0),
        "max_commute_minutes": int(values.get("max_commute_minutes") or 0),
        "min_salary": values.get("min_salary"),
        "target_salary": values.get("target_salary"),
        "languages": _copy_list(values.get("languages")),
        "primary_role_terms": _copy_list(values.get("primary_role_terms")),
        "additional_role_terms": _copy_list(values.get("additional_role_terms")),
        "industries": _copy_list(extra.get("industries")),
        "company_sizes": _copy_list(extra.get("company_sizes")),
        "max_travel_percent": extra.get("max_travel_percent"),
    }


def _split_list(value: str) -> list[str]:
    if _key(value) in _NOT_SPECIFIED:
        return []
    return list(dict.fromkeys(item.strip() for item in re.split(r"[,;]", value) if item.strip()))


def _parse_states(value: str) -> list[str] | None:
    if _key(value) in _NOT_SPECIFIED:
        return []
    states: list[str] = []
    for item in _split_list(value):
        state = _STATE_ALIASES.get(_key(item))
        if state is None:
            return None
        if state not in states:
            states.append(state)
    return states


def _parse_boolean(value: str) -> bool | None:
    normalized = _key(value)
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    return None


def _parse_salary(value: str) -> int | None | object:
    if _key(value) in _NOT_SPECIFIED:
        return None
    digits = re.sub(r"[^0-9]", "", value)
    if not digits:
        return _INVALID
    amount = int(digits)
    return amount if 0 < amount <= 2_000_000 else _INVALID


def _parse_percentage(value: str) -> int | None | object:
    if _key(value) in _NOT_SPECIFIED:
        return None
    match = re.search(r"\b(\d{1,3})\s*%?", value)
    if not match:
        return _INVALID
    percentage = int(match.group(1))
    return percentage if 0 <= percentage <= 100 else _INVALID


def _parse_workplace(value: str) -> str | None:
    normalized = _key(value)
    if "hybrid" in normalized:
        return "hybrid"
    if "remote" in normalized or "entfernt" in normalized:
        return "remote"
    if "prasenz" in normalized or "on-site" in normalized or "onsite" in normalized:
        return "onsite"
    return None


def _apply_labeled_value(
    values: dict[str, Any], field: str, value: str
) -> str | None:
    extra = values["extra_preferences"]
    if field in {"employment_types", "contract_types", "languages"}:
        values[field] = _split_list(value)
        return None
    if field == "preferred_states":
        parsed_states = _parse_states(value)
        if parsed_states is None:
            return "invalid_state"
        values[field] = parsed_states
        return None
    if field == "include_germany_remote":
        parsed_boolean = _parse_boolean(value)
        if parsed_boolean is None:
            return "invalid_boolean"
        values[field] = parsed_boolean
        return None
    if field == "workplace_preference":
        selected = _parse_workplace(value)
        if selected is None:
            return "invalid_workplace"
        extra["workplace_preference"] = selected
        return None
    if field == "workplace_weights":
        percentages = [int(item) for item in re.findall(r"(\d{1,3})\s*%", value)]
        if len(percentages) != 3 or any(item > 100 for item in percentages):
            return "invalid_weights"
        values["onsite_weight"], values["hybrid_weight"], values["remote_weight"] = (
            item / 100 for item in percentages
        )
        return None
    if field == "commute":
        numbers = [int(item) for item in re.findall(r"\b\d{1,3}\b", value)]
        if len(numbers) < 2 or numbers[0] > numbers[1] or numbers[1] > 480:
            return "invalid_commute"
        values["commute_penalty_minutes"] = numbers[0]
        values["max_commute_minutes"] = numbers[1]
        return None
    if field in {"min_salary", "target_salary"}:
        salary = _parse_salary(value)
        if salary is _INVALID:
            return "invalid_salary"
        values[field] = salary
        return None
    if field in {"industries", "company_sizes"}:
        extra[field] = _split_list(value)
        return None
    if field == "max_travel_percent":
        percentage = _parse_percentage(value)
        if percentage is _INVALID:
            return "invalid_percentage"
        extra[field] = percentage
        return None
    return "unknown_instruction"


def _states_in_text(value: str) -> list[tuple[int, str]]:
    normalized = _key(value)
    found: list[tuple[int, str]] = []
    for alias, state in _STATE_ALIASES.items():
        position = normalized.find(alias)
        if position >= 0 and all(existing != state for _, existing in found):
            found.append((position, state))
    return sorted(found)


def _apply_prose(values: dict[str, Any], line: str) -> bool:
    normalized = _key(line)
    applied = False
    states = _states_in_text(line)
    if states and any(token in normalized for token in ("prefer", "bevorzug", "region", "state")):
        separator_positions = [
            position
            for token in (" instead of ", " statt ")
            if (position := normalized.find(token)) >= 0
        ]
        if separator_positions:
            separator = min(separator_positions)
            preferred = [state for position, state in states if position < separator]
            values["preferred_states"] = preferred or [states[0][1]]
        else:
            values["preferred_states"] = [state for _, state in states]
        applied = True
    if "remote" in normalized:
        if any(token in normalized for token in ("no remote", "kein remote", "ohne remote")):
            values["include_germany_remote"] = False
            applied = True
        elif any(token in normalized for token in ("include", "einbezieh", "allow", "offen")):
            values["include_germany_remote"] = True
            applied = True
    if any(token in normalized for token in ("prefer", "bevorzug")):
        workplace = _parse_workplace(line)
        if workplace is not None:
            values["extra_preferences"]["workplace_preference"] = workplace
            (
                values["onsite_weight"],
                values["hybrid_weight"],
                values["remote_weight"],
            ) = _WORKPLACE_WEIGHTS[workplace]
            applied = True
    commute = re.search(
        r"(?:maximum|maximal(?:e)?|hochstens)\D{0,24}(\d{1,3})\s*(?:minutes|minuten|min)",
        normalized,
    )
    if commute:
        maximum = int(commute.group(1))
        if 1 <= maximum <= 480:
            values["max_commute_minutes"] = maximum
            values["commute_penalty_minutes"] = min(
                int(values["commute_penalty_minutes"]), maximum
            )
            applied = True
    return applied


def _apply_role_taxonomy(
    values: dict[str, Any],
    role_taxonomy: str,
    issues: list[PromptIssue],
) -> None:
    parsed = {
        "primary_role_terms": [],
        "additional_role_terms": [],
    }
    active_field: str | None = None
    for line_number, raw_line in enumerate(role_taxonomy.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        if line.endswith(":"):
            field = _ROLE_SECTION_LABELS.get(_key(line[:-1]))
            if field is None:
                issues.append(
                    PromptIssue(
                        line_number,
                        line,
                        "unknown_role_section",
                        source="role_taxonomy",
                    )
                )
            else:
                active_field = field
            continue
        if active_field is None or not line.startswith(("-", "*")):
            issues.append(
                PromptIssue(
                    line_number,
                    line,
                    "invalid_role_term",
                    source="role_taxonomy",
                )
            )
            continue
        term = line[1:].strip()
        if not term:
            issues.append(
                PromptIssue(
                    line_number,
                    line,
                    "invalid_role_term",
                    source="role_taxonomy",
                )
            )
            continue
        if term.casefold() not in {
            existing.casefold() for existing in parsed[active_field]
        }:
            parsed[active_field].append(term)

    primary_keys = {term.casefold() for term in parsed["primary_role_terms"]}
    parsed["additional_role_terms"] = [
        term
        for term in parsed["additional_role_terms"]
        if term.casefold() not in primary_keys
    ]
    if not parsed["primary_role_terms"]:
        issues.append(
            PromptIssue(0, "", "missing_primary_role_terms", source="role_taxonomy")
        )
    if (
        len(parsed["primary_role_terms"]) > 10
        or len(parsed["additional_role_terms"]) > 50
        or any(
            len(term) > 100
            for term in (
                *parsed["primary_role_terms"],
                *parsed["additional_role_terms"],
            )
        )
    ):
        issues.append(PromptIssue(0, "", "role_list_too_large", source="role_taxonomy"))
    values.update(parsed)


def compile_preference_prompt(
    preference: PreferenceProfile,
    prompt: str,
    role_taxonomy: str | None = None,
) -> PreferenceChangePlan:
    current = preference_snapshot(preference)
    values = copy.deepcopy(current)
    values["extra_preferences"] = copy.deepcopy(values.get("extra_preferences") or {})
    values["extra_preferences"].pop("preference_prompt", None)
    issues: list[PromptIssue] = []
    current_semantic = _semantic_values(current)

    for line_number, raw_line in enumerate(prompt.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        if ":" not in line:
            if not _apply_prose(values, line):
                issues.append(PromptIssue(line_number, line, "unknown_instruction"))
            continue
        label, value = (part.strip() for part in line.split(":", 1))
        field = _FIELD_LABELS.get(_key(label))
        if field is None:
            issues.append(PromptIssue(line_number, line, "unknown_instruction"))
            continue
        error = _apply_labeled_value(values, field, value)
        if error:
            issues.append(PromptIssue(line_number, line, error))
            continue

    if role_taxonomy is not None:
        _apply_role_taxonomy(values, role_taxonomy, issues)

    workplace_changed = _workplace(values) != current_semantic["workplace_preference"]
    weights_changed = (
        values["onsite_weight"],
        values["hybrid_weight"],
        values["remote_weight"],
    ) != (
        current["onsite_weight"],
        current["hybrid_weight"],
        current["remote_weight"],
    )
    if workplace_changed and not weights_changed:
        selected = _workplace(values)
        (
            values["onsite_weight"],
            values["hybrid_weight"],
            values["remote_weight"],
        ) = _WORKPLACE_WEIGHTS[selected]
    if (
        values.get("min_salary") is not None
        and values.get("target_salary") is not None
        and int(values["target_salary"]) < int(values["min_salary"])
    ):
        issues.append(PromptIssue(0, "", "target_below_minimum"))
    if int(values["commute_penalty_minutes"]) > int(values["max_commute_minutes"]):
        issues.append(PromptIssue(0, "", "commute_order"))

    if len(values["preferred_states"]) > 5:
        issues.append(PromptIssue(0, "", "too_many_states"))
    for field in ("employment_types", "contract_types", "languages"):
        items = values[field]
        if len(items) > 20 or any(len(item) > 100 for item in items):
            issues.append(PromptIssue(0, "", "list_too_large"))
    for field in ("industries", "company_sizes"):
        items = values["extra_preferences"].get(field, [])
        if len(items) > 20 or any(len(item) > 100 for item in items):
            issues.append(PromptIssue(0, "", "list_too_large"))
    values["extra_preferences"]["preference_prompt_source"] = prompt.strip()
    after_semantic = _semantic_values(values)
    changes = tuple(
        PreferenceChange(
            field=field,
            before=current_semantic[field],
            after=after_semantic[field],
            requires_crawl=field in COVERAGE_FIELDS,
        )
        for field in current_semantic
        if current_semantic[field] != after_semantic[field]
    )
    return PreferenceChangePlan(
        prompt=prompt.strip(),
        role_taxonomy=(role_taxonomy or "").strip(),
        values={field: copy.deepcopy(values[field]) for field in PREFERENCE_FIELDS},
        changes=changes,
        issues=tuple(issues),
    )


def coverage_changed(before: dict[str, Any], after: dict[str, Any]) -> bool:
    before_semantic = _semantic_values(before)
    after_semantic = _semantic_values(after)
    return any(before_semantic[field] != after_semantic[field] for field in COVERAGE_FIELDS)


_INVALID = object()
