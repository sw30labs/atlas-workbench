"""Shared provenance validation for externally supplied source references."""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import quote, unquote, urlparse


_PRIVATE_HOST_SUFFIXES = (".local", ".internal", ".corp", ".intranet")
_DNS_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$", re.I)
APPROVED_REVIEW_STATUSES = frozenset({"approved", "approved_synthetic"})


def is_public_http_url(value: object) -> bool:
    """Return whether ``value`` is a structurally public HTTP(S) URL.

    This is a provenance boundary, not an availability check. It rejects
    credentials in URLs, loopback/private/reserved IP literals, single-label
    hosts, and common private DNS suffixes. The application does not resolve or
    fetch the URL, so human review must still confirm that it is genuinely
    public, canonical, and appropriate to use.
    """
    if not isinstance(value, str) or not value or value != value.strip():
        return False
    try:
        parsed = urlparse(value)
        host = (parsed.hostname or "").rstrip(".").lower()
        # Accessing .port validates bracket and port syntax and can raise.
        _ = parsed.port
    except ValueError:
        return False

    if parsed.scheme.lower() not in {"http", "https"}:
        return False
    if not host or parsed.username is not None or parsed.password is not None:
        return False
    if host == "localhost" or host.endswith(_PRIVATE_HOST_SUFFIXES):
        return False

    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        try:
            ascii_host = host.encode("idna").decode("ascii")
        except UnicodeError:
            return False
        labels = ascii_host.split(".")
        return len(labels) >= 2 and all(
            _DNS_LABEL.fullmatch(label) for label in labels
        )
    return address.is_global


# Reserved URL characters are left alone; unreserved-but-unsafe ones (spaces
# above all) are escaped. '%' is safe, so the transform is idempotent and an
# already-encoded URL is never double-encoded. Must stay identical to
# ontology/guard.py:_url_to_iri, or the store and the graph disagree on
# identity — atlas:sourceUrl is inverse-functional, so a raw/encoded pair
# would infer owl:sameAs and surface as a spurious duplicate.
_URL_SAFE = ":/?#[]@!$&'()*+,;=%"


def canonical_public_url(value: str) -> str:
    """Percent-encode a source URL into the form stored and graphed.

    Applied at ingest so no stored ``source_url`` carries literal spaces. It
    does not validate — callers still gate on :func:`is_public_http_url`.
    """
    if not isinstance(value, str):
        return value
    return quote(value.strip(), safe=_URL_SAFE)


def url_dedup_variants(value: str) -> tuple[str, ...]:
    """Every form a stored ``source_url`` may take for the same document.

    Ingest canonicalizes, but rows written before that kept the raw string, so
    an exact-match lookup has to try both directions: an incoming canonical URL
    must find a stored raw row, and vice versa. For lookups only — never store
    the decoded form.
    """
    if not isinstance(value, str) or not value:
        return (value,)
    return tuple(
        dict.fromkeys((value, canonical_public_url(value), unquote(value)))
    )


def all_public_http_urls(values: list[str]) -> bool:
    """Return True for a non-empty list containing only public HTTP(S) URLs."""
    return bool(values) and all(is_public_http_url(value) for value in values)


def is_approved_record(record: dict[str, object]) -> bool:
    """Return whether a record has completed the applicable human review."""
    return str(record.get("review_status") or "") in APPROVED_REVIEW_STATUSES
