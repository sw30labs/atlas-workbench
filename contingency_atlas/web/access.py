"""Single-tenant authorization for an explicitly configured identity proxy.

The application still binds loopback. Proxy mode additionally requires a
fresh signature over identity, permissions and the exact request. It does
not trust forwarding headers, browser-supplied identities or loopback alone.
The default local mode preserves the existing local-operator workflow.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from contingency_atlas.config import get_settings

ROLES = frozenset({"viewer", "analyst", "reviewer", "admin"})
CAPABILITIES = frozenset({"export"})
MAX_REQUEST_AGE_SECONDS = 60
MAX_FUTURE_SKEW_SECONDS = 5
MAX_LIVE_NONCES = 100_000
_NONCE = re.compile(r"[A-Za-z0-9_-]{22,128}\Z")
_DIGEST = re.compile(r"[a-f0-9]{64}\Z")
_JOB_PATH = re.compile(r"/api/jobs/web-[a-f0-9]{8}(?:[a-f0-9]{24})?\Z")
PUBLIC_SHELL_PATHS = frozenset({
    "/", "/index.html", "/favicon.svg", "/experience.css", "/experience.js", "/costs",
})
READ_PATHS = frozenset({
    "/api/scenarios",
    "/api/health", "/api/data", "/api/about", "/api/costs/summary", "/api/tokens/usage",
    "/api/eval/reports", "/api/review/pending", "/api/review/history",
    "/api/scenarios/recommendation", "/api/frameworks/mitre-atlas/status",
    "/api/frameworks/mitre-atlas/techniques", "/api/compounds/history",
    "/api/context", "/api/context/compare", "/api/jobs", "/api/access",
    "/api/diagnostics", "/api/integrations",
})
ANALYST_READ_PATHS = frozenset({"/api/intake/sources", "/api/omlx"})
ADMIN_READ_PATHS = frozenset({"/api/settings", "/api/readiness"})
ANALYST_POST_PATHS = frozenset({
    "/api/run", "/api/ingest/convert", "/api/intake/prepare", "/api/intake/submit",
    "/api/review/adopt-refs", "/api/review/detach-refs", "/api/review/scan-duplicates",
    "/api/frameworks/mitre-atlas/proposals", "/api/context/save", "/api/context/archive",
    "/api/context/baseline", "/api/imports/preview", "/api/imports/submit",
    "/api/jobs/cancel", "/api/jobs/retry",
})
REVIEWER_POST_PATHS = frozenset({
    "/api/review/decide", "/api/review/decide-batch", "/api/review/rate-memo",
})
ADMIN_POST_PATHS = frozenset({
    "/api/settings/models", "/api/settings/frameworks/mitre-atlas/import",
    "/api/settings/frameworks/mitre-atlas/activate", "/api/context/enable",
    "/api/operations/backup", "/api/operations/restore", "/api/jobs/recover",
})
EXPORT_POST_PATHS = frozenset({
    "/api/exports/preview", "/api/exports/download",
    "/api/integrations/preview", "/api/integrations/download",
})
INTEGRATION_POST_PATHS = frozenset({"/api/integrations/ack", "/api/integrations/retry"})


class AccessError(ValueError):
    def __init__(self, message: str, status: int = 401):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Principal:
    subject: str
    role: str
    capabilities: frozenset[str] = frozenset()
    mode: str = "proxy"

    @property
    def actor(self) -> str:
        return self.subject

    def public_dict(self) -> dict[str, Any]:
        return {"subject": self.subject, "role": self.role,
                "capabilities": sorted(self.capabilities), "mode": self.mode}


@dataclass(frozen=True)
class AccessConfig:
    mode: str = "local"
    public_origin: str = ""
    secret: bytes = field(default=b"", repr=False)
    nonce_db: Path | None = None


def _canonical_origin(value: str, *, https_only: bool = True) -> str:
    if not isinstance(value, str):
        raise AccessError("Invalid canonical application origin", 503)
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise AccessError("Invalid canonical application origin", 503) from exc
    if (
        not isinstance(value, str) or value != value.strip()
        or any(ord(c) < 33 or ord(c) == 127 for c in value)
        or parsed.scheme not in ({"https"} if https_only else {"http", "https"})
        or not host or parsed.username is not None or parsed.password is not None
        or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
    ):
        raise AccessError("Configure one canonical HTTPS application origin", 503)
    try:
        host = host.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise AccessError("Invalid canonical application hostname", 503) from exc
    if ":" in host:
        host = f"[{host}]"
    default_port = 443 if parsed.scheme == "https" else 80
    return f"{parsed.scheme}://{host}" + (f":{port}" if port not in {None, default_port} else "")


def load_access_config() -> AccessConfig:
    mode = os.environ.get("ATLAS_ACCESS_MODE", "local").strip().lower()
    if mode == "local":
        return AccessConfig()
    if mode != "proxy":
        raise AccessError("ATLAS_ACCESS_MODE must be local or proxy", 503)
    origin = _canonical_origin(os.environ.get("ATLAS_PUBLIC_ORIGIN", ""))
    raw_secret = os.environ.get("ATLAS_PROXY_HMAC_SECRET", "")
    if len(raw_secret.encode()) < 32 or raw_secret != raw_secret.strip():
        raise AccessError("Proxy mode requires a secret of at least 32 bytes", 503)
    nonce_path = os.environ.get("ATLAS_PROXY_NONCE_DB")
    path = (Path(nonce_path).expanduser().resolve() if nonce_path else
            Path(get_settings().scenario_db_path).parent / "generated" / "security" / "proxy_nonces.sqlite3")
    return AccessConfig("proxy", origin, raw_secret.encode(), path)


def _header(handler: Any, name: str, *, required: bool = True) -> str:
    headers = handler.headers
    if hasattr(headers, "get_all"):
        values = headers.get_all(name) or []
    else:
        values = [value for key, value in headers.items() if key.lower() == name.lower()]
    if len(values) > 1:
        raise AccessError("Duplicate identity headers are not accepted")
    if not values:
        if required:
            raise AccessError("Authenticated proxy identity is required")
        return ""
    value = values[0]
    if not isinstance(value, str) or len(value) > 512 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise AccessError("Invalid identity header")
    return value


def _loopback_authority(authority: str) -> bool:
    try:
        parsed = urlsplit("//" + authority)
        _ = parsed.port
        if (any(ord(c) < 33 or ord(c) == 127 for c in authority)
            or parsed.username is not None or parsed.password is not None
            or parsed.path or parsed.query or parsed.fragment or not parsed.hostname):
            return False
        return parsed.hostname.lower() == "localhost" or ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        return False


def peer_request_allowed(handler: Any, *, config: AccessConfig | None = None) -> bool:
    """Keep the backend loopback-only; optionally accept the proxy's public Host."""
    config = config or load_access_config()
    try:
        if not ipaddress.ip_address(handler.client_address[0]).is_loopback:
            return False
        host = _header(handler, "Host").strip().lower()
        if _loopback_authority(host):
            return True
        return config.mode == "proxy" and _canonical_origin("https://" + host) == config.public_origin
    except (AccessError, ValueError, IndexError):
        return False


