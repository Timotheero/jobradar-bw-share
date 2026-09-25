"""Small httpx-based transport with explicit timeout and retry policies."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urljoin

import httpx


@dataclass(frozen=True, slots=True)
class TimeoutOptions:
    connect: float = 5.0
    read: float = 20.0
    write: float = 20.0
    pool: float = 5.0

    def as_httpx(self) -> httpx.Timeout:
        return httpx.Timeout(
            connect=self.connect,
            read=self.read,
            write=self.write,
            pool=self.pool,
        )


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Retries only idempotent methods unless a call explicitly opts in."""

    max_attempts: int = 3
    backoff_seconds: float = 0.25
    max_backoff_seconds: float = 5.0
    retry_statuses: frozenset[int] = field(
        default_factory=lambda: frozenset({408, 425, 429, 500, 502, 503, 504})
    )
    retry_methods: frozenset[str] = field(
        default_factory=lambda: frozenset({"GET", "HEAD", "OPTIONS"})
    )

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if self.backoff_seconds < 0 or self.max_backoff_seconds < 0:
            raise ValueError("backoff values must be non-negative")


class HTTPTransport:
    """HTTP boundary shared by connectors and injectable in unit tests."""

    def __init__(
        self,
        base_url: str,
        *,
        client: httpx.Client | None = None,
        timeout: TimeoutOptions | None = None,
        retry: RetryPolicy | None = None,
        default_headers: Mapping[str, str] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.base_url = base_url.rstrip("/") + "/"
        self.timeout = timeout or TimeoutOptions()
        self.retry = retry or RetryPolicy()
        self.default_headers = dict(default_headers or {})
        self._sleeper = sleeper
        self._owns_client = client is None
        self._client = client or httpx.Client(follow_redirects=True)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> HTTPTransport:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def request(
        self,
        method: str,
        path_or_url: str,
        *,
        params: Mapping[str, Any] | None = None,
        data: Mapping[str, Any] | None = None,
        json: Any = None,
        headers: Mapping[str, str] | None = None,
        retryable: bool | None = None,
    ) -> httpx.Response:
        method = method.upper()
        url = self._url(path_or_url)
        merged_headers = {**self.default_headers, **dict(headers or {})}
        may_retry = method in self.retry.retry_methods if retryable is None else retryable
        last_error: httpx.TransportError | None = None

        for attempt in range(1, self.retry.max_attempts + 1):
            try:
                response = self._client.request(
                    method,
                    url,
                    params=params,
                    data=data,
                    json=json,
                    headers=merged_headers,
                    timeout=self.timeout.as_httpx(),
                )
            except httpx.TransportError as exc:
                last_error = exc
                if not may_retry or attempt == self.retry.max_attempts:
                    raise
                self._sleeper(self._backoff(attempt, None))
                continue

            if (
                may_retry
                and response.status_code in self.retry.retry_statuses
                and attempt < self.retry.max_attempts
            ):
                delay = self._backoff(attempt, response.headers.get("Retry-After"))
                response.close()
                self._sleeper(delay)
                continue

            response.raise_for_status()
            return response

        assert last_error is not None
        raise last_error

    def request_json(self, method: str, path_or_url: str, **kwargs: Any) -> Any:
        return self.request(method, path_or_url, **kwargs).json()

    def _url(self, path_or_url: str) -> str:
        if path_or_url.startswith(("https://", "http://")):
            return path_or_url
        return urljoin(self.base_url, path_or_url.lstrip("/"))

    def _backoff(self, attempt: int, retry_after: str | None) -> float:
        parsed_retry_after = _retry_after_seconds(retry_after)
        if parsed_retry_after is not None:
            return min(parsed_retry_after, self.retry.max_backoff_seconds)
        exponential = self.retry.backoff_seconds * (2 ** (attempt - 1))
        return min(exponential, self.retry.max_backoff_seconds)


def _retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
        now = time.time()
        return max(0.0, retry_at.timestamp() - now)
