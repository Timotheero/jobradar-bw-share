"""Canonical editable defaults for source-search role terms."""

from __future__ import annotations

DEFAULT_PRIMARY_ROLE_TERMS: tuple[str, ...] = (
    "Assistenz der Geschäftsführung",
    "Executive Assistant",
)

DEFAULT_ADDITIONAL_ROLE_TERMS: tuple[str, ...] = (
    "Assistenz",
    "Assistant",
    "Geschäftsführung",
    "Geschäftsleitung",
    "Referent",
    "Office Manager",
    "Chief of Staff",
    "CEO Office",
    "Projektkoordination",
    "Stabsmitarbeiter",
    "Vorstandsassistenz",
    "Executive Office",
    "Management Coordinator",
    "Referent des Vorstands",
)


def normalized_role_terms(*groups: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    """Return non-empty role terms once, preserving user-visible order."""

    terms: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for value in group:
            term = value.strip()
            key = term.casefold()
            if term and key not in seen:
                terms.append(term)
                seen.add(key)
    return tuple(terms)