def allowed_origin(handler: Any, *, config: AccessConfig | None = None) -> bool:
    """Mutation gate. Proxy mode requires the browser's original public Origin."""
    config = config or load_access_config()
    if not peer_request_allowed(handler, config=config):
        return False
    try:
        origin = _header(handler, "Origin", required=False)
        if config.mode == "proxy":
            return bool(origin) and _canonical_origin(origin) == config.public_origin
        if not origin:
            return True
        parsed = urlsplit(origin)
        _canonical_origin(origin, https_only=False)
        return parsed.netloc.lower() == _header(handler, "Host").strip().lower()
    except AccessError:
        return False


def _request_target(target: str) -> str:
    if (
        not isinstance(target, str) or not target.startswith("/") or target.startswith("//")
        or "#" in target or len(target) > 16_384 or not target.isascii()
        or any(ord(c) < 33 or ord(c) == 127 for c in target)
    ):
        raise AccessError("Invalid signed request target", 400)
    return target


def permitted(principal: Principal, method: str, path: str) -> bool:
    """Explicit endpoint permission table; unknown roles and routes deny."""
    if principal.role not in ROLES or not principal.capabilities <= CAPABILITIES:
        return False
    path = path.split("?", 1)[0]
    if method == "GET":
        if path in READ_PATHS or _JOB_PATH.fullmatch(path):
            return True
        if path in ANALYST_READ_PATHS:
            return principal.role in {"analyst", "admin"}
        return path in ADMIN_READ_PATHS and principal.role == "admin"
    if method == "POST":
        if path in ANALYST_POST_PATHS:
            return principal.role in {"analyst", "admin"}
        if path in REVIEWER_POST_PATHS:
            return principal.role in {"reviewer", "admin"}
        if path in ADMIN_POST_PATHS:
            return principal.role == "admin"
        if path in EXPORT_POST_PATHS:
            return principal.role == "admin" or "export" in principal.capabilities
        if path in INTEGRATION_POST_PATHS:
            return principal.role == "admin" or (
                principal.role == "analyst" and "export" in principal.capabilities
            )
    return False


