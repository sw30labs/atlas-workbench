"""Opt-in public-source scouting that produces review proposals, never evidence.

This module is deliberately outside the eight-phase pipeline. Network collection
is disabled unless a caller passes ``allow_network=True``. Even then, collected
feed entries are emitted only as source-packet proposals that require explicit
human review and cannot be inserted into an Atlas database by this module.
"""

from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import multiprocessing
import queue
import re
import socket
import ssl
import sys
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from importlib import resources
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence
from urllib.parse import urljoin, urlparse, urlunparse
from xml.etree import ElementTree

import yaml

from workbench_atlas.independence_lexicon import host_is_denied
from workbench_atlas.provenance import is_public_http_url

REGISTRY_SCHEMA_VERSION = "atlas-scouting-registry-v1"
OUTPUT_SCHEMA_VERSION = "atlas-scouting-proposals-v1"
SOURCE_CATEGORIES = frozenset(
    {
        "regulator",
        "central_bank",
        "cyber_agency",
        "standards_body",
        "infrastructure_incidents",
        "science_climate_agency",
        "legislative_policy_tracker",
    }
)

ABSOLUTE_MAX_SOURCES = 25
ABSOLUTE_MAX_TIMEOUT_SECONDS = 30.0
ABSOLUTE_MAX_RESPONSE_BYTES = 2_000_000
ABSOLUTE_MAX_ITEMS_PER_SOURCE = 100
ABSOLUTE_MAX_TOTAL_ITEMS = 500
ABSOLUTE_MAX_REDIRECTS = 3
MAX_REGISTRY_SOURCES = 100

_SOURCE_ID = re.compile(r"^[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ALLOWED_CONTENT_TYPES = frozenset(
    {
        "application/atom+xml",
        "application/feed+json",
        "application/json",
        "application/rss+xml",
        "application/xml",
        "text/json",
        "text/xml",
    }
)
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_SOURCE_FIELDS = frozenset(
    {
        "id",
        "name",
        "category",
        "feed_url",
        "homepage_url",
        "allowed_hosts",
        "enabled",
        "jurisdiction",
        "notes",
    }
)
_REGISTRY_FIELDS = frozenset(
    {
        "schema_version",
        "network_collection_default",
        "last_reviewed_on",
        "sources",
    }
)


class ScoutingError(ValueError):
    """Base class for safe, user-facing scouting failures."""


class RegistryValidationError(ScoutingError):
    """Raised when a scouting registry fails closed validation."""


class NetworkOptInRequired(ScoutingError):
    """Raised when collection is attempted without explicit network opt-in."""


class FeedParseError(ScoutingError):
    """Raised when a bounded response is not a supported feed."""


class FetchFailure(ScoutingError):
    """A classified, safe-to-report transport failure."""

    def __init__(self, code: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class RegistrySource:
    """One validated, explicitly allow-listed public feed."""

    source_id: str
    name: str
    category: str
    feed_url: str
    homepage_url: str
    allowed_hosts: tuple[str, ...]
    enabled: bool
    jurisdiction: str
    notes: str

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.source_id,
            "name": self.name,
            "category": self.category,
            "feed_url": self.feed_url,
            "homepage_url": self.homepage_url,
            "allowed_hosts": list(self.allowed_hosts),
            "enabled": self.enabled,
            "jurisdiction": self.jurisdiction,
            "notes": self.notes,
        }


@dataclass(frozen=True)
class SourceRegistry:
    """A validated registry whose default can never enable the network."""

    schema_version: str
    network_collection_default: bool
    last_reviewed_on: str
    sources: tuple[RegistrySource, ...]

    def source_by_id(self, source_id: str) -> RegistrySource | None:
        return next(
            (source for source in self.sources if source.source_id == source_id),
            None,
        )

    @property
    def category_counts(self) -> dict[str, int]:
        return {
            category: sum(
                1 for source in self.sources if source.category == category
            )
            for category in sorted(SOURCE_CATEGORIES)
        }


@dataclass(frozen=True)
class ScoutingLimits:
    """Hard-bounded runtime limits for a single collection run."""

    timeout_seconds: float = 10.0
    max_response_bytes: int = 1_000_000
    max_items_per_source: int = 20
    max_total_items: int = 100
    max_sources: int = 10
    max_redirects: int = 2

    def __post_init__(self) -> None:
        _bounded_number(
            "timeout_seconds",
            self.timeout_seconds,
            minimum=0.1,
            maximum=ABSOLUTE_MAX_TIMEOUT_SECONDS,
        )
        _bounded_integer(
            "max_response_bytes",
            self.max_response_bytes,
            minimum=1,
            maximum=ABSOLUTE_MAX_RESPONSE_BYTES,
        )
        _bounded_integer(
            "max_items_per_source",
            self.max_items_per_source,
            minimum=1,
            maximum=ABSOLUTE_MAX_ITEMS_PER_SOURCE,
        )
        _bounded_integer(
            "max_total_items",
            self.max_total_items,
            minimum=1,
            maximum=ABSOLUTE_MAX_TOTAL_ITEMS,
        )
        _bounded_integer(
            "max_sources",
            self.max_sources,
            minimum=1,
            maximum=ABSOLUTE_MAX_SOURCES,
        )
        _bounded_integer(
            "max_redirects",
            self.max_redirects,
            minimum=0,
            maximum=ABSOLUTE_MAX_REDIRECTS,
        )

    def as_dict(self) -> dict[str, int | float]:
        return {
            "timeout_seconds": self.timeout_seconds,
            "max_response_bytes": self.max_response_bytes,
            "max_items_per_source": self.max_items_per_source,
            "max_total_items": self.max_total_items,
            "max_sources": self.max_sources,
            "max_redirects": self.max_redirects,
        }


