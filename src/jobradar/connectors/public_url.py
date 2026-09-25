"""Validation for user-configured public HTTP targets.

The guard rejects addresses that could reach the Jobradar host, its Docker
network, or another non-public service. Redirects are followed manually so
every destination is checked before a request is sent to it.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

Resolver = Callable[[str, int], Iterable[str]]
RedirectProbe = Callable[[str], tuple[int, str | None]]

_ALLOWED_SCHEMES = frozenset({"http", "https"})
_ALLOWED_PORTS = frozenset({80, 443})
_BLOCKED_HOST_SUFFIXES = (".internal", ".lan", ".local", ".localhost")
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


class UnsafePublicURLError(ValueError):
    """Raised when a URL could address something other than the public web."""


@dataclass(frozen=True, slots=True)
class PreparedPublicURL:
    original_url: str
    final_url: str
    redirect_count: int


def validate_public_url(
    value: str,
    *,
    resolve_dns: bool = True,
    resolver: Resolver | None = None,
) -> str:
    """Return a normalized public HTTP(S) URL or raise ``UnsafePublicURLError``."""

    raw = value.strip()
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise UnsafePublicURLError("Die URL enthält einen ungültigen Port.") from exc
    if parsed.scheme.casefold() not in _ALLOWED_SCHEMES or not parsed.hostname:
        raise UnsafePublicURLError("Nur vollständige öffentliche HTTP(S)-URLs sind erlaubt.")
    if parsed.username is not None or parsed.password is not None:
        raise UnsafePublicURLError("Zugangsdaten dürfen nicht Teil einer Ziel-URL sein.")
    effective_port = port or (443 if parsed.scheme.casefold() == "https" else 80)
    if effective_port not in _ALLOWED_PORTS:
        raise UnsafePublicURLError("Für Website-Ziele sind nur die Ports 80 und 443 erlaubt.")

    hostname = parsed.hostname.rstrip(".").casefold()
    if hostname == "localhost" or hostname.endswith(_BLOCKED_HOST_SUFFIXES):
        raise UnsafePublicURLError("Lokale oder interne Hostnamen sind nicht erlaubt.")
    try:
        ascii_hostname = hostname.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise UnsafePublicURLError("Der Hostname der Ziel-URL ist ungültig.") from exc

    literal = _parse_ip_literal(ascii_hostname)
    if literal is not None:
        _require_global_address(literal)
    elif resolve_dns:
        addresses = tuple((resolver or _resolve_addresses)(ascii_hostname, effective_port))
        if not addresses:
            raise UnsafePublicURLError("Der Hostname des Ziels konnte nicht aufgelöst werden.")
        for address in addresses:
            try:
                parsed_address = ipaddress.ip_address(address)
            except ValueError as exc:
                raise UnsafePublicURLError("Die DNS-Antwort für das Ziel ist ungültig.") from exc
            _require_global_address(parsed_address)

    normalized_netloc = ascii_hostname
    if ":" in ascii_hostname:
        normalized_netloc = f"[{ascii_hostname}]"
    if port is not None:
        normalized_netloc = f"{normalized_netloc}:{port}"
    return urlunsplit(
        (
            parsed.scheme.casefold(),
            normalized_netloc,
            parsed.path or "/",
            parsed.query,
            "",
        )
    )


def prepare_public_url(
    value: str,
    *,
    resolver: Resolver | None = None,
    probe: RedirectProbe | None = None,
    max_redirects: int = 5,
) -> PreparedPublicURL:
    """Validate a target and every redirect before returning the final public URL."""

    if max_redirects < 0:
        raise ValueError("max_redirects must not be negative")
    original = validate_public_url(value, resolver=resolver)
    current = original
    seen = {current}
    redirect_count = 0
    owned_client: httpx.Client | None = None
    if probe is None:
        owned_client = httpx.Client(timeout=httpx.Timeout(10.0), follow_redirects=False)

        def run_probe(url: str) -> tuple[int, str | None]:
            assert owned_client is not None
            return _probe_redirect(owned_client, url)
    else:
        run_probe = probe
    try:
        while True:
            status_code, location = run_probe(current)
            if status_code not in _REDIRECT_STATUSES or not location:
                return PreparedPublicURL(original, current, redirect_count)
            if redirect_count >= max_redirects:
                raise UnsafePublicURLError("Das Ziel verwendet zu viele Weiterleitungen.")
            candidate = validate_public_url(urljoin(current, location), resolver=resolver)
            if candidate in seen:
                raise UnsafePublicURLError("Das Ziel verwendet eine Weiterleitungsschleife.")
            seen.add(candidate)
            current = candidate
            redirect_count += 1
    except httpx.HTTPError as exc:
        raise UnsafePublicURLError(f"Das Ziel konnte nicht sicher geprüft werden: {exc}") from exc
    finally:
        if owned_client is not None:
            owned_client.close()


def _parse_ip_literal(
    hostname: str,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(hostname)
    except ValueError:
        return None


def _require_global_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> None:
    if not address.is_global:
        raise UnsafePublicURLError(
            "Das Ziel verweist auf eine lokale oder nicht öffentliche Adresse."
        )


def _resolve_addresses(hostname: str, port: int) -> tuple[str, ...]:
    try:
        records = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise UnsafePublicURLError("Der Hostname des Ziels konnte nicht aufgelöst werden.") from exc
    return tuple(dict.fromkeys(str(record[4][0]) for record in records))


def _probe_redirect(client: httpx.Client, url: str) -> tuple[int, str | None]:
    response = client.request("HEAD", url, headers={"User-Agent": "Jobradar/0.1"})
    if response.status_code in {405, 501}:
        with client.stream(
            "GET",
            url,
            headers={"Range": "bytes=0-0", "User-Agent": "Jobradar/0.1"},
        ) as streamed:
            return streamed.status_code, streamed.headers.get("location")
    return response.status_code, response.headers.get("location")
