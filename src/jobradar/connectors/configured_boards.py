"""Coordinate explicitly configured public employer job boards.

A configured provider source may contain several employer board identifiers. Each
board stays isolated so one unavailable employer cannot abort the other boards,
and external identifiers are namespaced because provider IDs are not guaranteed
to be globally unique across tenants.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from .base import JobConnector, JobQuery, RawJobRecord, SourceInfo


@dataclass(frozen=True, slots=True)
class BoardTarget:
    identifier: str
    company: str | None = None
    options: Mapping[str, Any] = field(default_factory=dict)


BoardConnectorBuilder = Callable[[BoardTarget], JobConnector]


class ConfiguredJobBoardsConnector:
    """Yield jobs from configured employer boards with per-board diagnostics."""

    def __init__(
        self,
        *,
        source_key: str,
        source_name: str,
        documentation_url: str,
        targets: tuple[BoardTarget, ...],
        builder: BoardConnectorBuilder,
    ) -> None:
        self.source_info = SourceInfo(
            key=source_key,
            name=source_name,
            acquisition="official_public_api",
            official=True,
            documentation_url=documentation_url,
        )
        self.targets = targets
        self._builder = builder
        self._diagnostics = {
            "target_count": len(targets),
            "targets_started": 0,
            "targets_completed": 0,
            "records_emitted": 0,
            "target_error_count": 0,
            "target_errors": [],
            "skipped": not targets,
            "skip_reason": "no_targets" if not targets else None,
        }

    @property
    def run_diagnostics(self) -> Mapping[str, Any]:
        return self._diagnostics

    def iter_jobs(self, query: JobQuery | None = None) -> Iterator[RawJobRecord]:
        for target in self.targets:
            self._diagnostics["targets_started"] += 1
            connector: JobConnector | None = None
            try:
                connector = self._builder(target)
                for record in connector.iter_jobs(query):
                    self._diagnostics["records_emitted"] += 1
                    yield replace(
                        record,
                        source_job_id=_namespaced_identifier(
                            target.identifier, record.source_job_id
                        ),
                    )
                self._diagnostics["targets_completed"] += 1
            except Exception as exc:
                self._diagnostics["target_error_count"] += 1
                self._diagnostics["target_errors"].append(
                    {
                        "target": target.identifier,
                        "error": f"{type(exc).__name__}: {exc}"[:1000],
                    }
                )
            finally:
                if connector is not None:
                    close = getattr(connector, "close", None)
                    if callable(close):
                        try:
                            close()
                        except Exception as exc:
                            self._diagnostics["target_error_count"] += 1
                            self._diagnostics["target_errors"].append(
                                {
                                    "target": target.identifier,
                                    "error": f"Connector-Abschluss: {type(exc).__name__}: {exc}"[
                                        :1000
                                    ],
                                }
                            )

    def close(self) -> None:
        """Child connectors are closed immediately after their board is read."""


def board_targets_from_metadata(metadata: Mapping[str, Any] | None) -> tuple[BoardTarget, ...]:
    raw_targets = metadata.get("boards", ()) if isinstance(metadata, Mapping) else ()
    if isinstance(raw_targets, (str, Mapping)):
        raw_targets = (raw_targets,)
    if not isinstance(raw_targets, (list, tuple)):
        return ()

    targets: list[BoardTarget] = []
    seen: set[str] = set()
    for raw in raw_targets:
        if isinstance(raw, str):
            identifier = raw.strip()
            company = None
            options: Mapping[str, Any] = {}
        elif isinstance(raw, Mapping):
            identifier = str(
                raw.get("identifier")
                or raw.get("board")
                or raw.get("site")
                or raw.get("account")
                or ""
            ).strip()
            company_value = raw.get("company")
            company = str(company_value).strip() if company_value else None
            options = {
                str(key): value
                for key, value in raw.items()
                if key not in {"identifier", "board", "site", "account", "company"}
            }
        else:
            continue
        deduplication_key = identifier.casefold()
        if not identifier or deduplication_key in seen:
            continue
        seen.add(deduplication_key)
        targets.append(BoardTarget(identifier=identifier, company=company, options=options))
    return tuple(targets)


def _namespaced_identifier(board_identifier: str, provider_identifier: str) -> str:
    combined = f"{board_identifier}:{provider_identifier}"
    if len(combined) <= 300:
        return combined
    digest = hashlib.sha256(combined.encode("utf-8")).hexdigest()
    return f"{board_identifier[:200]}:{digest}"