def _claims(subject: str, role: str, capabilities) -> tuple[str, str, frozenset[str]]:
    if not isinstance(subject, str) or not 1 <= len(subject) <= 160 or subject != subject.strip() or any(ord(c) < 32 or ord(c) == 127 for c in subject):
        raise AccessError("Proxy identity subject must contain 1–160 characters")
    capabilities = frozenset(capabilities)
    if role not in ROLES or not capabilities <= CAPABILITIES:
        raise AccessError("Proxy identity contains an unknown role or capability", 403)
    return subject, role, capabilities


def _signed_bytes(*, origin: str, subject: str, role: str, capabilities: frozenset[str],
                  method: str, target: str, timestamp: str, nonce: str, body_hash: str) -> bytes:
    return json.dumps([
        "atlas-proxy-v1", origin, subject, role, sorted(capabilities),
        method, target, timestamp, nonce, body_hash,
    ], ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def sign_proxy_headers(*, secret: str | bytes, public_origin: str, subject: str, role: str,
                       method: str, target: str, body: bytes = b"", capabilities=(),
                       timestamp: int | None = None, nonce: str | None = None) -> dict[str, str]:
    """Server-side adapter helper. Never put this key or function in browser code."""
    secret = secret.encode() if isinstance(secret, str) else secret
    if not isinstance(secret, bytes) or len(secret) < 32:
        raise AccessError("Proxy signing secret must contain at least 32 bytes", 503)
    origin = _canonical_origin(public_origin)
    subject, role, capabilities = _claims(subject, role, capabilities)
    method = method.upper()
    if method not in {"GET", "POST"}:
        raise AccessError("Unsupported signed method", 400)
    target = _request_target(target)
    timestamp = str(int(time.time()) if timestamp is None else timestamp)
    nonce = nonce or secrets.token_urlsafe(24)
    if not _NONCE.fullmatch(nonce):
        raise AccessError("Nonce must contain at least 128 bits of unpredictable data")
    body_hash = hashlib.sha256(body).hexdigest()
    signature = hmac.new(secret, _signed_bytes(
        origin=origin, subject=subject, role=role, capabilities=capabilities,
        method=method, target=target, timestamp=timestamp, nonce=nonce, body_hash=body_hash,
    ), hashlib.sha256).hexdigest()
    return {
        "X-Atlas-Subject": subject, "X-Atlas-Role": role,
        "X-Atlas-Capabilities": ",".join(sorted(capabilities)),
        "X-Atlas-Timestamp": timestamp, "X-Atlas-Nonce": nonce,
        "X-Atlas-Body-SHA256": body_hash, "X-Atlas-Signature": "v1=" + signature,
    }


class NonceStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def consume(self, nonce: str, *, expires_at: int, now: int) -> None:
        """One successful use across all workers; persistence failure denies."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
            os.close(descriptor)
            with closing(sqlite3.connect(self.path, timeout=5)) as connection:
                # Do not change journal mode here: simultaneous WAL upgrades
                # need a lock escalation which can return SQLITE_BUSY without
                # honoring the busy timeout. This all-write ledger works in
                # either existing mode; fresh stores retain SQLite's default.
                connection.execute("PRAGMA synchronous=FULL")
                # Serialize bootstrap as well as consumption. Acquiring the
                # write reservation first avoids a schema/read-to-write race.
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("CREATE TABLE IF NOT EXISTS proxy_nonces (nonce_hash TEXT PRIMARY KEY, expires_at INTEGER NOT NULL)")
                connection.execute("DELETE FROM proxy_nonces WHERE expires_at < ?", (now,))
                if connection.execute("SELECT COUNT(*) FROM proxy_nonces").fetchone()[0] >= MAX_LIVE_NONCES:
                    raise AccessError("Proxy replay protection is temporarily at capacity", 503)
                try:
                    connection.execute("INSERT INTO proxy_nonces VALUES (?,?)", (
                        hashlib.sha256(nonce.encode()).hexdigest(), expires_at,
                    ))
                except sqlite3.IntegrityError as exc:
                    raise AccessError("Signed request has already been used") from exc
                connection.commit()
        except (OSError, sqlite3.Error) as exc:
            raise AccessError("Proxy replay protection is unavailable", 503) from exc


def authorize_request(handler: Any, path: str | None = None, method: str | None = None,
                      body_bytes: bytes | None = None, *, config: AccessConfig | None = None,
                      nonce_store: NonceStore | None = None, now: int | None = None) -> Principal:
    """Authorize GET before dispatch and POST after reading its exact raw bytes.

    ``path`` may be the server's routing path; the signature always covers
    ``handler.path`` including its untouched query string. Call once per request.
    """
    config = config or load_access_config()
    actual_method = getattr(handler, "command", None)
    if method is not None and actual_method is not None and method != actual_method:
        raise AccessError("Signed method does not match the dispatched request", 400)
    method = method or actual_method
    target = _request_target(getattr(handler, "path", None) or path or "")
    if path is not None and path.split("?", 1)[0] != target.split("?", 1)[0]:
        raise AccessError("Signed target does not match the dispatched path", 400)
    if config.mode == "local":
        return Principal("local-operator", "admin", frozenset({"export"}), "local")
    if (config.mode != "proxy" or not isinstance(config.secret, bytes)
        or len(config.secret) < 32 or not config.nonce_db
        or _canonical_origin(config.public_origin) != config.public_origin):
        raise AccessError("Proxy authorization is not configured", 503)
    if not peer_request_allowed(handler, config=config):
        raise AccessError("The signed proxy must connect through the loopback backend", 403)
    if method == "GET" and target.split("?", 1)[0] in PUBLIC_SHELL_PATHS:
        return Principal("anonymous", "viewer", mode="public")
    if method not in {"GET", "POST"}:
        raise AccessError("Method is not permitted", 403)
    if method == "POST" and body_bytes is None:
        raise AccessError("Authorization requires the exact request body", 400)
    if method == "POST" and not allowed_origin(handler, config=config):
        raise AccessError("Request Origin does not match this application", 403)
    body = body_bytes if body_bytes is not None else b""
    if method == "GET" and body:
        raise AccessError("GET request bodies are not accepted", 400)
    subject = _header(handler, "X-Atlas-Subject")
    role = _header(handler, "X-Atlas-Role")
    capabilities_raw = _header(handler, "X-Atlas-Capabilities", required=False)
    capabilities_list = capabilities_raw.split(",") if capabilities_raw else []
    if capabilities_list != sorted(set(capabilities_list)):
        raise AccessError("Capabilities must use the canonical sorted representation")
    subject, role, capabilities = _claims(subject, role, capabilities_list)
    timestamp_raw = _header(handler, "X-Atlas-Timestamp")
    try:
        timestamp = int(timestamp_raw)
    except ValueError as exc:
        raise AccessError("Invalid signed timestamp") from exc
    clock = int(time.time()) if now is None else int(now)
    if str(timestamp) != timestamp_raw or timestamp < clock - MAX_REQUEST_AGE_SECONDS or timestamp > clock + MAX_FUTURE_SKEW_SECONDS:
        raise AccessError("Signed request is expired or outside the allowed clock window")
    nonce = _header(handler, "X-Atlas-Nonce")
    if not _NONCE.fullmatch(nonce):
        raise AccessError("Invalid signed nonce")
    body_hash = _header(handler, "X-Atlas-Body-SHA256")
    if not _DIGEST.fullmatch(body_hash) or not hmac.compare_digest(body_hash, hashlib.sha256(body).hexdigest()):
        raise AccessError("Signed request body does not match")
    signature = _header(handler, "X-Atlas-Signature")
    expected = "v1=" + hmac.new(config.secret, _signed_bytes(
        origin=config.public_origin, subject=subject, role=role, capabilities=capabilities,
        method=method, target=target, timestamp=timestamp_raw, nonce=nonce, body_hash=body_hash,
    ), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise AccessError("Invalid proxy request signature")
    principal = Principal(subject, role, capabilities)
    if not permitted(principal, method, target):
        raise AccessError("This role does not permit the requested action", 403)
    (nonce_store or NonceStore(config.nonce_db)).consume(
        nonce, expires_at=timestamp + MAX_REQUEST_AGE_SECONDS + MAX_FUTURE_SKEW_SECONDS, now=clock,
    )
    return principal


def bind_actor(body: dict[str, Any], principal: Principal) -> dict[str, Any]:
    """Bind mutation attribution to the verified subject in shared mode."""
    if principal.mode != "proxy":
        return dict(body)
    return {**body, "actor": principal.actor, "reviewer": principal.actor}


def proxy_adapter_headers(client_headers: Mapping[str, str], *, secret: str | bytes,
                          public_origin: str, subject: str, role: str, method: str,
                          target: str, body: bytes = b"", capabilities=(),
                          backend_authority: str = "127.0.0.1:8080") -> dict[str, str]:
    """Adapter building block, called only after the proxy authenticates its user.

    It strips client identity/auth/cookie headers and preserves the client's
    original Origin. Replacing Origin with the configured one would defeat CSRF.
    """
    if not _loopback_authority(backend_authority):
        raise AccessError("The adapter backend must use a loopback authority", 503)
    forwarded = {key: value for key, value in client_headers.items()
                 if key.lower() in {"accept", "content-type", "origin"}}
    forwarded.update(sign_proxy_headers(
        secret=secret, public_origin=public_origin, subject=subject, role=role,
        method=method, target=target, body=body, capabilities=capabilities,
    ))
    forwarded["Host"] = backend_authority
    forwarded["Content-Length"] = str(len(body))
    return forwarded