@dataclass(frozen=True)
class FetchResponse:
    """Transport-neutral response used to make collection easy to test."""

    requested_url: str
    final_url: str
    status_code: int
    headers: Mapping[str, str]
    body: bytes
    redirects: tuple[str, ...] = ()


@dataclass(frozen=True)
class FeedItem:
    """A minimally parsed feed entry before proposal boundary checks."""

    title: str
    url: str
    summary: str
    published_at: str


class FeedTransport(Protocol):
    """Injectable transport contract for deterministic tests and fixtures."""

    def fetch(
        self,
        url: str,
        *,
        allowed_hosts: frozenset[str],
        timeout_seconds: float,
        max_bytes: int,
        max_redirects: int,
    ) -> FetchResponse:
        """Fetch one bounded response from an allow-listed public endpoint."""


def _bounded_number(
    name: str,
    value: object,
    *,
    minimum: float,
    maximum: float,
) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RegistryValidationError(f"{name} must be a number")
    if not minimum <= float(value) <= maximum:
        raise RegistryValidationError(
            f"{name} must be between {minimum} and {maximum}"
        )


def _bounded_integer(
    name: str,
    value: object,
    *,
    minimum: int,
    maximum: int,
) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RegistryValidationError(f"{name} must be an integer")
    if not minimum <= value <= maximum:
        raise RegistryValidationError(
            f"{name} must be between {minimum} and {maximum}"
        )


def _require_string(
    mapping: Mapping[str, object],
    key: str,
    *,
    context: str,
    maximum: int,
    allow_empty: bool = False,
) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or value != value.strip():
        raise RegistryValidationError(f"{context}.{key} must be a trimmed string")
    if not allow_empty and not value:
        raise RegistryValidationError(f"{context}.{key} must not be empty")
    if len(value) > maximum:
        raise RegistryValidationError(
            f"{context}.{key} exceeds {maximum} characters"
        )
    return value


def _ascii_host(host: str) -> str:
    try:
        return host.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise RegistryValidationError("host is not a valid DNS name") from exc


def _host_from_url(url: str) -> str:
    try:
        parsed = urlparse(url)
        host = parsed.hostname or ""
        _ = parsed.port
    except ValueError as exc:
        raise RegistryValidationError("URL contains invalid host or port syntax") from exc
    if not host:
        raise RegistryValidationError("URL must contain a host")
    return _ascii_host(host)


def is_allowed_source_url(url: object, allowed_hosts: Sequence[str]) -> bool:
    """Return whether a URL is public HTTP(S), on a standard port, and allowed.

    Host rules are intentionally exact; wildcard and implicit subdomain
    matching are not supported.
    """
    if not is_public_http_url(url) or host_is_denied(url):
        return False
    assert isinstance(url, str)
    if any(ord(character) < 32 or ord(character) == 127 for character in url):
        return False
    try:
        parsed = urlparse(url)
        port = parsed.port
        host = _ascii_host(parsed.hostname or "")
    except (ValueError, RegistryValidationError):
        return False
    default_port = 443 if parsed.scheme.lower() == "https" else 80
    if port is not None and port != default_port:
        return False
    normalized_allowed = {
        _ascii_host(value)
        for value in allowed_hosts
        if isinstance(value, str) and value
    }
    return host in normalized_allowed


