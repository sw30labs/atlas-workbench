"""Public-identity residue lexicon and excluded-source policy.

This module is the machine-readable source of truth for residue class R1–R4
string terms, schema-forbidden identifiers, retired prompt-key shapes, and
the research/scout host denylist.

Terms that would trip ``scripts/public_release_gate.py`` are assembled at
runtime from fragments. Do not write a complete proper noun, consortium field
title, or leftover alias key as one token in this file.

Path allowlists (disclosure files, reviewed binaries, per-rule exemptions)
stay in the gate. Network admission paths must call :func:`host_is_denied`
before accepting a registry host, source URL, or redirect target.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from urllib.parse import urlparse


def _joined(*parts: str) -> str:
    """Build an audit term without storing the completed token in source."""
    return "".join(parts)


# ---------------------------------------------------------------------------
# String terms — worktree/history scanner for legacy identifiers, project
# names, disclosure boundaries, and credentials.
# ---------------------------------------------------------------------------

STRING_FORBIDDEN: dict[str, re.Pattern[str]] = {
    "legacy project identity": re.compile(
        rf"(?i)\b{_joined('grey')}[\s_-]*{_joined('swan')}\b"
    ),
    "legacy environment variable": re.compile(_joined("GREY", "SWAN_")),
    "legacy scenario database": re.compile(_joined("grey", "swandb"), re.I),
    "legacy evidence database": re.compile(_joined("news", "db"), re.I),
    "retired evidence-event identifier": re.compile(
        rf"(?i)(?:\b{_joined('News', 'Event')}\b|"
        rf"(?<![a-z0-9]){_joined('news', '_event')}(?![a-z0-9]))"
    ),
    "restricted-source endpoint": re.compile(
        rf"(?i)(?:{_joined('news', '.', 'o', 'rx', r'\.')}|/node/\d+)"
    ),
    "legacy event identifier": re.compile(
        _joined("Story ", "Reference Number"), re.I
    ),
    "legacy scenario identifier": re.compile(
        _joined("Insight ", "Scenario ID"), re.I
    ),
    "legacy organization field": re.compile(
        _joined("Institution ", "Name"), re.I
    ),
    "legacy date field": re.compile(_joined("Date First ", "In Media"), re.I),
    "legacy category field": re.compile(
        _joined("Scenario Category ", "Name"), re.I
    ),
    "legacy event-type field": re.compile(
        _joined("Event Type Level 1 ", "Name"), re.I
    ),
    "legacy root-cause field": re.compile(
        _joined("Root Cause Level 1 ", "Name"), re.I
    ),
    "prediction-era event field": re.compile(
        _joined("predicted_", "event"), re.I
    ),
    "prediction-era date field": re.compile(_joined("predicted_", "date"), re.I),
    "private macOS home path": re.compile(
        re.escape(_joined("/", "Users", "/"))
    ),
    "private Linux home path": re.compile(re.escape(_joined("/", "home", "/"))),
    # RFC 2606/6761 reserved TLDs (.example/.invalid/.test) are documentation
    # fixtures by definition (e.g. the egress red-team corpus), not real
    # addresses — exempt them so shipped negative tests can exercise the
    # egress scanner without tripping this gate.
    "email address": re.compile(
        r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.(?!(?:example|invalid|test)\b)[A-Z]{2,}\b",
        re.I,
    ),
    "credential in URL": re.compile(r"https?://[^/\s:@]+:[^/\s@]+@"),
    "private IPv4 address": re.compile(
        r"(?<!\d)(?:10(?:\.\d{1,3}){3}|192\.168(?:\.\d{1,3}){2}|"
        r"172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2})(?!\d)"
    ),
    "private hostname": re.compile(
        r"(?i)\b[a-z0-9][a-z0-9.-]*\.(?:corp|internal|intranet)\b"
    ),
    "private key material": re.compile(
        _joined("BEGIN ", "(?:RSA |OPENSSH |EC )?", "PRIVATE KEY")
    ),
    "AWS access-key identifier": re.compile(
        _joined(r"\bAK", "IA[0-9A-Z]{16}", r"\b")
    ),
    "GitHub access token": re.compile(
        _joined(r"\bgh", r"[pousr]_[A-Za-z0-9_]{20,}\b")
    ),
    "JWT-like bearer token": re.compile(
        r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\."
        r"[A-Za-z0-9_-]{10,}\b"
    ),
}

DISCLOSURE_ONLY: dict[str, re.Pattern[str]] = {
    "employer disclosure": re.compile(rf"\b{_joined('U', 'BS')}\b", re.I),
    "external industry resource": re.compile(rf"\b{_joined('O', 'RX')}\b", re.I),
    "author acknowledgment": re.compile(rf"\b{_joined('Tal', 'eb')}\b", re.I),
}


# ---------------------------------------------------------------------------
# Schema-forbidden identifiers — exact keys and retired API shapes.
# ---------------------------------------------------------------------------

SCHEMA_FORBIDDEN_IDENTIFIERS: frozenset[str] = frozenset(
    {
        _joined("Story ", "Reference Number"),
        _joined("Insight ", "Scenario ID"),
        _joined("Institution ", "Name"),
        _joined("Date First ", "In Media"),
        _joined("Scenario Category ", "Name"),
        _joined("Event Type Level 1 ", "Name"),
        _joined("Root Cause Level 1 ", "Name"),
        _joined("predicted_", "event"),
        _joined("predicted_", "date"),
        _joined("news", "db"),
        _joined("grey", "swandb"),
        _joined("cm", "db", "_info_", "CM", "DBID"),
        _joined("cm", "db", "_info_", "AAName"),
        _joined("cm", "db", "_info_", "BCM"),
        _joined("cm", "db", "_info_", "CID"),
        _joined("CM", "DB", "ID"),
        _joined("Technology ", "BCM"),
        _joined("CID ", "handling"),
        _joined("CI Responsible ", "SDU/Stream"),
        _joined("news", "_event"),
        _joined("News", "Event"),
        _joined("news", "_body"),
        _joined("list_", "news"),
        _joined("add_", "news", "_event"),
        _joined("migrate_", "news"),
        _joined("NEWS", "_COLUMNS"),
        _joined("NEWS", "_COLUMN_NAMES"),
        _joined("event_", "type"),
        _joined("root_", "cause"),
        _joined("taxonomy_", "event_type"),
        _joined("bu_", "code"),
        _joined("public_", "news"),
    }
)

SCHEMA_FORBIDDEN_PREFIXES: frozenset[str] = frozenset(
    {
        _joined("GREY", "SWAN_"),
        _joined("cm", "db", "_info_"),
        _joined("CM", "DB"),
    }
)


def forbidden_schema_names(names: Iterable[str]) -> tuple[str, ...]:
    """Return names that collide with the schema-forbidden set or prefixes."""
    forbidden_cf = {item.casefold(): item for item in SCHEMA_FORBIDDEN_IDENTIFIERS}
    prefixes = tuple(SCHEMA_FORBIDDEN_PREFIXES)
    hits: list[str] = []
    for name in names:
        if not isinstance(name, str) or not name:
            continue
        if name.casefold() in forbidden_cf:
            hits.append(name)
            continue
        folded = name.casefold()
        if any(folded.startswith(prefix.casefold()) for prefix in prefixes):
            hits.append(name)
    return tuple(hits)


_IDENTIFIER_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/\\-]*")


def _identifier_signature(value: str) -> str:
    """Return a separator- and case-insensitive identifier signature."""
    return "".join(
        character for character in value.casefold() if character.isalnum()
    )


_SCHEMA_IDENTIFIER_SIGNATURES: frozenset[str] = frozenset(
    _identifier_signature(value)
    for value in (*SCHEMA_FORBIDDEN_IDENTIFIERS, *SCHEMA_FORBIDDEN_PREFIXES)
    # These generic field names are still forbidden by exact schema
    # introspection, but their natural-language forms are too broad for a
    # substring scan of prose (for example, an incident's "root cause").
    if value not in {
        _joined("news", "_event"),
        _joined("News", "Event"),
        _joined("event_", "type"),
        _joined("root_", "cause"),
    }
)


def scan_identifier_residue(text: str) -> list[str]:
    """Detect forbidden schema names inside snake/camel/path identifiers.

    Exact schema validation is intentionally stricter than this helper. This
    scanner is for release surfaces, where a retired name embedded in a file
    name, JSON key, or longer compatibility identifier must also fail closed.
    """
    if not isinstance(text, str) or not text:
        return []
    for token in _IDENTIFIER_TOKEN.findall(text):
        signature = _identifier_signature(token)
        if any(
            forbidden and forbidden in signature
            for forbidden in _SCHEMA_IDENTIFIER_SIGNATURES
        ):
            return ["legacy schema identifier"]
    return []


# ---------------------------------------------------------------------------
# Prompt-forbidden retired alias shapes (R4)
# Applied to prompt strings and planted tests, not the whole worktree —
# audit documentation may describe the failure mode without copying the key.
# ---------------------------------------------------------------------------

PROMPT_FORBIDDEN: dict[str, re.Pattern[str]] = {
    "leftover inventory alias key": re.compile(
        _joined("cm", "db", "_info_"), re.I
    ),
    "leftover CID handling key": re.compile(_joined("CID ", "handling"), re.I),
    "leftover BCM technology key": re.compile(
        _joined("Technology ", "BCM"), re.I
    ),
    "leftover CI ownership key": re.compile(
        _joined("CI Responsible ", "SDU/Stream"), re.I
    ),
    "consortium L1 event-type field": re.compile(
        _joined("Event Type Level 1 ", "Name"), re.I
    ),
    "consortium L1 root-cause field": re.compile(
        _joined("Root Cause Level 1 ", "Name"), re.I
    ),
}


def scan_prompt_residue(text: str) -> list[str]:
    """Return labels for leftover alias keys in a prompt or schema string."""
    if not isinstance(text, str) or not text:
        return []
    hits: list[str] = []
    for label, pattern in PROMPT_FORBIDDEN.items():
        if pattern.search(text):
            hits.append(label)
    return hits


# ---------------------------------------------------------------------------
# Host denylist — excluded-provider public sites.
# Scouting and research enforce this at admission and every redirect.
# Subdomains inherit the deny; ``not<name>.tld`` does not.
# ---------------------------------------------------------------------------

RESEARCH_HOST_DENY: frozenset[str] = frozenset(
    {
        _joined("o", "rx", ".org"),
        _joined("u", "bs", ".com"),
        _joined("u", "bs", ".ch"),
    }
)


def _hostname(value: str) -> str:
    raw = value.strip()
    if not raw:
        return ""
    if "://" in raw:
        try:
            parsed = urlparse(raw)
            host = (parsed.hostname or "").rstrip(".").lower()
        except ValueError:
            return ""
        return host
    return raw.split("/")[0].split(":")[0].rstrip(".").lower()


def host_is_denied(host_or_url: object) -> bool:
    """Return whether a host or URL belongs to an excluded public site.

    Public is not the same as cacheable. Callers use this in addition to their
    ordinary public-address and explicit-host allow-list checks.
    """
    if not isinstance(host_or_url, str) or not host_or_url.strip():
        return False
    host = _hostname(host_or_url)
    if not host:
        return False
    for denied in RESEARCH_HOST_DENY:
        if host == denied or host.endswith("." + denied):
            return True
    return False
