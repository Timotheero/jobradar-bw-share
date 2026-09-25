"""Safely gated source synchronization and normalized job ingestion.

This module is intentionally network-silent when imported.  Connector objects
are created only after both crawling switches have been checked.  The runtime
setting is the hard deployment boundary; the database setting is the user's
second, reversible switch in the application.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import re
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from html import unescape
from html.parser import HTMLParser
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..connectors.adzuna import AdzunaConnector
from ..connectors.arbeitnow import ArbeitnowConnector
from ..connectors.ashby import AshbyJobBoardConnector
from ..connectors.ba_jobsuche import BAJobsucheConnector
from ..connectors.base import JobConnector, JobQuery, PaginationOptions, RawJobRecord
from ..connectors.beesite import BeeSiteConnector
from ..connectors.configured_boards import (
    BoardTarget,
    ConfiguredJobBoardsConnector,
    board_targets_from_metadata,
)
from ..connectors.deutsche_bahn import DeutscheBahnConnector
from ..connectors.firecrawl_jobs import FirecrawlJobsConnector
from ..connectors.greenhouse import GreenhouseJobBoardConnector
from ..connectors.icims import ICIMSJobBoardConnector
from ..connectors.interamt import InteramtConnector
from ..connectors.jobicy import JobicyConnector
from ..connectors.join import JoinConnector
from ..connectors.jooble import JoobleConnector
from ..connectors.lever import LeverJobBoardConnector
from ..connectors.oracle import OracleRecruitingCloudConnector
from ..connectors.personio import PersonioJobFeedConnector
from ..connectors.phenom import PhenomConnector
from ..connectors.radancy import RadancyConnector
from ..connectors.recruitee import RecruiteeJobBoardConnector
from ..connectors.remotive import RemotiveConnector
from ..connectors.rheinmetall import RheinmetallConnector
from ..connectors.smartrecruiters import SmartRecruitersJobBoardConnector
from ..connectors.softgarden import SoftgardenConnector
from ..connectors.successfactors import SuccessFactorsJobBoardConnector
from ..connectors.teamtailor import TeamtailorJobFeedConnector
from ..connectors.tkms import TKMSConnector
from ..connectors.workable import WorkableJobBoardConnector
from ..connectors.workday import WorkdayJobBoardConnector
from ..domain.role_terms import (
    DEFAULT_ADDITIONAL_ROLE_TERMS,
    DEFAULT_PRIMARY_ROLE_TERMS,
    normalized_role_terms,
)
from ..models import (
    CandidateProfile,
    CrawlRun,
    CrawlRunStatus,
    JobAvailabilityStatus,
    JobScore,
    PreferenceProfile,
    Source,
    utcnow,
)
from . import jobs
from .rescore import score_job_with_learning
from .settings import database_crawling_switch, database_firecrawl_switch

FIRECRAWL_SEARCH_TERMS_PER_QUERY = 8


@dataclass(frozen=True, slots=True)
class SourceDefinition:
    """Stable source catalogue entry; no credentials or mutable user choices."""

    slug: str
    name: str
    kind: str
    base_url: str | None
    enabled: bool
    is_official: bool
    free_api: bool
    status: str = "prepared"
    metadata: Mapping[str, Any] = field(default_factory=dict)


def _board_source_metadata(acquisition: str) -> Mapping[str, Any]:
    return {
        "acquisition": acquisition,
        "default_enabled": True,
        "requires_boards": True,
        "coverage": "Veroeffentlichte Stellen explizit konfigurierter Arbeitgeber",
    }


DEFAULT_SOURCES: tuple[SourceDefinition, ...] = (
    SourceDefinition(
        slug=BAJobsucheConnector.source_info.key,
        name=BAJobsucheConnector.source_info.name,
        kind="api",
        base_url=BAJobsucheConnector.BASE_URL,
        enabled=True,
        is_official=False,
        free_api=True,
        metadata={
            "acquisition": BAJobsucheConnector.source_info.acquisition,
            "experimental": True,
            "default_enabled": True,
            "coverage": "Relevante Stellenanzeigen der BA-Jobsuche mit vollständiger Pagination",
            "documentation_url": BAJobsucheConnector.source_info.documentation_url,
            "terms_url": BAJobsucheConnector.source_info.terms_url,
            "support_level": "Nicht offiziell dokumentierte interne Schnittstelle",
            "authorization": "written_permission_confirmed",
        },
    ),
    SourceDefinition(
        slug=InteramtConnector.source_info.key,
        name=InteramtConnector.source_info.name,
        kind="html",
        base_url=InteramtConnector.BASE_URL,
        enabled=True,
        is_official=True,
        free_api=True,
        metadata={
            "acquisition": InteramtConnector.source_info.acquisition,
            "default_enabled": True,
            "experimental": True,
            "coverage": "Stellen des oeffentlichen Dienstes aus der INTERAMT-Suche",
            "documentation_url": InteramtConnector.source_info.documentation_url,
            "terms_url": InteramtConnector.source_info.terms_url,
        },
    ),
    SourceDefinition(
        slug=DeutscheBahnConnector.source_info.key,
        name=DeutscheBahnConnector.source_info.name,
        kind="html",
        base_url=DeutscheBahnConnector.BASE_URL,
        enabled=True,
        is_official=True,
        free_api=True,
        metadata={
            "acquisition": DeutscheBahnConnector.source_info.acquisition,
            "default_enabled": True,
            "coverage": "Veroeffentlichte Stellen der Deutschen Bahn",
            "documentation_url": DeutscheBahnConnector.source_info.documentation_url,
        },
    ),
    SourceDefinition(
        slug="greenhouse",
        name="Greenhouse – konfigurierte Arbeitgeber",
        kind="api",
        base_url=GreenhouseJobBoardConnector.BASE_URL,
        enabled=True,
        is_official=True,
        free_api=True,
        metadata={
            "acquisition": "official_public_api",
            "default_enabled": True,
            "requires_boards": True,
            "coverage": "Veroeffentlichte Stellen explizit konfigurierter Arbeitgeber",
            "documentation_url": "https://developers.greenhouse.io/job-board.html",
        },
    ),
    SourceDefinition(
        slug="lever",
        name="Lever – konfigurierte Arbeitgeber",
        kind="api",
        base_url=LeverJobBoardConnector.BASE_URL,
        enabled=True,
        is_official=True,
        free_api=True,
        metadata={
            "acquisition": "official_public_api",
            "default_enabled": True,
            "requires_boards": True,
            "coverage": "Veroeffentlichte Stellen explizit konfigurierter Arbeitgeber",
            "documentation_url": "https://github.com/lever/postings-api",
        },
    ),
    SourceDefinition(
        slug="ashby",
        name="Ashby – konfigurierte Arbeitgeber",
        kind="api",
        base_url=AshbyJobBoardConnector.BASE_URL,
        enabled=True,
        is_official=True,
        free_api=True,
        metadata={
            "acquisition": "official_public_api",
            "default_enabled": True,
            "requires_boards": True,
            "coverage": "Veroeffentlichte Stellen explizit konfigurierter Arbeitgeber",
            "documentation_url": (
                "https://developers.ashbyhq.com/docs/public-job-posting-api"
            ),
        },
    ),
    SourceDefinition(
        slug="smartrecruiters",
        name="SmartRecruiters – konfigurierte Arbeitgeber",
        kind="api",
        base_url=SmartRecruitersJobBoardConnector.BASE_URL,
        enabled=True,
        is_official=True,
        free_api=True,
        metadata={
            "acquisition": "official_public_api",
            "default_enabled": True,
            "requires_boards": True,
            "coverage": "Veroeffentlichte Stellen explizit konfigurierter Arbeitgeber",
            "documentation_url": (
                "https://developers.smartrecruiters.com/reference/get-all-postings"
            ),
        },
    ),
    SourceDefinition(
        slug="personio",
        name="Personio – konfigurierte Arbeitgeber",
        kind="feed",
        base_url="https://jobs.personio.de",
        enabled=True,
        is_official=True,
        free_api=True,
        metadata={
            "acquisition": "official_public_feed",
            "default_enabled": True,
            "requires_boards": True,
            "coverage": "Veroeffentlichte Stellen explizit konfigurierter Arbeitgeber",
            "documentation_url": (
                "https://support.personio.de/hc/en-us/search"
                "?query=XML%20feed%20job%20advertisements"
            ),
        },
    ),
    SourceDefinition(
        slug="join",
        name="JOIN – konfigurierte Arbeitgeber",
        kind="html",
        base_url=JoinConnector.BASE_URL,
        enabled=True,
        is_official=True,
        free_api=True,
        metadata=_board_source_metadata("public_server_rendered_board"),
    ),
    SourceDefinition(
        slug="softgarden",
        name="softgarden – konfigurierte Arbeitgeber",
        kind="html",
        base_url="https://softgarden.io",
        enabled=True,
        is_official=True,
        free_api=True,
        metadata=_board_source_metadata("official_public_widget"),
    ),
    SourceDefinition(
        slug="successfactors",
        name="SuccessFactors – konfigurierte Arbeitgeber",
        kind="api",
        base_url=None,
        enabled=True,
        is_official=True,
        free_api=True,
        metadata=_board_source_metadata("public_career_site"),
    ),
    SourceDefinition(
        slug="workday",
        name="Workday – konfigurierte Arbeitgeber",
        kind="api",
        base_url=None,
        enabled=True,
        is_official=True,
        free_api=True,
        metadata=_board_source_metadata("public_cxs_api"),
    ),
    SourceDefinition(
        slug="workable",
        name="Workable – konfigurierte Arbeitgeber",
        kind="api",
        base_url=WorkableJobBoardConnector.BASE_URL,
        enabled=True,
        is_official=True,
        free_api=True,
        metadata=_board_source_metadata("public_widget_api"),
    ),
    SourceDefinition(
        slug="recruitee",
        name="Recruitee – konfigurierte Arbeitgeber",
        kind="api",
        base_url="https://recruitee.com",
        enabled=True,
        is_official=True,
        free_api=True,
        metadata=_board_source_metadata("official_public_api"),
    ),
    SourceDefinition(
        slug="teamtailor",
        name="Teamtailor – konfigurierte Arbeitgeber",
        kind="feed",
        base_url="https://teamtailor.com",
        enabled=True,
        is_official=True,
        free_api=True,
        metadata=_board_source_metadata("official_public_feed"),
    ),
    SourceDefinition(
        slug="icims",
        name="iCIMS – konfigurierte Arbeitgeber",
        kind="html",
        base_url="https://icims.com",
        enabled=True,
        is_official=True,
        free_api=True,
        metadata=_board_source_metadata("public_server_rendered_search"),
    ),
    SourceDefinition(
        slug="oracle",
        name="Oracle Recruiting Cloud – konfigurierte Arbeitgeber",
        kind="api",
        base_url=None,
        enabled=True,
        is_official=True,
        free_api=True,
        metadata=_board_source_metadata("official_public_api"),
    ),
    SourceDefinition(
        slug="phenom",
        name="Phenom – konfigurierte Arbeitgeber",
        kind="api",
        base_url=None,
        enabled=True,
        is_official=True,
        free_api=True,
        metadata=_board_source_metadata("public_widget_api"),
    ),
    SourceDefinition(
        slug="radancy",
        name="Radancy – konfigurierte Arbeitgeber",
        kind="html",
        base_url=None,
        enabled=True,
        is_official=True,
        free_api=True,
        metadata=_board_source_metadata("official_public_career_site"),
    ),
    SourceDefinition(
        slug="beesite",
        name="BeeSite – konfigurierte Arbeitgeber",
        kind="api",
        base_url=None,
        enabled=True,
        is_official=True,
        free_api=True,
        metadata=_board_source_metadata("official_public_career_api"),
    ),
    SourceDefinition(
        slug=RheinmetallConnector.source_info.key,
        name=RheinmetallConnector.source_info.name,
        kind="html",
        base_url="https://www.rheinmetall.com",
        enabled=True,
        is_official=True,
        free_api=True,
        metadata={
            "acquisition": RheinmetallConnector.source_info.acquisition,
            "default_enabled": True,
            "coverage": "Veroeffentlichte Stellen von Rheinmetall",
            "documentation_url": RheinmetallConnector.source_info.documentation_url,
        },
    ),
    SourceDefinition(
        slug=TKMSConnector.source_info.key,
        name=TKMSConnector.source_info.name,
        kind="api",
        base_url="https://jobs.tkmsgroup.com",
        enabled=True,
        is_official=True,
        free_api=True,
        metadata={
            "acquisition": TKMSConnector.source_info.acquisition,
            "default_enabled": True,
            "coverage": "Veroeffentlichte Stellen von TKMS",
            "documentation_url": TKMSConnector.source_info.documentation_url,
        },
    ),
    SourceDefinition(
        slug=ArbeitnowConnector.source_info.key,
        name=ArbeitnowConnector.source_info.name,
        kind="api",
        base_url=ArbeitnowConnector.BASE_URL,
        enabled=True,
        is_official=True,
        free_api=True,
        metadata={
            "acquisition": ArbeitnowConnector.source_info.acquisition,
            "default_enabled": True,
            "coverage": "Deutschlandweite Stellenangebote mit inkrementeller Pagination",
            "documentation_url": ArbeitnowConnector.source_info.documentation_url,
            "terms_url": ArbeitnowConnector.source_info.terms_url,
            "attribution": "Arbeitnow",
            "overlap_hours": 24,
        },
    ),
    SourceDefinition(
        slug=AdzunaConnector.source_info.key,
        name=AdzunaConnector.source_info.name,
        kind="api",
        base_url=AdzunaConnector.BASE_URL,
        enabled=True,
        is_official=True,
        free_api=True,
        metadata={
            "acquisition": AdzunaConnector.source_info.acquisition,
            "default_enabled": True,
            "coverage": "Stellenanzeigen aus Deutschland",
            "documentation_url": AdzunaConnector.source_info.documentation_url,
            "terms_url": AdzunaConnector.source_info.terms_url,
            "attribution": "Adzuna",
            "overlap_hours": 24,
        },
    ),

    SourceDefinition(
        slug=JobicyConnector.source_info.key,
        name=JobicyConnector.source_info.name,
        kind="api",
        base_url=JobicyConnector.BASE_URL,
        enabled=True,
        is_official=True,
        free_api=True,
        metadata={
            "acquisition": JobicyConnector.source_info.acquisition,
            "default_enabled": True,
            "coverage": "Fuer Deutschland geeignete Remote-Assistenz- und Stabsrollen",
            "documentation_url": JobicyConnector.source_info.documentation_url,
            "terms_url": JobicyConnector.source_info.terms_url,
            "attribution": "Jobicy",
        },
    ),
    SourceDefinition(
        slug=JoobleConnector.source_info.key,
        name=JoobleConnector.source_info.name,
        kind="api",
        base_url=JoobleConnector.BASE_URL,
        enabled=True,
        is_official=True,
        free_api=True,
        metadata={
            "acquisition": JoobleConnector.source_info.acquisition,
            "default_enabled": True,
            "coverage": "Stellenanzeigen aus Deutschland",
            "documentation_url": JoobleConnector.source_info.documentation_url,
            "attribution": "Jooble",
            "overlap_hours": 24,
        },
    ),
    SourceDefinition(
        slug=RemotiveConnector.source_info.key,
        name=RemotiveConnector.source_info.name,
        kind="api",
        base_url=RemotiveConnector.BASE_URL,
        enabled=True,
        is_official=True,
        free_api=True,
        metadata={
            "acquisition": RemotiveConnector.source_info.acquisition,
            "default_enabled": True,
            "coverage": "Fuer Deutschland geeignete internationale Remote-Stellen",
            "documentation_url": RemotiveConnector.source_info.documentation_url,
            "terms_url": RemotiveConnector.source_info.terms_url,
            "attribution": "Remotive",
        },
    ),
    SourceDefinition(
        slug=FirecrawlJobsConnector.source_info.key,
        name=FirecrawlJobsConnector.source_info.name,
        kind="crawler",
        base_url="http://firecrawl-api:3002/v2",
        enabled=True,
        is_official=False,
        free_api=True,
        metadata={
            "acquisition": "self_hosted_crawler",
            "experimental": False,
            "default_enabled": True,
            "self_hosted": True,
            "requires_targets": True,
            "coverage": "Ergaenzende Websites nach expliziter Zielkonfiguration",
            "documentation_url": FirecrawlJobsConnector.source_info.documentation_url,
            "configuration": "Zielseiten unter metadata.firecrawl.targets eintragen.",
        },
    ),
)
def _join_boards(source: Source) -> ConfiguredJobBoardsConnector:
    return _configured_boards(
        source,
        lambda target: JoinConnector(target.identifier, company=target.company),
    )


def _softgarden_boards(source: Source) -> ConfiguredJobBoardsConnector:
    return _configured_boards(
        source,
        lambda target: SoftgardenConnector(
            target.identifier,
            company=target.company,
            language=str(target.options.get("language") or "de"),
        ),
    )


def _successfactors_boards(source: Source) -> ConfiguredJobBoardsConnector:
    return _configured_boards(
        source,
        lambda target: SuccessFactorsJobBoardConnector(
            str(target.options.get("careers_url") or target.identifier),
            identifier=str(target.options.get("site") or target.identifier),
            company=target.company,
            search_path=str(target.options.get("search_path") or "/search/"),
            response_format=str(target.options.get("response_format") or "json"),
        ),
    )


def _workday_boards(source: Source) -> ConfiguredJobBoardsConnector:
    return _configured_boards(
        source,
        lambda target: WorkdayJobBoardConnector(
            str(target.options.get("careers_url") or target.identifier),
            tenant=str(target.options.get("tenant") or ""),
            site=str(target.options.get("site") or ""),
            company=target.company,
        ),
    )


def _workable_boards(source: Source) -> ConfiguredJobBoardsConnector:
    return _configured_boards(
        source,
        lambda target: WorkableJobBoardConnector(target.identifier, company=target.company),
    )


def _recruitee_boards(source: Source) -> ConfiguredJobBoardsConnector:
    return _configured_boards(
        source,
        lambda target: RecruiteeJobBoardConnector(target.identifier, company=target.company),
    )


def _teamtailor_boards(source: Source) -> ConfiguredJobBoardsConnector:
    return _configured_boards(
        source,
        lambda target: TeamtailorJobFeedConnector(
            target.identifier,
            company=target.company,
            locale=str(target.options.get("locale") or "") or None,
        ),
    )


def _icims_boards(source: Source) -> ConfiguredJobBoardsConnector:
    return _configured_boards(
        source,
        lambda target: ICIMSJobBoardConnector(target.identifier, company=target.company),
    )


def _oracle_boards(source: Source) -> ConfiguredJobBoardsConnector:
    return _configured_boards(
        source,
        lambda target: OracleRecruitingCloudConnector(
            str(target.options.get("base_url") or target.identifier),
            site_number=target.options.get("site_number"),
            company=target.company,
        ),
    )


def _phenom_boards(source: Source) -> ConfiguredJobBoardsConnector:
    return _configured_boards(
        source,
        lambda target: PhenomConnector(
            str(target.options.get("careers_url") or target.identifier),
            company=target.company,
            widget_path=str(target.options.get("widget_path") or "/widgets"),
        ),
    )


def _radancy_boards(source: Source) -> ConfiguredJobBoardsConnector:
    return _configured_boards(
        source,
        lambda target: RadancyConnector(
            str(target.options.get("site") or target.identifier),
            str(target.options.get("careers_url") or target.identifier),
            company=target.company,
            search_path=str(target.options.get("search_path") or "/search-jobs/results"),
        ),
    )


def _beesite_boards(source: Source) -> ConfiguredJobBoardsConnector:
    return _configured_boards(
        source,
        lambda target: BeeSiteConnector(
            str(target.options.get("brand") or target.identifier),
            str(target.options.get("careers_url") or target.identifier),
            company=target.company,
            search_path=str(target.options.get("search_path") or "/api/jobs"),
        ),
    )





@dataclass(frozen=True, slots=True)
class SyncQueryPlan:
    """Configurable coverage plan used by the BA and future connectors.

    Broad role and management terms keep every BA result set below the API's
    10,000-result window while allowing local task-based scoring to reject false
    positives. The BA home-office filter is best effort: it includes hybrid
    roles, which remain explicitly classified as hybrid after normalization.
    """

    search_terms: tuple[str | None, ...] = normalized_role_terms(
        list(DEFAULT_PRIMARY_ROLE_TERMS), list(DEFAULT_ADDITIONAL_ROLE_TERMS)
    )
    bw_location: str = "Baden-Württemberg"
    preferred_locations: tuple[str, ...] = ()
    include_germany_remote: bool = True
    remote_location: str = "Deutschland"
    remote_filter_key: str = "homeoffice"
    remote_filter_value: str = "nv_true"
    page_size: int = 100
    max_pages: int | None = None
    published_since_days: int | None = None

    def __post_init__(self) -> None:
        if not self.search_terms:
            raise ValueError("search_terms must contain at least one broad or explicit term")
        PaginationOptions(page_size=self.page_size, max_pages=self.max_pages)

    def queries_for(self, source: Source) -> tuple[JobQuery, ...]:
        pagination = PaginationOptions(page_size=self.page_size, max_pages=self.max_pages)
        if source.slug == FirecrawlJobsConnector.source_info.key:
            metadata = source.metadata_json if isinstance(source.metadata_json, Mapping) else {}
            firecrawl = metadata.get("firecrawl")
            firecrawl_config = firecrawl if isinstance(firecrawl, Mapping) else {}
            portal_search = firecrawl_config.get("portal_search")
            portal_search_config = (
                portal_search if isinstance(portal_search, Mapping) else {}
            )
            if bool(portal_search_config.get("enabled")):
                queries: list[JobQuery] = []
                search_terms = tuple(
                    str(term).strip() for term in self.search_terms if term and str(term).strip()
                )
                if not search_terms:
                    return (JobQuery(pagination=pagination),)
                locations = self.preferred_locations or (self.bw_location,)
                for start in range(0, len(search_terms), FIRECRAWL_SEARCH_TERMS_PER_QUERY):
                    terms = search_terms[start : start + FIRECRAWL_SEARCH_TERMS_PER_QUERY]
                    text = " OR ".join(
                        '"' + term.replace('"', " ").strip() + '"' for term in terms
                    )
                    for location in locations:
                        queries.append(
                            JobQuery(
                                text=text,
                                location=location,
                                published_since_days=self.published_since_days,
                                pagination=pagination,
                            )
                        )
                    if self.include_germany_remote:
                        queries.append(
                            JobQuery(
                                text=text,
                                location=self.remote_location,
                                published_since_days=self.published_since_days,
                                pagination=pagination,
                                filters={self.remote_filter_key: self.remote_filter_value},
                            )
                        )
                return tuple(queries)
            return (JobQuery(pagination=pagination),)
        if source.slug != BAJobsucheConnector.source_info.key:
            return (JobQuery(pagination=pagination),)

        queries: list[JobQuery] = []
        locations = self.preferred_locations or (self.bw_location,)
        for term in self.search_terms:
            for location in locations:
                queries.append(
                    JobQuery(
                        text=term,
                        location=location,
                        published_since_days=self.published_since_days,
                        pagination=pagination,
                    )
                )
            if self.include_germany_remote:
                queries.append(
                    JobQuery(
                        text=term,
                        location=self.remote_location,
                        published_since_days=self.published_since_days,
                        pagination=pagination,
                        filters={self.remote_filter_key: self.remote_filter_value},
                    )
                )
        return tuple(queries)


def query_plan_for_preferences(preference: PreferenceProfile) -> SyncQueryPlan:
    """Build source coverage from confirmed structured preferences."""

    locations = tuple(
        dict.fromkeys(
            location.strip()
            for location in (preference.preferred_states or ())
            if location.strip()
        )
    )
    search_terms = normalized_role_terms(
        preference.primary_role_terms or [],
        preference.additional_role_terms or [],
    )
    return SyncQueryPlan(
        search_terms=search_terms,
        preferred_locations=locations or ("Baden-Württemberg",),
        include_germany_remote=preference.include_germany_remote,
    )


ConnectorBuilder = Callable[[Source], JobConnector]


class ConnectorFactoryLike(Protocol):
    def create(self, source: Source) -> JobConnector | None: ...


def _configured_boards(
    source: Source, builder: Callable[[BoardTarget], JobConnector]
) -> ConfiguredJobBoardsConnector:
    metadata = source.metadata_json if isinstance(source.metadata_json, Mapping) else {}
    documentation_url = str(metadata.get("documentation_url") or "")
    return ConfiguredJobBoardsConnector(
        source_key=source.slug,
        source_name=source.name,
        documentation_url=documentation_url,
        targets=board_targets_from_metadata(metadata),
        builder=builder,
    )


def _greenhouse_boards(source: Source) -> ConfiguredJobBoardsConnector:
    return _configured_boards(
        source,
        lambda target: GreenhouseJobBoardConnector(
            target.identifier, company=target.company
        ),
    )


def _lever_boards(source: Source) -> ConfiguredJobBoardsConnector:
    return _configured_boards(
        source,
        lambda target: LeverJobBoardConnector(target.identifier, company=target.company),
    )


def _ashby_boards(source: Source) -> ConfiguredJobBoardsConnector:
    return _configured_boards(
        source,
        lambda target: AshbyJobBoardConnector(target.identifier, company=target.company),
    )


def _smartrecruiters_boards(source: Source) -> ConfiguredJobBoardsConnector:
    return _configured_boards(
        source,
        lambda target: SmartRecruitersJobBoardConnector(
            target.identifier, company=target.company
        ),
    )


def _personio_boards(source: Source) -> ConfiguredJobBoardsConnector:
    return _configured_boards(
        source,
        lambda target: PersonioJobFeedConnector(
            target.identifier,
            company=target.company,
            language=str(target.options.get("language") or "de"),
        ),
    )


class ConnectorFactory:
    """Lazy registry for connector dependency injection.

    Builders are deliberately callables instead of ready connector instances.
    Consequently even constructing the default factory cannot open a client or
    contact a source.  Firecrawl orchestration can later register a builder
    that converts configured crawl results into ``RawJobRecord`` values.
    """

    def __init__(self, builders: Mapping[str, ConnectorBuilder] | None = None) -> None:
        self._builders: dict[str, ConnectorBuilder] = dict(builders or {})

    @classmethod
    def with_defaults(cls) -> ConnectorFactory:
        return cls(
            {
                BAJobsucheConnector.source_info.key: lambda _source: BAJobsucheConnector(),
                "greenhouse": _greenhouse_boards,
                "lever": _lever_boards,
                "ashby": _ashby_boards,
                "smartrecruiters": _smartrecruiters_boards,
                "personio": _personio_boards,
                "join": _join_boards,
                "softgarden": _softgarden_boards,
                "successfactors": _successfactors_boards,
                "workday": _workday_boards,
                "workable": _workable_boards,
                "recruitee": _recruitee_boards,
                "teamtailor": _teamtailor_boards,
                "icims": _icims_boards,
                "oracle": _oracle_boards,
                "phenom": _phenom_boards,
                "radancy": _radancy_boards,
                "beesite": _beesite_boards,
                AdzunaConnector.source_info.key: AdzunaConnector.from_source,
                ArbeitnowConnector.source_info.key: ArbeitnowConnector.from_source,
                JobicyConnector.source_info.key: lambda _source: JobicyConnector(),
                JoobleConnector.source_info.key: JoobleConnector.from_source,
                RemotiveConnector.source_info.key: lambda _source: RemotiveConnector(),
                InteramtConnector.source_info.key: lambda _source: InteramtConnector(),
                DeutscheBahnConnector.source_info.key: lambda _source: DeutscheBahnConnector(),
                RheinmetallConnector.source_info.key: lambda _source: RheinmetallConnector(),
                TKMSConnector.source_info.key: lambda _source: TKMSConnector(),
                FirecrawlJobsConnector.source_info.key: FirecrawlJobsConnector.from_source,
            }
        )

    def register(self, slug: str, builder: ConnectorBuilder) -> None:
        if not slug.strip():
            raise ValueError("source slug must not be empty")
        self._builders[slug] = builder

    def create(self, source: Source) -> JobConnector | None:
        builder = self._builders.get(source.slug)
        return builder(source) if builder is not None else None


@dataclass(frozen=True, slots=True)
class IngestResult:
    job_id: int
    created: bool
    changed: bool
    scored: bool


@dataclass(frozen=True, slots=True)
class SyncBatchResult:
    runs: tuple[CrawlRun, ...]
    blocked: bool = False
    lock_reason: str | None = None

    @property
    def created_count(self) -> int:
        return sum(run.created_count for run in self.runs)

    @property
    def updated_count(self) -> int:
        return sum(run.updated_count for run in self.runs)

    @property
    def error_count(self) -> int:
        return sum(run.error_count for run in self.runs)


class SourceNotFoundError(LookupError):
    pass


class _VisibleTextParser(HTMLParser):
    _BLOCK_TAGS = {
        "address",
        "article",
        "br",
        "div",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "li",
        "p",
        "section",
        "table",
        "tr",
    }
    _HIDDEN_TAGS = {"script", "style", "template"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._hidden_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag in self._HIDDEN_TAGS:
            self._hidden_depth += 1
        elif not self._hidden_depth:
            if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
                self.parts.append("\n\n## ")
            elif tag == "li":
                self.parts.append("- " if self.parts and self.parts[-1].endswith("\n") else "\n- ")
            elif tag in self._BLOCK_TAGS:
                self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._HIDDEN_TAGS and self._hidden_depth:
            self._hidden_depth -= 1
        elif tag in self._BLOCK_TAGS and not self._hidden_depth:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._hidden_depth:
            self.parts.append(data)


def ensure_default_sources(
    session: Session,
    *,
    settings: Settings | None = None,
    commit: bool = True,
) -> list[Source]:
    """Idempotently create/update the built-in source catalogue without fetching."""

    runtime = settings or get_settings()
    existing = {source.slug: source for source in session.scalars(select(Source))}
    result: list[Source] = []
    for definition in DEFAULT_SOURCES:
        source = existing.get(definition.slug)
        if source is None:
            source = Source(
                slug=definition.slug,
                name=definition.name,
                kind=definition.kind,
                base_url=definition.base_url,
                enabled=_initial_source_enabled(definition, runtime),
                is_official=definition.is_official,
                free_api=definition.free_api,
                status=definition.status,
                metadata_json=dict(definition.metadata),
            )
            session.add(source)
        else:
            # Preserve user activation and a custom self-hosted URL.
            source.name = definition.name
            source.kind = definition.kind
            source.is_official = definition.is_official
            source.free_api = definition.free_api
            source.base_url = source.base_url or definition.base_url
            source.metadata_json = {
                **dict(source.metadata_json or {}),
                **dict(definition.metadata),
            }
        if definition.slug == FirecrawlJobsConnector.source_info.key:
            metadata = dict(source.metadata_json or {})
            firecrawl = dict(
                metadata.get("firecrawl")
                if isinstance(metadata.get("firecrawl"), Mapping)
                else {}
            )
            portal_search = dict(
                firecrawl.get("portal_search")
                if isinstance(firecrawl.get("portal_search"), Mapping)
                else {}
            )
            portal_search.setdefault("enabled", True)
            portal_search["limit"] = 100
            portal_search["accept_external_results"] = True
            portal_search.pop("domain_batch_size", None)
            firecrawl["portal_search"] = portal_search
            metadata["firecrawl"] = firecrawl
            source.metadata_json = metadata
        result.append(source)

    if commit:
        session.commit()
        for source in result:
            session.refresh(source)
    else:
        session.flush()
    return result


def source_environment_enabled(
    source_slug: str, settings: Settings, *, default: bool = True
) -> bool:
    """Return whether the deployment environment enables a source connector."""

    if source_slug == BAJobsucheConnector.source_info.key:
        return settings.ba_jobs_enabled
    if source_slug == FirecrawlJobsConnector.source_info.key:
        return settings.firecrawl_enabled
    return default


def _initial_source_enabled(definition: SourceDefinition, settings: Settings) -> bool:
    """Apply environment defaults only when a source is first registered."""

    return source_environment_enabled(
        definition.slug, settings, default=definition.enabled
    )


def clean_description(value: str | None, description_format: str | None = None) -> str:
    """Return compact visible text while retaining the original in the snapshot blob."""

    if not value:
        return ""
    looks_like_html = (description_format or "").casefold() in {"html", "text/html"}
    looks_like_html = looks_like_html or bool(re.search(r"<[/!a-zA-Z][^>]*>", value))
    if looks_like_html:
        parser = _VisibleTextParser()
        try:
            parser.feed(value)
            value = "".join(parser.parts)
        except Exception:
            value = re.sub(r"<[^>]+>", " ", value)
    value = unescape(value).replace("\r", "").replace("\u00a0", " ")
    value = re.sub(r"[^\S\n]+", " ", value)
    value = re.sub(r" *\n *", "\n", value)
    return re.sub(r"\n{3,}", "\n\n", value).strip()


def compress_raw_record(record: RawJobRecord) -> tuple[bytes, str]:
    """Serialize and gzip the original normalized envelope deterministically."""

    envelope = {
        "description": record.description,
        "description_format": record.description_format,
        "metadata": _json_safe(record.metadata),
        "raw": _json_safe(record.raw),
    }
    raw_bytes = json.dumps(
        envelope,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return gzip.compress(raw_bytes, compresslevel=9, mtime=0), hashlib.sha256(raw_bytes).hexdigest()


def ingest_raw_job(
    session: Session,
    source: Source,
    record: RawJobRecord,
    *,
    candidate: CandidateProfile | None = None,
    preferences: PreferenceProfile | None = None,
    commit: bool = True,
) -> IngestResult:
    """Normalize, version, store, and locally score one connector record."""

    compressed_raw, raw_hash = compress_raw_record(record)
    cleaned_text = clean_description(record.description, record.description_format)
    location_text = _limit_text(
        " | ".join(item.strip() for item in record.locations if item.strip()) or None,
        1000,
    )
    postcode, city, state = _location_parts(record.locations, record.metadata)
    structured_data = {
        "apply_url": record.apply_url,
        "salary": record.salary,
        "provider_updated_at": _iso(record.updated_at),
        "source_key": record.source,
        "source_metadata": _json_safe(record.metadata),
        "raw_payload_hash": raw_hash,
        "raw_storage_format": "gzip-json-v1",
    }
    structured_data = {key: value for key, value in structured_data.items() if value is not None}
    values: dict[str, Any] = {
        "canonical_url": _limit_text(record.canonical_url or record.apply_url, 2000),
        "title": _limit_text(record.title.strip(), 500),
        "employer": _limit_text(_clean_optional(record.company), 500),
        "description_text": cleaned_text,
        "location_text": location_text,
        "postcode": _limit_text(postcode, 20),
        "city": _limit_text(city, 200),
        "state": _limit_text(state, 200),
        "country": _country_code(_metadata_text(record.metadata, "country", "land")),
        "remote_type": _remote_type(record),
        "employment_type": _limit_text(", ".join(record.employment_types) or None, 100),
        "contract_type": _limit_text(
            _metadata_text(record.metadata, "contract_type", "contractType", "befristung"),
            100,
        ),
        "language": _limit_text(_language(record, cleaned_text), 20),
        "published_at": record.published_at,
        "is_active": True,
        "availability_status": JobAvailabilityStatus.ACTIVE.value,
        "availability_checked_at": utcnow(),
        "availability_check_method": "source_sync",
        "availability_reason": "present_in_source",
        "availability_failures": 0,
        "structured_data": structured_data,
    }
    job, created, changed = jobs.upsert_job(
        session,
        source_id=source.id,
        external_id=record.source_job_id,
        values=values,
        raw_html_compressed=compressed_raw,
        commit=False,
    )

    latest_score = session.scalar(
        select(JobScore).where(JobScore.job_id == job.id).order_by(JobScore.id.desc()).limit(1)
    )
    if candidate is not None and not candidate.is_confirmed:
        candidate = None
    if candidate is None:
        candidate = session.scalar(
            select(CandidateProfile)
            .where(CandidateProfile.is_confirmed.is_(True))
            .order_by(CandidateProfile.updated_at.desc(), CandidateProfile.id.desc())
            .limit(1)
        )
    if preferences is None:
        if candidate is not None and candidate.preferences is not None:
            preferences = candidate.preferences
        else:
            preferences = session.scalar(
                select(PreferenceProfile).order_by(PreferenceProfile.updated_at.desc()).limit(1)
            )

    needs_score = changed or latest_score is None
    if latest_score is not None:
        needs_score = needs_score or latest_score.profile_version != (
            candidate.version if candidate else None
        )
        needs_score = needs_score or latest_score.preference_version != (
            preferences.version if preferences else None
        )
    if needs_score:
        if candidate is not None and preferences is not None:
            score_job_with_learning(
                session,
                job,
                candidate=candidate,
                preferences=preferences,
                commit=False,
            )
        else:
            jobs.score_job(
                session,
                job,
                candidate=candidate,
                preferences=preferences,
                commit=False,
            )

    if commit:
        session.commit()
        session.refresh(job)
    else:
        session.flush()
    return IngestResult(job_id=job.id, created=created, changed=changed, scored=needs_score)


def run_source(
    session: Session,
    source: Source,
    connector: JobConnector,
    *,
    settings: Settings | None = None,
    query_plan: SyncQueryPlan | None = None,
    run_type: str = "scheduled",
    clock: Callable[[], datetime] = utcnow,
) -> CrawlRun:
    """Run one connector without permitting callers to bypass source permissions."""

    runtime_settings = settings or get_settings()
    lock_reason = _crawl_lock_reason(session, runtime_settings)
    if lock_reason is None:
        lock_reason = _source_lock_reason(session, source, runtime_settings)
    if lock_reason is not None:
        return _record_blocked(session, source, run_type, clock, lock_reason)

    return _run_authorized_source(
        session,
        source,
        connector,
        query_plan=query_plan,
        run_type=run_type,
        clock=clock,
    )


def _run_authorized_source(
    session: Session,
    source: Source,
    connector: JobConnector,
    *,
    query_plan: SyncQueryPlan | None = None,
    run_type: str = "scheduled",
    clock: Callable[[], datetime] = utcnow,
) -> CrawlRun:
    """Execute a connector after ``run_all_enabled`` or ``run_source`` authorized it."""

    query_plan = query_plan or SyncQueryPlan()
    run = CrawlRun(
        source_id=source.id,
        run_type=run_type,
        status=CrawlRunStatus.RUNNING.value,
        started_at=clock(),
    )
    source.status = "running"
    source.last_error = None
    session.add(run)
    session.commit()
    session.refresh(run)

    discovered = fetched = created = updated = skipped = errors = 0
    seen_external_ids: set[str] = set()
    error_messages: list[str] = []
    queries = query_plan.queries_for(source)
    try:
        for query in queries:
            for record in connector.iter_jobs(query):
                discovered += 1
                record_key = record.source_job_id.strip()
                if record_key in seen_external_ids:
                    skipped += 1
                    continue
                seen_external_ids.add(record_key)
                try:
                    with session.begin_nested():
                        outcome = ingest_raw_job(session, source, record, commit=False)
                    fetched += 1
                    if outcome.created:
                        created += 1
                    elif outcome.changed:
                        updated += 1
                    else:
                        skipped += 1
                except Exception as exc:  # one malformed provider record must not stop the run
                    errors += 1
                    error_messages.append(_safe_error(exc))
    except Exception as exc:
        errors += 1
        error_messages.append(_safe_error(exc))
    finally:
        close = getattr(connector, "close", None)
        if callable(close):
            try:
                close()
            except Exception as exc:
                errors += 1
                error_messages.append(f"Connector-Abschluss: {_safe_error(exc)}")

    diagnostics = getattr(connector, "run_diagnostics", None)
    if callable(diagnostics):
        diagnostics = diagnostics()
    if isinstance(diagnostics, Mapping):
        try:
            target_error_count = max(0, int(diagnostics.get("target_error_count", 0)))
            page_error_count = max(0, int(diagnostics.get("page_error_count", 0)))
            diagnostic_errors = target_error_count + page_error_count
        except (TypeError, ValueError):
            diagnostic_errors = 0
        if diagnostic_errors:
            errors += diagnostic_errors
            target_errors = diagnostics.get("target_errors")
            if isinstance(target_errors, list):
                for item in target_errors[:5]:
                    if isinstance(item, Mapping):
                        error_messages.append(str(item.get("error") or "Connector-Zielfehler"))
            page_errors = diagnostics.get("page_errors")
            if isinstance(page_errors, list):
                for item in page_errors[:5]:
                    if isinstance(item, Mapping):
                        error_messages.append(str(item.get("error") or "Seitennormalisierung"))

    finished_at = clock()
    successful = errors == 0
    partial = bool(errors and fetched)
    if successful:
        run.status = CrawlRunStatus.SUCCEEDED.value
    elif partial:
        run.status = CrawlRunStatus.PARTIAL.value
    else:
        run.status = CrawlRunStatus.FAILED.value
    run.finished_at = finished_at
    run.discovered_count = discovered
    run.fetched_count = fetched
    run.created_count = created
    run.updated_count = updated
    run.skipped_count = skipped
    run.error_count = errors
    run.error_message = " | ".join(error_messages[:5])[:4000] or None
    run.details = {
        "query_count": len(queries),
        "unique_record_count": len(seen_external_ids),
        "partial_success": partial,
        "coverage": {
            "preferred_locations": list(
                query_plan.preferred_locations or (query_plan.bw_location,)
            ),
            "germany_remote_best_effort": query_plan.include_germany_remote,
        },
    }
    if isinstance(diagnostics, Mapping):
        run.details["connector"] = _json_safe(diagnostics)
        metadata_diagnostics = _json_safe(diagnostics)
    else:
        metadata_diagnostics = None
    prepared_only = bool(
        isinstance(diagnostics, Mapping)
        and diagnostics.get("skipped")
        and diagnostics.get("skip_reason") == "no_targets"
    )
    if successful and prepared_only:
        source.status = "prepared"
    elif partial:
        source.status = "limited"
    else:
        source.status = "healthy" if successful else "error"
    source.last_error = run.error_message
    if (successful or partial) and not prepared_only:
        source.last_success_at = finished_at
    metadata = dict(source.metadata_json or {})
    metadata["last_counts"] = {
        "discovered": discovered,
        "fetched": fetched,
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "errors": errors,
    }
    if metadata_diagnostics is not None:
        metadata["last_connector_diagnostics"] = metadata_diagnostics
    if source.slug == BAJobsucheConnector.source_info.key:
        run.details["coverage"] = {
            "preferred_locations": list(
                query_plan.preferred_locations or (query_plan.bw_location,)
            ),
            "search_terms": [term for term in query_plan.search_terms if term],
            "germany_homeoffice_best_effort": query_plan.include_germany_remote,
        }
    elif source.slug == FirecrawlJobsConnector.source_info.key:
        run.details["coverage"] = {
            "selection": "configured_target_pages",
            "configured_targets": (
                diagnostics.get("target_count", 0) if isinstance(diagnostics, Mapping) else 0
            ),
        }
    else:
        run.details["coverage"] = {"selection": "source_defined"}
    source.metadata_json = metadata
    session.commit()
    session.refresh(run)
    return run


def run_all_enabled(
    session: Session,
    *,
    settings: Settings | None = None,
    connector_factory: ConnectorFactoryLike | Callable[[Source], JobConnector | None] | None = None,
    query_plan: SyncQueryPlan | None = None,
    run_type: str = "scheduled",
    exclude_source_slugs: Collection[str] = (),
    clock: Callable[[], datetime] = utcnow,
) -> SyncBatchResult:
    """Run enabled sources after checking API and source-specific permissions."""

    runtime_settings = settings or get_settings()
    ensure_default_sources(session, settings=runtime_settings)

    lock_reason = _crawl_lock_reason(session, runtime_settings)
    if lock_reason is not None:
        blocked = CrawlRun(
            source_id=None,
            run_type=run_type,
            status=CrawlRunStatus.BLOCKED.value,
            started_at=clock(),
            finished_at=clock(),
            lock_reason=lock_reason,
            details={"network_called": False},
        )
        session.add(blocked)
        session.commit()
        session.refresh(blocked)
        return SyncBatchResult(runs=(blocked,), blocked=True, lock_reason=lock_reason)

    # Construct the default factory only after the hard deployment and UI gates.
    factory = connector_factory or ConnectorFactory.with_defaults()
    excluded = {slug.strip() for slug in exclude_source_slugs if slug.strip()}
    source_query = select(Source).where(Source.enabled.is_(True))
    if excluded:
        source_query = source_query.where(Source.slug.not_in(excluded))
    sources = list(session.scalars(source_query.order_by(Source.slug)))
    runs: list[CrawlRun] = []
    for source in sources:
        source_lock_reason = _source_lock_reason(session, source, runtime_settings)
        if source_lock_reason is not None:
            runs.append(_record_blocked(session, source, run_type, clock, source_lock_reason))
            continue
        try:
            connector = _create_connector(factory, source)
        except Exception as exc:
            runs.append(_record_unavailable(session, source, run_type, clock, _safe_error(exc)))
            continue
        if connector is None:
            runs.append(
                _record_unavailable(
                    session,
                    source,
                    run_type,
                    clock,
                    "Fuer diese aktivierte Quelle ist kein Connector konfiguriert.",
                )
            )
            continue
        runs.append(
            _run_authorized_source(
                session,
                source,
                connector,
                query_plan=query_plan,
                run_type=run_type,
                clock=clock,
            )
        )
    return SyncBatchResult(runs=tuple(runs))


def run_source_by_slug(
    session: Session,
    slug: str,
    *,
    settings: Settings | None = None,
    connector_factory: ConnectorFactoryLike | Callable[[Source], JobConnector | None] | None = None,
    query_plan: SyncQueryPlan | None = None,
    run_type: str = "manual",
    clock: Callable[[], datetime] = utcnow,
) -> CrawlRun:
    """Safely run one configured source for a CLI or manual UI action."""

    runtime_settings = settings or get_settings()
    ensure_default_sources(session, settings=runtime_settings)
    source = session.scalar(select(Source).where(Source.slug == slug.strip()))
    if source is None:
        raise SourceNotFoundError(f"Unknown source: {slug}")
    lock_reason = _crawl_lock_reason(session, runtime_settings)
    if lock_reason is not None:
        return _record_blocked(session, source, run_type, clock, lock_reason)
    if not source.enabled:
        return _record_blocked(
            session,
            source,
            run_type,
            clock,
            "Die ausgewaehlte Quelle ist deaktiviert.",
        )

    source_lock_reason = _source_lock_reason(session, source, runtime_settings)
    if source_lock_reason is not None:
        return _record_blocked(
            session,
            source,
            run_type,
            clock,
            source_lock_reason,
        )

    factory = connector_factory or ConnectorFactory.with_defaults()
    try:
        connector = _create_connector(factory, source)
    except Exception as exc:
        return _record_unavailable(session, source, run_type, clock, _safe_error(exc))
    if connector is None:
        return _record_unavailable(
            session,
            source,
            run_type,
            clock,
            "Fuer diese aktivierte Quelle ist kein Connector konfiguriert.",
        )
    return _run_authorized_source(
        session,
        source,
        connector,
        query_plan=query_plan,
        run_type=run_type,
        clock=clock,
    )


def _crawl_lock_reason(session: Session, settings: Settings) -> str | None:
    if not settings.crawling_enabled:
        return "Serverfreigabe CRAWLING_ENABLED ist deaktiviert."
    if not database_crawling_switch(session):
        return "Der Anwendungsschalter fuer API- und Feed-Abrufe ist deaktiviert."
    return None


def _source_lock_reason(session: Session, source: Source, settings: Settings) -> str | None:
    setting_names = {
        BAJobsucheConnector.source_info.key: "BA_JOBS_ENABLED",
        FirecrawlJobsConnector.source_info.key: "FIRECRAWL_ENABLED",
    }
    if not source_environment_enabled(source.slug, settings):
        return f"Serverfreigabe {setting_names[source.slug]} ist deaktiviert."
    if (
        source.slug == FirecrawlJobsConnector.source_info.key
        and not database_firecrawl_switch(session)
    ):
        return "Die separate Firecrawl-Freigabe in den Einstellungen ist deaktiviert."
    return None


def _create_connector(
    factory: ConnectorFactoryLike | Callable[[Source], JobConnector | None],
    source: Source,
) -> JobConnector | None:
    create = getattr(factory, "create", None)
    if callable(create):
        return create(source)
    if callable(factory):
        return factory(source)
    raise TypeError("connector_factory must be callable or expose create(source)")


def _record_unavailable(
    session: Session,
    source: Source,
    run_type: str,
    clock: Callable[[], datetime],
    message: str,
) -> CrawlRun:
    now = clock()
    run = CrawlRun(
        source_id=source.id,
        run_type=run_type,
        status=CrawlRunStatus.FAILED.value,
        started_at=now,
        finished_at=now,
        error_count=1,
        error_message=message[:4000],
        details={"connector_available": False, "network_called": False},
    )
    source.status = "not_configured"
    source.last_error = message[:4000]
    session.add(run)
    session.commit()
    session.refresh(run)
    return run


def _record_blocked(
    session: Session,
    source: Source,
    run_type: str,
    clock: Callable[[], datetime],
    reason: str,
) -> CrawlRun:
    now = clock()
    blocked = CrawlRun(
        source_id=source.id,
        run_type=run_type,
        status=CrawlRunStatus.BLOCKED.value,
        started_at=now,
        finished_at=now,
        lock_reason=reason,
        details={"network_called": False},
    )
    session.add(blocked)
    session.commit()
    session.refresh(blocked)
    return blocked


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        return _iso(value)
    if isinstance(value, bytes):
        return {"type": "bytes", "sha256": hashlib.sha256(value).hexdigest()}
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, set):
        converted = [_json_safe(item) for item in value]
        return sorted(
            converted,
            key=lambda item: json.dumps(item, sort_keys=True, ensure_ascii=False),
        )
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return str(value)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _clean_optional(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = re.sub(r"\s+", " ", value).strip()
    return cleaned or None


def _limit_text(value: str | None, maximum: int) -> str | None:
    return value[:maximum] if value is not None else None


def _country_code(value: str | None) -> str:
    if not value:
        return "DE"
    normalized = value.casefold().strip().replace(".", "")
    aliases = {
        "deutschland": "DE",
        "germany": "DE",
        "österreich": "AT",
        "oesterreich": "AT",
        "austria": "AT",
        "schweiz": "CH",
        "switzerland": "CH",
        "frankreich": "FR",
        "france": "FR",
        "niederlande": "NL",
        "netherlands": "NL",
        "belgien": "BE",
        "belgium": "BE",
        "luxemburg": "LU",
        "luxembourg": "LU",
        "polen": "PL",
        "poland": "PL",
        "vereinigtes königreich": "GB",
        "united kingdom": "GB",
        "uk": "GB",
        "united states": "US",
        "usa": "US",
    }
    if normalized in aliases:
        return aliases[normalized]
    if len(normalized) == 2 and normalized.isalpha():
        return normalized.upper()
    return "ZZ"


def _metadata_text(metadata: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = metadata.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _location_parts(
    locations: tuple[str, ...], metadata: Mapping[str, Any]
) -> tuple[str | None, str | None, str | None]:
    rendered = locations[0].strip() if locations else ""
    postcode = _metadata_text(metadata, "postcode", "postal_code", "plz")
    if postcode is None:
        match = re.search(r"\b\d{5}\b", rendered)
        postcode = match.group(0) if match else None

    city = _metadata_text(metadata, "city", "ort")
    if city is None and rendered:
        city_candidate = re.sub(r"\b\d{5}\b", "", rendered).split(",", 1)[0]
        city_candidate = re.sub(
            r"(?i)\b(remote|homeoffice|home-office|deutschland|germany)\b", "", city_candidate
        )
        city = _clean_optional(city_candidate.strip(" /|-"))

    state = _metadata_text(metadata, "state", "region", "bundesland")
    joined = " ".join(locations).casefold()
    normalized = joined.replace("ü", "ue").replace("-", " ")
    if state is None and "baden wuerttemberg" in normalized:
        state = "Baden-Württemberg"
    return postcode, city, state


def _remote_type(record: RawJobRecord) -> str | None:
    explicit = _metadata_text(
        record.metadata, "remote_type", "workplace_type", "workplaceType", "arbeitsmodell"
    )
    explicit_normalized = (explicit or "").casefold()
    if "hybrid" in explicit_normalized:
        return "hybrid"
    if explicit_normalized in {"remote", "fully remote", "full remote", "100% remote"}:
        return "remote"
    if any(token in explicit_normalized for token in ("onsite", "on-site", "praesenz")):
        return "onsite"
    if record.remote is False:
        return "onsite"
    if record.remote is True:
        locations = " ".join(record.locations).casefold()
        without_remote = re.sub(
            r"(?i)\b(remote|homeoffice|home-office|deutschland|germany|bundesweit)\b",
            "",
            locations,
        )
        return "hybrid" if re.search(r"[a-zA-ZäöüÄÖÜ]{3,}", without_remote) else "remote"
    return None


def _language(record: RawJobRecord, cleaned_text: str) -> str | None:
    explicit = _metadata_text(record.metadata, "language", "sprache")
    if explicit:
        return explicit
    text = f"{record.title} {cleaned_text}".casefold()
    german_hits = sum(text.count(word) for word in (" der ", " die ", " und ", " sie ", "für"))
    english_hits = sum(text.count(word) for word in (" the ", " and ", " you ", " with ", " for "))
    if german_hits == english_hits == 0:
        return None
    return "de" if german_hits >= english_hits else "en"


def _safe_error(exc: Exception) -> str:
    message = re.sub(r"\s+", " ", str(exc)).strip()
    return f"{type(exc).__name__}: {message}"[:1000]
