"""Credential-free endpoint references for browser-visible diagnostics."""

from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

_HTTP_URL = re.compile(r"https?://[^\s<>'\"]+", re.IGNORECASE)


def safe_endpoint_url(value: str) -> str:
    """Show the connection location without URL userinfo, query or fragment.

    This is display-only: callers retain their original connection URL.
    Malformed values are omitted rather than echoed in a diagnostic error.
    """
    if not isinstance(value, str) or any(ord(c) < 32 or ord(c) == 127 for c in value):
        return ""
    try:
        parsed = urlsplit(value)
        hostname, port = parsed.hostname, parsed.port
        if parsed.scheme not in {"http", "https"} or not hostname:
            return ""
        authority = f"[{hostname}]" if ":" in hostname else hostname
        if port is not None:
            authority += f":{port}"
        return urlunsplit((parsed.scheme, authority, parsed.path, "", ""))
    except (ValueError, UnicodeError):
        return ""


def safe_endpoint_message(value: str | None) -> str | None:
    """Strip URL credentials from probe errors which may echo request URLs."""
    if value is None:
        return None
    return _HTTP_URL.sub(lambda match: safe_endpoint_url(match.group()) or "[invalid endpoint]", str(value))
