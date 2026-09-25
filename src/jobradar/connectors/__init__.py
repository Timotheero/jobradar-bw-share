"""Source connectors.

Importing this package only defines classes and constants. It never performs a
network request; every fetch or crawl must be invoked explicitly by the caller.
"""

from .adzuna import AdzunaConnector
from .arbeitnow import ArbeitnowConnector
from .ashby import AshbyJobBoardConnector
from .ba_jobsuche import BAJobsucheConnector
from .base import (
    JobConnector,
    JobQuery,
    PaginationOptions,
    RawJobRecord,
    SourceInfo,
)
from .beesite import BeeSiteConnector
from .configured_boards import BoardTarget, ConfiguredJobBoardsConnector
from .deutsche_bahn import DeutscheBahnConnector
from .firecrawl import FirecrawlDocument, FirecrawlJobStatus, FirecrawlV2Client
from .greenhouse import GreenhouseJobBoardConnector
from .http import HTTPTransport, RetryPolicy, TimeoutOptions
from .icims import ICIMSJobBoardConnector
from .interamt import InteramtConnector
from .jobicy import JobicyConnector, JobicyFeedQuery
from .join import JoinConnector
from .jooble import JoobleConnector
from .lever import LeverJobBoardConnector
from .oracle import OracleRecruitingCloudConnector
from .personio import PersonioJobFeedConnector
from .phenom import PhenomConnector
from .radancy import RadancyConnector
from .recruitee import RecruiteeJobBoardConnector
from .remotive import RemotiveConnector
from .rheinmetall import RheinmetallConnector
from .smartrecruiters import SmartRecruitersJobBoardConnector
from .softgarden import SoftgardenConnector
from .successfactors import SuccessFactorsJobBoardConnector
from .teamtailor import TeamtailorJobFeedConnector
from .tkms import TKMSConnector
from .workable import WorkableJobBoardConnector
from .workday import WorkdayJobBoardConnector

__all__ = [
    "AdzunaConnector",
    "ArbeitnowConnector",
    "AshbyJobBoardConnector",
    "BAJobsucheConnector",
    "BeeSiteConnector",
    "BoardTarget",
    "ConfiguredJobBoardsConnector",
    "DeutscheBahnConnector",
    "FirecrawlDocument",
    "FirecrawlJobStatus",
    "FirecrawlV2Client",
    "GreenhouseJobBoardConnector",
    "HTTPTransport",
    "ICIMSJobBoardConnector",
    "InteramtConnector",
    "JobConnector",
    "JobQuery",
    "JobicyConnector",
    "JobicyFeedQuery",
    "JoobleConnector",
    "LeverJobBoardConnector",
    "JoinConnector",
    "PersonioJobFeedConnector",
    "OracleRecruitingCloudConnector",
    "PhenomConnector",
    "PaginationOptions",
    "RawJobRecord",
    "RemotiveConnector",
    "RadancyConnector",
    "RecruiteeJobBoardConnector",
    "RheinmetallConnector",
    "RetryPolicy",
    "SmartRecruitersJobBoardConnector",
    "SoftgardenConnector",
    "SuccessFactorsJobBoardConnector",
    "TeamtailorJobFeedConnector",
    "TKMSConnector",
    "SourceInfo",
    "TimeoutOptions",
    "WorkableJobBoardConnector",
    "WorkdayJobBoardConnector",
]