def validate_registry_data(raw: object) -> SourceRegistry:
    """Validate untrusted YAML-compatible data and return an immutable registry."""
    if not isinstance(raw, Mapping):
        raise RegistryValidationError("registry root must be a mapping")
    unknown = set(raw) - _REGISTRY_FIELDS
    if unknown:
        raise RegistryValidationError(
            f"registry has unknown fields: {', '.join(sorted(map(str, unknown)))}"
        )

    schema_version = _require_string(
        raw,
        "schema_version",
        context="registry",
        maximum=80,
    )
    if schema_version != REGISTRY_SCHEMA_VERSION:
        raise RegistryValidationError(
            f"registry.schema_version must be {REGISTRY_SCHEMA_VERSION!r}"
        )
    if raw.get("network_collection_default") is not False:
        raise RegistryValidationError(
            "registry.network_collection_default must be false"
        )
    last_reviewed_on = _require_string(
        raw,
        "last_reviewed_on",
        context="registry",
        maximum=10,
    )
    if not _DATE.fullmatch(last_reviewed_on):
        raise RegistryValidationError(
            "registry.last_reviewed_on must use YYYY-MM-DD"
        )
    try:
        date.fromisoformat(last_reviewed_on)
    except ValueError as exc:
        raise RegistryValidationError(
            "registry.last_reviewed_on is not a calendar date"
        ) from exc

    source_values = raw.get("sources")
    if not isinstance(source_values, list) or not source_values:
        raise RegistryValidationError("registry.sources must be a non-empty list")
    if len(source_values) > MAX_REGISTRY_SOURCES:
        raise RegistryValidationError(
            f"registry.sources exceeds {MAX_REGISTRY_SOURCES} entries"
        )

    sources: list[RegistrySource] = []
    seen_ids: set[str] = set()
    for index, value in enumerate(source_values):
        context = f"registry.sources[{index}]"
        if not isinstance(value, Mapping):
            raise RegistryValidationError(f"{context} must be a mapping")
        unknown = set(value) - _SOURCE_FIELDS
        if unknown:
            raise RegistryValidationError(
                f"{context} has unknown fields: "
                f"{', '.join(sorted(map(str, unknown)))}"
            )
        source_id = _require_string(
            value,
            "id",
            context=context,
            maximum=64,
        )
        if not _SOURCE_ID.fullmatch(source_id):
            raise RegistryValidationError(
                f"{context}.id must be a lowercase slug"
            )
        if source_id in seen_ids:
            raise RegistryValidationError(
                f"{context}.id duplicates {source_id!r}"
            )
        seen_ids.add(source_id)

        name = _require_string(value, "name", context=context, maximum=200)
        category = _require_string(
            value,
            "category",
            context=context,
            maximum=64,
        )
        if category not in SOURCE_CATEGORIES:
            raise RegistryValidationError(
                f"{context}.category must be one of "
                f"{', '.join(sorted(SOURCE_CATEGORIES))}"
            )
        feed_url = _require_string(
            value,
            "feed_url",
            context=context,
            maximum=2048,
        )
        homepage_url = _require_string(
            value,
            "homepage_url",
            context=context,
            maximum=2048,
        )
        host_values = value.get("allowed_hosts")
        if (
            not isinstance(host_values, list)
            or not host_values
            or len(host_values) > 10
        ):
            raise RegistryValidationError(
                f"{context}.allowed_hosts must contain 1 to 10 hosts"
            )
        allowed_hosts: list[str] = []
        for host_index, host_value in enumerate(host_values):
            if (
                not isinstance(host_value, str)
                or not host_value
                or host_value != host_value.strip()
            ):
                raise RegistryValidationError(
                    f"{context}.allowed_hosts[{host_index}] must be a host string"
                )
            host = _ascii_host(host_value)
            if host_is_denied(host):
                raise RegistryValidationError(
                    f"{context}.allowed_hosts[{host_index}] is excluded"
                )
            if not is_public_http_url(f"https://{host}/"):
                raise RegistryValidationError(
                    f"{context}.allowed_hosts[{host_index}] is not public"
                )
            if host in allowed_hosts:
                raise RegistryValidationError(
                    f"{context}.allowed_hosts contains duplicate {host!r}"
                )
            allowed_hosts.append(host)

        if not is_allowed_source_url(feed_url, allowed_hosts):
            raise RegistryValidationError(
                f"{context}.feed_url is not on its public host allow-list"
            )
        if not is_allowed_source_url(homepage_url, allowed_hosts):
            raise RegistryValidationError(
                f"{context}.homepage_url is not on its public host allow-list"
            )
        enabled = value.get("enabled")
        if not isinstance(enabled, bool):
            raise RegistryValidationError(f"{context}.enabled must be boolean")
        jurisdiction = _require_string(
            value,
            "jurisdiction",
            context=context,
            maximum=100,
        )
        notes = _require_string(
            value,
            "notes",
            context=context,
            maximum=500,
            allow_empty=True,
        )
        sources.append(
            RegistrySource(
                source_id=source_id,
                name=name,
                category=category,
                feed_url=feed_url,
                homepage_url=homepage_url,
                allowed_hosts=tuple(allowed_hosts),
                enabled=enabled,
                jurisdiction=jurisdiction,
                notes=notes,
            )
        )

    return SourceRegistry(
        schema_version=schema_version,
        network_collection_default=False,
        last_reviewed_on=last_reviewed_on,
        sources=tuple(sources),
    )


def load_registry(path: str | Path | None = None) -> SourceRegistry:
    """Load and validate a custom registry or the packaged default registry."""
    if path is None:
        text = (
            resources.files("workbench_atlas")
            .joinpath("source_registry.yaml")
            .read_text(encoding="utf-8")
        )
    else:
        text = Path(path).read_text(encoding="utf-8")
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise RegistryValidationError(f"registry YAML is invalid: {exc}") from exc
    return validate_registry_data(raw)


def registry_summary(registry: SourceRegistry) -> dict[str, object]:
    """Return transparent validation metadata without performing any fetch."""
    return {
        "valid": True,
        "schema_version": registry.schema_version,
        "network_collection_default": registry.network_collection_default,
        "last_reviewed_on": registry.last_reviewed_on,
        "source_count": len(registry.sources),
        "enabled_source_count": sum(
            1 for source in registry.sources if source.enabled
        ),
        "category_counts": registry.category_counts,
        "missing_categories": sorted(
            SOURCE_CATEGORIES
            - {source.category for source in registry.sources}
        ),
    }


