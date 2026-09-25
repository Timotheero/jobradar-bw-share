from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from jobradar.connectors.public_url import (  # noqa: E402
    UnsafePublicURLError,
    prepare_public_url,
    validate_public_url,
)


@pytest.mark.parametrize(
    "url",
    (
        "http://127.0.0.1/jobs",
        "http://[::1]/jobs",
        "http://169.254.169.254/latest/meta-data",
        "http://10.0.0.4/jobs",
        "http://user:secret@example.org/jobs",
        "https://example.org:8443/jobs",
        "http://service.local/jobs",
    ),
)
def test_public_url_guard_rejects_non_public_targets(url: str) -> None:
    with pytest.raises(UnsafePublicURLError):
        validate_public_url(url, resolve_dns=False)


def test_public_url_guard_rejects_hostname_when_any_dns_answer_is_private() -> None:
    def resolver(hostname: str, port: int) -> tuple[str, ...]:
        assert (hostname, port) == ("jobs.example.org", 443)
        return ("93.184.216.34", "10.0.0.8")

    with pytest.raises(UnsafePublicURLError, match="nicht öffentliche Adresse"):
        validate_public_url("https://jobs.example.org/openings", resolver=resolver)


def test_redirects_are_validated_before_the_final_url_is_returned() -> None:
    calls: list[str] = []

    def resolver(hostname: str, port: int) -> tuple[str, ...]:
        assert port == 443
        return ("93.184.216.34",)

    def probe(url: str) -> tuple[int, str | None]:
        calls.append(url)
        if url == "https://example.org/careers":
            return 302, "https://jobs.example.org/openings"
        return 200, None

    prepared = prepare_public_url(
        "https://example.org/careers", resolver=resolver, probe=probe
    )

    assert prepared.final_url == "https://jobs.example.org/openings"
    assert prepared.redirect_count == 1
    assert calls == ["https://example.org/careers", "https://jobs.example.org/openings"]


def test_redirect_to_private_address_is_rejected_without_probing_it() -> None:
    calls: list[str] = []

    def probe(url: str) -> tuple[int, str | None]:
        calls.append(url)
        return 302, "http://127.0.0.1:5432/"

    with pytest.raises(UnsafePublicURLError):
        prepare_public_url(
            "https://example.org/jobs",
            resolver=lambda _hostname, _port: ("93.184.216.34",),
            probe=probe,
        )

    assert calls == ["https://example.org/jobs"]
