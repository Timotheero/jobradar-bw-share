"""Shared geographic filtering for remote feeds available to candidates in Germany."""

from __future__ import annotations

import re

_ELIGIBLE_PHRASES = (
    "anywhere",
    "worldwide",
    "global",
    "germany",
    "deutschland",
    "europe",
    "european",
    "emea",
    "dach",
    "cet",
    "cest",
)
_US_ONLY = re.compile(r"\b(?:us|usa|united states)(?:\s+only)?\b", re.IGNORECASE)
_EU_OR_DE = re.compile(r"\b(?:de|eu)\b", re.IGNORECASE)


def available_in_germany(location: str | None) -> bool:
    """Return whether a remote-location restriction permits work from Germany."""

    if location is None or not location.strip():
        return True
    normalized = " ".join(location.casefold().split())
    if _EU_OR_DE.search(normalized):
        return True
    if any(phrase in normalized for phrase in _ELIGIBLE_PHRASES):
        if "anywhere" in normalized and _US_ONLY.search(normalized):
            return False
        return True
    return normalized in {"remote", "fully remote"}