def _remaining_seconds(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise FetchFailure(
            "timeout",
            "source request exceeded the timeout",
            retryable=True,
        )
    return remaining


def _resolve_public_addresses(
    host: str,
    port: int,
    *,
    deadline: float,
) -> tuple[tuple[int, tuple[object, ...]], ...]:
    """Resolve on a bounded daemon thread and return only public socket targets.

    The returned addresses are used directly for the connection. The transport
    therefore does not perform a second, attacker-influenceable DNS lookup
    after this validation step.
    """
    result_queue: queue.Queue[
        tuple[list[tuple[Any, ...]] | None, BaseException | None]
    ] = queue.Queue(maxsize=1)

    def resolve() -> None:
        try:
            answers = socket.getaddrinfo(
                host,
                port,
                type=socket.SOCK_STREAM,
            )
            result_queue.put((answers, None))
        except BaseException as exc:  # pragma: no cover - defensive thread edge
            result_queue.put((None, exc))

    threading.Thread(
        target=resolve,
        name="atlas-scout-dns",
        daemon=True,
    ).start()
    try:
        answers, error = result_queue.get(
            timeout=_remaining_seconds(deadline)
        )
    except queue.Empty as exc:
        raise FetchFailure(
            "timeout",
            "source DNS resolution exceeded the timeout",
            retryable=True,
        ) from exc
    _remaining_seconds(deadline)
    if error is not None:
        raise FetchFailure(
            "dns_resolution_failed",
            "source host could not be resolved",
            retryable=True,
        ) from error
    assert answers is not None

    targets: list[tuple[int, tuple[object, ...]]] = []
    seen: set[tuple[int, str, int]] = set()
    for family, socket_type, _protocol, _canonical, sockaddr in answers:
        if socket_type not in {0, socket.SOCK_STREAM} or not sockaddr:
            continue
        address = str(sockaddr[0])
        try:
            is_public = ipaddress.ip_address(address).is_global
        except ValueError as exc:
            raise FetchFailure(
                "dns_resolution_failed",
                "source host returned an invalid network address",
            ) from exc
        if not is_public:
            raise FetchFailure(
                "non_public_dns",
                "source host resolved to a non-public network address",
            )
        key = (family, address, int(sockaddr[1]))
        if key not in seen:
            targets.append((family, tuple(sockaddr)))
            seen.add(key)
    if not targets:
        raise FetchFailure(
            "dns_resolution_failed",
            "source host returned no usable network addresses",
            retryable=True,
        )
    return tuple(targets)


def _connect_pinned(
    targets: Sequence[tuple[int, tuple[object, ...]]],
    *,
    deadline: float,
) -> socket.socket:
    """Connect to one previously validated address within the shared deadline."""
    last_error: OSError | None = None
    for index, (family, sockaddr) in enumerate(targets):
        remaining = _remaining_seconds(deadline)
        attempts_left = len(targets) - index
        attempt_timeout = max(0.05, remaining / attempts_left)
        candidate = socket.socket(family, socket.SOCK_STREAM)
        try:
            candidate.settimeout(min(remaining, attempt_timeout))
            candidate.connect(sockaddr)
            candidate.settimeout(_remaining_seconds(deadline))
            return candidate
        except OSError as exc:
            last_error = exc
            candidate.close()
    if isinstance(last_error, (TimeoutError, socket.timeout)):
        raise FetchFailure(
            "timeout",
            "source connection exceeded the timeout",
            retryable=True,
        ) from last_error
    raise FetchFailure(
        "network_error",
        "source connection failed",
        retryable=True,
    ) from last_error


def _check_fetch_target(url: str, allowed_hosts: frozenset[str]) -> None:
    if not is_allowed_source_url(url, tuple(allowed_hosts)):
        raise FetchFailure(
            "url_not_allowed",
            "fetch URL is outside the source host allow-list",
        )


def _receive_isolated_payload(
    receiver: Any,
    results: queue.Queue[tuple[str, object]],
) -> None:
    """Receive a possibly large pipe frame without blocking the deadline owner."""
    try:
        payload = receiver.recv()
    except BaseException as exc:
        results.put(("error", exc))
    else:
        results.put(("ok", payload))


class PinnedFeedTransport:
    """Hard-deadline HTTP transport with DNS pinning and SSRF defenses."""

    def __init__(self, *, isolate_process: bool = True):
        # Platform DNS and some TLS/HTTP parsing paths cannot be cancelled
        # reliably from a Python thread. Production fetches therefore run in a
        # killable process; deterministic local transport tests may opt out.
        self._isolate_process = isolate_process

    def fetch(
        self,
        url: str,
        *,
        allowed_hosts: frozenset[str],
        timeout_seconds: float,
        max_bytes: int,
        max_redirects: int,
    ) -> FetchResponse:
        """Run the complete exchange behind a strict, killable deadline."""
        deadline = time.monotonic() + timeout_seconds
        if not self._isolate_process:
            return self._fetch(
                url,
                allowed_hosts=allowed_hosts,
                deadline=deadline,
                max_bytes=max_bytes,
                max_redirects=max_redirects,
            )

        if multiprocessing.current_process().daemon:
            raise FetchFailure(
                "process_isolation_unavailable",
                "killable source transport is unavailable in a daemonic process",
            )
        main_path = getattr(sys.modules.get("__main__"), "__file__", None)
        if isinstance(main_path, str) and main_path.startswith("<"):
            raise FetchFailure(
                "process_isolation_unavailable",
                "killable source transport is unavailable from a pseudo-file entry point",
            )

        context = multiprocessing.get_context("spawn")
        receiver, sender = context.Pipe(duplex=False)
        receive_results: queue.Queue[tuple[str, object]] = queue.Queue(
            maxsize=1
        )
        receiver_thread: threading.Thread | None = None
        process = context.Process(
            target=_isolated_fetch_worker,
            args=(
                sender,
                url,
                allowed_hosts,
                timeout_seconds,
                max_bytes,
                max_redirects,
            ),
            name="atlas-scout-fetch",
            daemon=True,
        )
        try:
            process.start()
            sender.close()
            receiver_thread = threading.Thread(
                target=_receive_isolated_payload,
                args=(receiver, receive_results),
                name="atlas-scout-recv",
                daemon=True,
            )
            receiver_thread.start()
            try:
                receive_status, received = receive_results.get(
                    timeout=_remaining_seconds(deadline)
                )
            except queue.Empty as exc:
                raise FetchFailure(
                    "timeout",
                    "source request exceeded the timeout",
                    retryable=True,
                ) from exc
            _remaining_seconds(deadline)
            if receive_status != "ok":
                assert isinstance(received, BaseException)
                raise FetchFailure(
                    "network_error",
                    "isolated source transport exited without a result",
                    retryable=True,
                ) from received
            payload = received
        except FetchFailure:
            raise
        except (AssertionError, OSError, RuntimeError) as exc:
            raise FetchFailure(
                "process_isolation_unavailable",
                "killable source transport could not be started",
            ) from exc
        finally:
            sender.close()
            # This also runs for cancellation and BaseException paths so a
            # caller interruption cannot strand a DNS/HTTP worker.
            _stop_fetch_process(process)
            receiver.close()
            if receiver_thread is not None:
                receiver_thread.join(timeout=0.2)

        if (
            isinstance(payload, tuple)
            and len(payload) >= 2
            and payload[0] == "ok"
            and isinstance(payload[1], FetchResponse)
        ):
            return payload[1]
        if (
            isinstance(payload, tuple)
            and len(payload) == 4
            and payload[0] == "fetch_failure"
        ):
            raise FetchFailure(
                str(payload[1]),
                str(payload[2]),
                retryable=bool(payload[3]),
            )
        raise FetchFailure(
            "network_error",
            "isolated source transport failed",
            retryable=True,
        )

    def _fetch(
        self,
        url: str,
        *,
        allowed_hosts: frozenset[str],
        deadline: float,
        max_bytes: int,
        max_redirects: int,
    ) -> FetchResponse:
        current_url = url
        redirects: list[str] = []
        headers = {
            "Accept": (
                "application/rss+xml, application/atom+xml, "
                "application/feed+json, application/xml, text/xml;q=0.9"
            ),
            "User-Agent": (
                "workbench-atlas-scouting/0.1 "
                "(proposal-only; human-review-required)"
            ),
            # http.client does not transparently decompress, and rejecting any
            # contrary response encoding below prevents compressed-size bombs.
            "Accept-Encoding": "identity",
            "Connection": "close",
        }
        try:
            while True:
                _remaining_seconds(deadline)
                _check_fetch_target(current_url, allowed_hosts)
                parsed = urlparse(current_url)
                host = _host_from_url(current_url)
                scheme = parsed.scheme.lower()
                port = parsed.port or (443 if scheme == "https" else 80)
                targets = _resolve_public_addresses(
                    host,
                    port,
                    deadline=deadline,
                )
                network_socket = _connect_pinned(targets, deadline=deadline)
                if scheme == "https":
                    try:
                        network_socket.settimeout(
                            _remaining_seconds(deadline)
                        )
                        network_socket = ssl.create_default_context().wrap_socket(
                            network_socket,
                            server_hostname=host,
                        )
                    except BaseException:
                        network_socket.close()
                        raise

                connection = http.client.HTTPConnection(
                    host,
                    port,
                    timeout=_remaining_seconds(deadline),
                )
                connection.sock = network_socket
                target = urlunparse(
                    ("", "", parsed.path or "/", parsed.params, parsed.query, "")
                )
                response: http.client.HTTPResponse | None = None
                try:
                    network_socket.settimeout(_remaining_seconds(deadline))
                    connection.request("GET", target, headers=headers)
                    network_socket.settimeout(_remaining_seconds(deadline))
                    # Build the response directly on the pinned socket. Calling
                    # HTTPConnection.getresponse() may close its socket handle
                    # immediately for `Connection: close`, which prevents us
                    # from tightening the timeout to the remaining wall clock
                    # before every bounded read.
                    response = http.client.HTTPResponse(
                        network_socket,
                        method="GET",
                    )
                    response.begin()
                    response_headers = {
                        key.lower(): value
                        for key, value in response.headers.items()
                    }
                    if response.status in _REDIRECT_STATUSES:
                        location = response.headers.get("location")
                        if not location:
                            raise FetchFailure(
                                "invalid_redirect",
                                "redirect response omitted its target",
                            )
                        if len(redirects) >= max_redirects:
                            raise FetchFailure(
                                "redirect_limit",
                                "source exceeded the redirect limit",
                            )
                        next_url = urljoin(current_url, location)
                        if not is_allowed_source_url(
                            next_url,
                            tuple(allowed_hosts),
                        ):
                            raise FetchFailure(
                                "redirect_not_allowed",
                                "redirect target is outside the source allow-list",
                            )
                        redirects.append(next_url)
                        current_url = next_url
                        continue
                    if not 200 <= response.status < 300:
                        raise FetchFailure(
                            "http_status",
                            f"source returned HTTP {response.status}",
                            retryable=response.status >= 500
                            or response.status == 429,
                        )
                    content_encoding = (
                        response.headers.get("content-encoding") or ""
                    ).strip().lower()
                    if content_encoding not in {"", "identity"}:
                        raise FetchFailure(
                            "unexpected_content_encoding",
                            "source ignored the identity-encoding requirement",
                        )
                    content_length = response.headers.get("content-length")
                    if content_length:
                        try:
                            declared_size = int(content_length)
                        except ValueError:
                            declared_size = 0
                        if declared_size > max_bytes:
                            raise FetchFailure(
                                "response_too_large",
                                "source response exceeds the byte limit",
                            )
                    chunks: list[bytes] = []
                    received = 0
                    while True:
                        network_socket.settimeout(_remaining_seconds(deadline))
                        chunk = response.read1(
                            min(65_536, max_bytes - received + 1)
                        )
                        _remaining_seconds(deadline)
                        if not chunk:
                            break
                        received += len(chunk)
                        if received > max_bytes:
                            raise FetchFailure(
                                "response_too_large",
                                "source response exceeds the byte limit",
                            )
                        chunks.append(chunk)
                    return FetchResponse(
                        requested_url=url,
                        final_url=current_url,
                        status_code=response.status,
                        headers=response_headers,
                        body=b"".join(chunks),
                        redirects=tuple(redirects),
                    )
                finally:
                    if response is not None:
                        response.close()
                    connection.close()
        except FetchFailure:
            raise
        except (TimeoutError, socket.timeout) as exc:
            raise FetchFailure(
                "timeout",
                "source request exceeded the timeout",
                retryable=True,
            ) from exc
        except (http.client.HTTPException, OSError, ssl.SSLError) as exc:
            raise FetchFailure(
                "network_error",
                "source request failed at the HTTP transport",
                retryable=True,
            ) from exc


def _stop_fetch_process(process: multiprocessing.Process) -> None:
    """Kill and reap an isolated fetch worker without retaining resources."""
    if process.pid is None:
        process.close()
        return
    if process.is_alive():
        process.kill()
    process.join(timeout=0.2)
    if process.is_alive():  # pragma: no cover - exceptional OS scheduling edge
        process.terminate()
        process.join(timeout=0.2)
    if not process.is_alive():
        process.close()


def _isolated_fetch_worker(
    sender: Any,
    url: str,
    allowed_hosts: frozenset[str],
    timeout_seconds: float,
    max_bytes: int,
    max_redirects: int,
) -> None:
    """Child-process entry point; emit only a small classified result tuple."""
    try:
        response = PinnedFeedTransport(isolate_process=False)._fetch(
            url,
            allowed_hosts=allowed_hosts,
            deadline=time.monotonic() + timeout_seconds,
            max_bytes=max_bytes,
            max_redirects=max_redirects,
        )
        payload: tuple[object, ...] = ("ok", response)
    except FetchFailure as exc:
        payload = (
            "fetch_failure",
            exc.code,
            str(exc),
            exc.retryable,
        )
    except BaseException as exc:  # pragma: no cover - process safety boundary
        payload = ("unexpected", exc.__class__.__name__)
    try:
        sender.send(payload)
    except (BrokenPipeError, EOFError, OSError):
        pass
    finally:
        sender.close()


# Backwards-compatible name for callers that imported the initial transport.
HttpxFeedTransport = PinnedFeedTransport


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _clean_text(value: object, maximum: int) -> str:
    if value is None:
        return ""
    parser = _TextExtractor()
    try:
        parser.feed(str(value))
        parser.close()
        text = " ".join(parser.parts)
    except (ValueError, TypeError):
        text = str(value)
    return " ".join(text.split())[:maximum]


def _local_name(tag: object) -> str:
    return str(tag).rsplit("}", 1)[-1].lower()


def _element_text(element: ElementTree.Element) -> str:
    return "".join(element.itertext())


def _first_element_text(
    entry: ElementTree.Element,
    names: frozenset[str],
) -> str:
    for child in entry:
        if _local_name(child.tag) in names:
            return _element_text(child)
    return ""


def _xml_link(entry: ElementTree.Element) -> str:
    fallback = ""
    for child in entry:
        if _local_name(child.tag) != "link":
            continue
        href = str(child.attrib.get("href") or "").strip()
        text = _element_text(child).strip()
        candidate = href or text
        if not candidate:
            continue
        relation = str(child.attrib.get("rel") or "alternate").lower()
        if relation == "alternate":
            return candidate
        if not fallback:
            fallback = candidate
    return fallback


def _parse_xml_feed(body: bytes, limit: int) -> list[FeedItem]:
    upper_body = body.upper()
    if b"<!DOCTYPE" in upper_body or b"<!ENTITY" in upper_body:
        raise FeedParseError("XML declarations and entities are not supported")
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError as exc:
        raise FeedParseError("response is not valid RSS or Atom XML") from exc
    entry_elements = [
        element
        for element in root.iter()
        if _local_name(element.tag) in {"item", "entry"}
    ]
    if not entry_elements:
        raise FeedParseError("XML response contains no RSS or Atom entries")

    items: list[FeedItem] = []
    for entry in entry_elements[:limit]:
        items.append(
            FeedItem(
                title=_clean_text(
                    _first_element_text(entry, frozenset({"title"})),
                    300,
                ),
                url=_xml_link(entry),
                summary=_clean_text(
                    _first_element_text(
                        entry,
                        frozenset(
                            {"description", "summary", "content", "encoded"}
                        ),
                    ),
                    1_000,
                ),
                published_at=_clean_text(
                    _first_element_text(
                        entry,
                        frozenset(
                            {
                                "pubdate",
                                "published",
                                "updated",
                                "date",
                            }
                        ),
                    ),
                    100,
                ),
            )
        )
    return items


def _parse_json_feed(body: bytes, limit: int) -> list[FeedItem]:
    try:
        value = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise FeedParseError("response is not valid JSON Feed data") from exc
    if not isinstance(value, Mapping) or not isinstance(value.get("items"), list):
        raise FeedParseError("JSON response does not contain a feed items list")
    items: list[FeedItem] = []
    for raw_item in value["items"][:limit]:
        if not isinstance(raw_item, Mapping):
            continue
        items.append(
            FeedItem(
                title=_clean_text(raw_item.get("title"), 300),
                url=str(
                    raw_item.get("url")
                    or raw_item.get("external_url")
                    or ""
                ).strip(),
                summary=_clean_text(
                    raw_item.get("summary")
                    or raw_item.get("content_text")
                    or raw_item.get("content_html"),
                    1_000,
                ),
                published_at=_clean_text(
                    raw_item.get("date_published")
                    or raw_item.get("date_modified"),
                    100,
                ),
            )
        )
    if not items:
        raise FeedParseError("JSON Feed response contains no object entries")
    return items


def parse_feed_items(
    body: bytes,
    *,
    content_type: str = "",
    limit: int = ABSOLUTE_MAX_ITEMS_PER_SOURCE,
) -> list[FeedItem]:
    """Parse a bounded RSS, Atom, or JSON Feed response without fetching."""
    _bounded_integer(
        "limit",
        limit,
        minimum=1,
        maximum=ABSOLUTE_MAX_ITEMS_PER_SOURCE,
    )
    if not isinstance(body, bytes) or not body:
        raise FeedParseError("feed response body is empty")
    media_type = content_type.partition(";")[0].strip().lower()
    stripped = body.removeprefix(b"\xef\xbb\xbf").lstrip()
    if media_type in {"application/feed+json", "application/json", "text/json"}:
        return _parse_json_feed(body, limit)
    if media_type and media_type not in _ALLOWED_CONTENT_TYPES:
        raise FeedParseError(f"unsupported feed content type {media_type!r}")
    if stripped.startswith((b"{", b"[")):
        return _parse_json_feed(body, limit)
    if stripped.startswith(b"<"):
        return _parse_xml_feed(body, limit)
    raise FeedParseError("response is not recognizable RSS, Atom, or JSON Feed data")


def _canonical_url(value: str) -> str:
    parsed = urlparse(value)
    return urlunparse(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path or "/",
            parsed.params,
            parsed.query,
            "",
        )
    )


