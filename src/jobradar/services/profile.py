"""Candidate and preference profile services."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import CandidateAddress, CandidateProfile, PreferenceProfile
from .rescore import request_rescore

MAX_CANDIDATE_ADDRESSES = 10
MAX_CANDIDATE_ADDRESS_LENGTH = 500


def get_candidate_profile(session: Session) -> CandidateProfile | None:
    return session.scalar(select(CandidateProfile).order_by(CandidateProfile.id).limit(1))


def get_or_create_candidate_profile(session: Session) -> CandidateProfile:
    profile = get_candidate_profile(session)
    if profile is None:
        profile = CandidateProfile()
        session.add(profile)
        session.commit()
        session.refresh(profile)
    return profile


def _mark_rescore_requested(
    session: Session,
    *,
    profile_version: int | None,
    preference_version: int | None,
) -> None:
    request_rescore(
        session,
        reason="profile_changed",
        profile_version=profile_version,
        preference_version=preference_version,
    )


def normalize_candidate_addresses(values: Any) -> list[str]:
    """Normalize, de-duplicate, and limit locally stored commute origins."""

    if values is None:
        raw_values: list[Any] = []
    elif isinstance(values, str):
        raw_values = [values]
    elif isinstance(values, (list, tuple)):
        raw_values = list(values)
    else:
        raise ValueError("Startadressen müssen als Liste übergeben werden.")

    normalized: list[str] = []
    seen: set[str] = set()
    for raw_value in raw_values:
        if not isinstance(raw_value, str):
            raise ValueError("Jede Startadresse muss Text sein.")
        address = " ".join(raw_value.split())
        if not address:
            continue
        if len(address) > MAX_CANDIDATE_ADDRESS_LENGTH:
            raise ValueError(
                "Eine Startadresse darf höchstens "
                f"{MAX_CANDIDATE_ADDRESS_LENGTH} Zeichen lang sein."
            )
        key = address.casefold()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(address)

    if len(normalized) > MAX_CANDIDATE_ADDRESSES:
        raise ValueError(
            f"Es können höchstens {MAX_CANDIDATE_ADDRESSES} Startadressen gespeichert werden."
        )
    return normalized


def _sync_candidate_addresses(
    session: Session,
    profile: CandidateProfile,
    address_values: list[str],
    *,
    previous_home_location: str | None,
    previous_home_latitude: float | None,
    previous_home_longitude: float | None,
    latitude_supplied: bool,
    longitude_supplied: bool,
) -> None:
    existing = {address.address_text.casefold(): address for address in profile.addresses}
    addresses: list[CandidateAddress] = []
    previous_key = (previous_home_location or "").strip().casefold()

    for position, address_text in enumerate(address_values):
        address = existing.pop(address_text.casefold(), None)
        if address is None:
            address = CandidateAddress(address_text=address_text, position=position)
            if position == 0 and address_text.casefold() == previous_key:
                address.latitude = previous_home_latitude
                address.longitude = previous_home_longitude
        else:
            address.address_text = address_text
            address.position = position
        addresses.append(address)

    for removed_address in existing.values():
        session.delete(removed_address)
    profile.addresses = addresses
    if not addresses:
        profile.home_location = None
        profile.home_latitude = None
        profile.home_longitude = None
        return

    primary = addresses[0]
    if latitude_supplied:
        primary.latitude = profile.home_latitude
    if longitude_supplied:
        primary.longitude = profile.home_longitude
    profile.home_location = primary.address_text
    profile.home_latitude = primary.latitude
    profile.home_longitude = primary.longitude


def update_candidate_profile(session: Session, values: dict[str, Any]) -> CandidateProfile:
    address_values: list[str] | None = None
    if "addresses" in values:
        address_values = normalize_candidate_addresses(values["addresses"])
    elif "home_location" in values:
        address_values = normalize_candidate_addresses(values["home_location"])

    profile = get_candidate_profile(session)
    if profile is None:
        profile = CandidateProfile()
        session.add(profile)
        session.flush()
        new_record = True
    else:
        new_record = False

    previous_home_location = profile.home_location
    previous_home_latitude = profile.home_latitude
    previous_home_longitude = profile.home_longitude

    allowed = {
        "full_name",
        "email",
        "phone",
        "home_location",
        "home_latitude",
        "home_longitude",
        "cv_filename",
        "cv_text",
        "structured_data",
        "is_confirmed",
    }
    for key, value in values.items():
        if key in allowed:
            setattr(profile, key, value)
    if address_values is not None:
        _sync_candidate_addresses(
            session,
            profile,
            address_values,
            previous_home_location=previous_home_location,
            previous_home_latitude=previous_home_latitude,
            previous_home_longitude=previous_home_longitude,
            latitude_supplied="home_latitude" in values,
            longitude_supplied="home_longitude" in values,
        )
    elif profile.addresses:
        if "home_latitude" in values:
            profile.addresses[0].latitude = profile.home_latitude
        if "home_longitude" in values:
            profile.addresses[0].longitude = profile.home_longitude
    if not new_record:
        profile.version += 1
    _mark_rescore_requested(
        session,
        profile_version=profile.version,
        preference_version=profile.preferences.version if profile.preferences else None,
    )
    session.commit()
    session.refresh(profile)
    return profile


def get_preference_profile(session: Session) -> PreferenceProfile | None:
    return session.scalar(select(PreferenceProfile).order_by(PreferenceProfile.id).limit(1))


def get_or_create_preference_profile(session: Session) -> PreferenceProfile:
    preference = get_preference_profile(session)
    if preference is None:
        candidate = get_or_create_candidate_profile(session)
        preference = PreferenceProfile(candidate_profile_id=candidate.id)
        session.add(preference)
        session.commit()
        session.refresh(preference)
    return preference


def update_preference_profile(session: Session, values: dict[str, Any]) -> PreferenceProfile:
    preference = get_preference_profile(session)
    if preference is None:
        candidate = get_or_create_candidate_profile(session)
        preference = PreferenceProfile(candidate_profile_id=candidate.id)
        session.add(preference)
        session.flush()
        new_record = True
    else:
        new_record = False

    allowed = {
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
    }
    for key, value in values.items():
        if key in allowed:
            setattr(preference, key, value)
    if not new_record:
        preference.version += 1
    candidate = session.get(CandidateProfile, preference.candidate_profile_id)
    _mark_rescore_requested(
        session,
        profile_version=candidate.version if candidate else None,
        preference_version=preference.version,
    )
    session.commit()
    session.refresh(preference)
    return preference