def _timestamp(value: datetime | None) -> str:
    moment = value or datetime.now(timezone.utc)
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ScoutingError("now must be timezone-aware")
    return (
        moment.astimezone(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _safe_error_message(value: object) -> str:
    return " ".join(str(value).split())[:300]


def _error(
    *,
    source_id: str | None,
    stage: str,
    code: str,
    message: str,
    retryable: bool = False,
    item_index: int | None = None,
) -> dict[str, object]:
    result: dict[str, object] = {
        "source_id": source_id,
        "stage": stage,
        "code": code,
        "message": _safe_error_message(message),
        "retryable": retryable,
    }
    if item_index is not None:
        result["item_index"] = item_index
    return result


def _validate_response(
    response: FetchResponse,
    source: RegistrySource,
    limits: ScoutingLimits,
) -> tuple[str, str]:
    allowed_hosts = source.allowed_hosts
    if _canonical_url(response.requested_url) != _canonical_url(source.feed_url):
        raise FetchFailure(
            "request_url_mismatch",
            "transport response does not match the requested registry URL",
        )
    for hop in (*response.redirects, response.final_url):
        if not is_allowed_source_url(hop, allowed_hosts):
            raise FetchFailure(
                "response_url_not_allowed",
                "transport returned a URL outside the source allow-list",
            )
    if len(response.redirects) > limits.max_redirects:
        raise FetchFailure(
            "redirect_limit",
            "transport response exceeded the redirect limit",
        )
    if not 200 <= response.status_code < 300:
        raise FetchFailure(
            "http_status",
            f"source returned HTTP {response.status_code}",
            retryable=response.status_code >= 500 or response.status_code == 429,
        )
    if len(response.body) > limits.max_response_bytes:
        raise FetchFailure(
            "response_too_large",
            "source response exceeds the byte limit",
        )
    normalized_headers = {
        str(key).lower(): str(value) for key, value in response.headers.items()
    }
    content_type = normalized_headers.get("content-type", "")
    return response.final_url, content_type


def _proposal(
    *,
    source: RegistrySource,
    item: FeedItem,
    item_url: str,
    retrieved_at: str,
    feed_final_url: str,
    content_type: str,
) -> dict[str, object]:
    digest_input = "\n".join(
        (source.source_id, _canonical_url(item_url), item.title, item.summary)
    ).encode("utf-8")
    digest = hashlib.sha256(digest_input).hexdigest()
    return {
        "proposal_id": f"SCOUT-{digest[:16].upper()}",
        "record_type": "scouting_source_packet_proposal",
        "review_status": "pending_human_review",
        "evidence_status": "not_evidence",
        "pipeline_eligible": False,
        "rights_status": "unreviewed",
        "title": item.title,
        "summary_excerpt": item.summary,
        "published_at": item.published_at or None,
        "source_url": item_url,
        "source_publisher": source.name,
        "source_category": source.category,
        "source_jurisdiction": source.jurisdiction,
        "registry_source_id": source.source_id,
        "registry_homepage_url": source.homepage_url,
        "collection": {
            "retrieved_at": retrieved_at,
            "feed_url": source.feed_url,
            "feed_final_url": feed_final_url,
            "content_type": content_type.partition(";")[0].strip().lower() or None,
            "content_fingerprint_sha256": digest,
        },
        "provenance_note": (
            "Automated public-feed scouting proposal only. Verify the canonical "
            "source, accuracy, relevance, rights, and provenance before manually "
            "creating any evidence record."
        ),
    }


def _select_sources(
    registry: SourceRegistry,
    source_ids: Sequence[str] | None,
) -> tuple[list[RegistrySource], list[dict[str, object]]]:
    errors: list[dict[str, object]] = []
    if source_ids is None:
        return [source for source in registry.sources if source.enabled], errors
    if isinstance(source_ids, (str, bytes)):
        raise RegistryValidationError("source_ids must be a sequence of source IDs")

    requested = list(dict.fromkeys(source_ids))
    unknown = [
        source_id
        for source_id in requested
        if registry.source_by_id(source_id) is None
    ]
    if unknown:
        raise RegistryValidationError(
            f"unknown registry source IDs: {', '.join(sorted(unknown))}"
        )
    selected: list[RegistrySource] = []
    for source_id in requested:
        source = registry.source_by_id(source_id)
        assert source is not None
        if not source.enabled:
            errors.append(
                _error(
                    source_id=source_id,
                    stage="selection",
                    code="source_disabled",
                    message="registry source is disabled",
                )
            )
            continue
        selected.append(source)
    return selected, errors


def collect_proposals(
    registry: SourceRegistry | None = None,
    *,
    allow_network: bool = False,
    source_ids: Sequence[str] | None = None,
    limits: ScoutingLimits | None = None,
    transport: FeedTransport | None = None,
    now: datetime | None = None,
) -> dict[str, object]:
    """Collect bounded feed entries as pending source-packet proposals.

    This function never imports or calls the pipeline, phases, schema, or
    database modules. It performs no writes and has no promotion operation.
    """
    if allow_network is not True:
        raise NetworkOptInRequired(
            "network scouting is disabled; pass allow_network=True explicitly"
        )
    active_registry = registry or load_registry()
    active_limits = limits or ScoutingLimits()
    active_transport = transport or HttpxFeedTransport()
    retrieved_at = _timestamp(now)
    selected, errors = _select_sources(active_registry, source_ids)

    if len(selected) > active_limits.max_sources:
        for source in selected[active_limits.max_sources :]:
            errors.append(
                _error(
                    source_id=source.source_id,
                    stage="selection",
                    code="source_limit_reached",
                    message="source was skipped by the per-run source limit",
                )
            )
        selected = selected[: active_limits.max_sources]

    proposals: list[dict[str, object]] = []
    source_results: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    for source_index, source in enumerate(selected):
        if len(proposals) >= active_limits.max_total_items:
            for skipped in selected[source_index:]:
                source_results.append(
                    {
                        "source_id": skipped.source_id,
                        "status": "skipped",
                        "reason": "total_item_limit_reached",
                        "requested_url": skipped.feed_url,
                        "proposal_count": 0,
                    }
                )
            break

        try:
            response = active_transport.fetch(
                source.feed_url,
                allowed_hosts=frozenset(source.allowed_hosts),
                timeout_seconds=active_limits.timeout_seconds,
                max_bytes=active_limits.max_response_bytes,
                max_redirects=active_limits.max_redirects,
            )
            final_url, content_type = _validate_response(
                response,
                source,
                active_limits,
            )
            remaining = active_limits.max_total_items - len(proposals)
            parse_limit = min(active_limits.max_items_per_source, remaining)
            items = parse_feed_items(
                response.body,
                content_type=content_type,
                limit=parse_limit,
            )
            accepted = 0
            skipped_count = 0
            for item_index, item in enumerate(items):
                if not item.title:
                    skipped_count += 1
                    errors.append(
                        _error(
                            source_id=source.source_id,
                            stage="item_validation",
                            code="missing_title",
                            message="feed item has no usable title",
                            item_index=item_index,
                        )
                    )
                    continue
                item_url = urljoin(final_url, item.url)
                if not item.url or not is_allowed_source_url(
                    item_url,
                    source.allowed_hosts,
                ):
                    skipped_count += 1
                    errors.append(
                        _error(
                            source_id=source.source_id,
                            stage="item_validation",
                            code="item_url_not_allowed",
                            message=(
                                "feed item URL is missing, non-public, or outside "
                                "the source host allow-list"
                            ),
                            item_index=item_index,
                        )
                    )
                    continue
                item_url = _canonical_url(item_url)
                deduplication_key = (source.source_id, item_url)
                if deduplication_key in seen:
                    skipped_count += 1
                    continue
                seen.add(deduplication_key)
                proposals.append(
                    _proposal(
                        source=source,
                        item=item,
                        item_url=item_url,
                        retrieved_at=retrieved_at,
                        feed_final_url=final_url,
                        content_type=content_type,
                    )
                )
                accepted += 1

            source_results.append(
                {
                    "source_id": source.source_id,
                    "status": "collected",
                    "requested_url": source.feed_url,
                    "final_url": final_url,
                    "http_status": response.status_code,
                    "content_type": content_type.partition(";")[0].strip().lower()
                    or None,
                    "bytes_received": len(response.body),
                    "redirect_count": len(response.redirects),
                    "parsed_item_count": len(items),
                    "proposal_count": accepted,
                    "skipped_item_count": skipped_count,
                    "retrieved_at": retrieved_at,
                }
            )
        except FetchFailure as exc:
            errors.append(
                _error(
                    source_id=source.source_id,
                    stage="fetch",
                    code=exc.code,
                    message=str(exc),
                    retryable=exc.retryable,
                )
            )
            source_results.append(
                {
                    "source_id": source.source_id,
                    "status": "error",
                    "requested_url": source.feed_url,
                    "error_code": exc.code,
                    "proposal_count": 0,
                }
            )
        except FeedParseError as exc:
            errors.append(
                _error(
                    source_id=source.source_id,
                    stage="parse",
                    code="feed_parse_error",
                    message=str(exc),
                )
            )
            source_results.append(
                {
                    "source_id": source.source_id,
                    "status": "error",
                    "requested_url": source.feed_url,
                    "error_code": "feed_parse_error",
                    "proposal_count": 0,
                }
            )
        except Exception as exc:
            errors.append(
                _error(
                    source_id=source.source_id,
                    stage="transport",
                    code="transport_error",
                    message=exc.__class__.__name__,
                )
            )
            source_results.append(
                {
                    "source_id": source.source_id,
                    "status": "error",
                    "requested_url": source.feed_url,
                    "error_code": "transport_error",
                    "proposal_count": 0,
                }
            )

    return {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "generated_at": retrieved_at,
        "scouting_status": "proposal_only",
        "network_opt_in_confirmed": True,
        "human_review_required": True,
        "pipeline_invoked": False,
        "evidence_records_created": 0,
        "registry": {
            "schema_version": active_registry.schema_version,
            "last_reviewed_on": active_registry.last_reviewed_on,
            "network_collection_default": (
                active_registry.network_collection_default
            ),
        },
        "limits": active_limits.as_dict(),
        "source_results": source_results,
        "proposal_count": len(proposals),
        "error_count": len(errors),
        "proposals": proposals,
        "errors": errors,
    }
