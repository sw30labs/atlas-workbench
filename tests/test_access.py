"""Offline signed-proxy authorization, using synthetic identities and temp stores."""

import hashlib
import multiprocessing
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from email.message import Message
from types import SimpleNamespace

import pytest

from workbench_atlas.web.access import (
    AccessConfig, AccessError, NonceStore, Principal, allowed_origin,
    authorize_request, bind_actor, load_access_config, peer_request_allowed,
    permitted, proxy_adapter_headers, sign_proxy_headers,
)

NOW = 1_800_000_000
SECRET = b"synthetic-test-signing-material-only-0001"
ORIGIN = "https://atlas.example.org"


@pytest.fixture
def config(tmp_path):
    return AccessConfig("proxy", ORIGIN, SECRET, tmp_path / "nonces.sqlite3")


def request(config, *, method="GET", target="/api/data", body=b"", role="viewer",
            capabilities=(), subject="synthetic-subject", timestamp=NOW, nonce=None):
    headers = Message()
    headers["Host"] = "127.0.0.1:8080"
    headers["Origin"] = ORIGIN
    for key, value in sign_proxy_headers(
        secret=config.secret, public_origin=config.public_origin,
        subject=subject, role=role, method=method, target=target, body=body,
        capabilities=capabilities, timestamp=timestamp, nonce=nonce,
    ).items():
        headers[key] = value
    return SimpleNamespace(headers=headers, command=method, path=target,
                           client_address=("127.0.0.1", 32100))


def test_local_mode_preserves_existing_operator_workflow():
    handler = SimpleNamespace(command="POST", path="/api/run")
    principal = authorize_request(handler, config=AccessConfig())
    assert principal.mode == "local" and principal.role == "admin"
    assert bind_actor({"actor": "Chosen local operator"}, principal)["actor"] == "Chosen local operator"


def test_signed_get_accepts_query_and_binds_immutable_subject(config):
    handler = request(config, target="/api/data?kind=evidence&limit=10")
    principal = authorize_request(handler, "/api/data", "GET", config=config, now=NOW)
    assert principal.subject == "synthetic-subject"
    assert principal.public_dict()["role"] == "viewer"
    assert bind_actor({"actor": "forged", "reviewer": "forged", "value": 1}, principal) == {
        "actor": "synthetic-subject", "reviewer": "synthetic-subject", "value": 1,
    }


@pytest.mark.parametrize("field,value", [
    ("X-Atlas-Subject", "another-subject"), ("X-Atlas-Role", "admin"),
    ("X-Atlas-Capabilities", "export"), ("X-Atlas-Nonce", "a" * 32),
    ("X-Atlas-Timestamp", str(NOW - 1)), ("X-Atlas-Signature", "v1=" + "0" * 64),
])
def test_tampered_claims_do_not_authorize(config, field, value):
    handler = request(config)
    handler.headers.replace_header(field, value)
    with pytest.raises(AccessError, match="signature"):
        authorize_request(handler, config=config, now=NOW)
    assert not config.nonce_db.exists()


def test_exact_method_path_query_and_body_are_bound(config):
    original = b'{"phase": "phase4"}'
    handler = request(config, method="POST", target="/api/run?attempt=1", body=original, role="analyst")
    with pytest.raises(AccessError, match="body does not match"):
        authorize_request(handler, body_bytes=b'{"phase":"phase4"}', config=config, now=NOW)
    handler.path = "/api/run?attempt=2"
    with pytest.raises(AccessError, match="signature"):
        authorize_request(handler, body_bytes=original, config=config, now=NOW)
    handler.path = "/api/run?attempt=1"
    with pytest.raises(AccessError, match="dispatched path"):
        authorize_request(handler, "/api/settings/models", body_bytes=original, config=config, now=NOW)
    with pytest.raises(AccessError, match="dispatched request"):
        authorize_request(handler, method="GET", body_bytes=original, config=config, now=NOW)
    assert authorize_request(handler, body_bytes=original, config=config, now=NOW).role == "analyst"


def test_signature_is_bound_to_configured_origin(config):
    handler = request(config)
    other = AccessConfig("proxy", "https://other.example.org", SECRET, config.nonce_db)
    with pytest.raises(AccessError, match="signature"):
        authorize_request(handler, config=other, now=NOW)


@pytest.mark.parametrize("timestamp", [NOW - 61, NOW + 6])
def test_expired_and_future_assertions_fail(config, timestamp):
    with pytest.raises(AccessError, match="clock window"):
        authorize_request(request(config, timestamp=timestamp), config=config, now=NOW)


def test_replay_is_rejected_by_a_fresh_store_instance(config):
    handler = request(config)
    authorize_request(handler, config=config, now=NOW)
    with pytest.raises(AccessError, match="already been used"):
        authorize_request(handler, config=config, nonce_store=NonceStore(config.nonce_db), now=NOW)
    with sqlite3.connect(config.nonce_db) as connection:
        saved = connection.execute("SELECT nonce_hash FROM proxy_nonces").fetchone()[0]
    assert saved == hashlib.sha256(handler.headers["X-Atlas-Nonce"].encode()).hexdigest()
    assert config.nonce_db.stat().st_mode & 0o077 == 0


@pytest.mark.parametrize("cold_start", range(6))
def test_concurrent_replay_has_only_one_winner(config, cold_start):
    handler = request(config)
    barrier = threading.Barrier(12)
    def authorize(_index):
        barrier.wait(timeout=5)
        try:
            authorize_request(handler, config=config, now=NOW)
            return "accepted"
        except AccessError as exc:
            return str(exc)
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(authorize, range(12)))
    assert results.count("accepted") == 1
    assert all(item in {"accepted", "Signed request has already been used"} for item in results), results


def test_cold_nonce_initialization_waits_for_existing_writer(tmp_path, monkeypatch):
    path = tmp_path / "cold-nonces.sqlite3"
    connected = threading.Event()
    connect = sqlite3.connect
    writer = connect(path)
    # The first nonce table has not been created. A concurrent writer holds
    # SQLite's reservation while the other request starts initialization.
    writer.execute("PRAGMA user_version=1")
    writer.execute("BEGIN IMMEDIATE")
    def traced_connect(*args, **kwargs):
        connection = connect(*args, **kwargs)
        connection.set_trace_callback(lambda sql: connected.set() if sql in {
            "PRAGMA journal_mode=WAL", "BEGIN IMMEDIATE",
        } else None)
        return connection
    monkeypatch.setattr(sqlite3, "connect", traced_connect)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(NonceStore(path).consume, "synthetic-writer-contention", expires_at=NOW + 60, now=NOW)
        try:
            assert connected.wait(timeout=2)
            # A WAL upgrade fails immediately here despite timeout=5; an
            # initial write reservation waits for the peer, as it should.
            with pytest.raises(FutureTimeoutError):
                future.result(timeout=0.1)
        finally:
            writer.rollback()
            writer.close()
        assert future.result(timeout=5) is None
    with connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM proxy_nonces").fetchone()[0] == 1


def _nonce_process_worker(path, index, barrier, results):
    try:
        barrier.wait(timeout=10)
        NonceStore(path).consume(f"synthetic-distinct-{index}", expires_at=NOW + 60, now=NOW)
        barrier.wait(timeout=10)
        try:
            NonceStore(path).consume("synthetic-shared-process-nonce", expires_at=NOW + 60, now=NOW)
            shared = "accepted"
        except AccessError as exc:
            shared = str(exc)
        results.put({"distinct": "accepted", "shared": shared})
    except Exception as exc:
        results.put({"error": str(exc)})


def test_fresh_ledger_concurrency_is_safe_across_spawned_processes(tmp_path):
    context = multiprocessing.get_context("spawn")
    path = tmp_path / "cross-process-nonces.sqlite3"
    barrier, results = context.Barrier(4), context.Queue()
    processes = [context.Process(target=_nonce_process_worker, args=(path, index, barrier, results))
                 for index in range(4)]
    try:
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=15)
        assert all(process.exitcode == 0 for process in processes)
        output = [results.get(timeout=2) for _ in processes]
        assert all(item.get("distinct") == "accepted" for item in output), output
        assert sum(item.get("shared") == "accepted" for item in output) == 1
        assert all(item["shared"] in {"accepted", "Signed request has already been used"} for item in output), output
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
            if process.pid is not None:
                process.join(timeout=2)
        results.close()
        results.join_thread()
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM proxy_nonces").fetchone()[0] == 5


def test_existing_wal_ledger_retains_mode_and_used_nonce(tmp_path):
    path = tmp_path / "existing-wal.sqlite3"
    NonceStore(path).consume("synthetic-retained-nonce", expires_at=NOW + 60, now=NOW)
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    barrier = threading.Barrier(8)
    def consume(index):
        barrier.wait(timeout=5)
        NonceStore(path).consume(f"synthetic-new-{index}", expires_at=NOW + 60, now=NOW)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(consume, range(8)))
    with pytest.raises(AccessError, match="already been used"):
        NonceStore(path).consume("synthetic-retained-nonce", expires_at=NOW + 60, now=NOW)
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("SELECT COUNT(*) FROM proxy_nonces").fetchone()[0] == 9


def test_unavailable_nonce_store_fails_closed(config):
    config.nonce_db.write_text("synthetic corruption")
    with pytest.raises(AccessError, match="replay protection is unavailable") as error:
        authorize_request(request(config), config=config, now=NOW)
    assert error.value.status == 503


def test_nonce_cleanup_retains_live_requests(config):
    store = NonceStore(config.nonce_db)
    store.consume("expired-nonce", expires_at=NOW - 1, now=NOW - 2)
    store.consume("live-nonce", expires_at=NOW + 60, now=NOW)
    with sqlite3.connect(config.nonce_db) as connection:
        assert connection.execute("SELECT COUNT(*) FROM proxy_nonces").fetchone()[0] == 1


@pytest.mark.parametrize("role,method,path,capabilities,expected", [
    ("viewer", "GET", "/api/data", (), True),
    ("viewer", "GET", "/api/context", (), True),
    ("viewer", "GET", "/api/diagnostics", (), True),
    ("viewer", "GET", "/api/settings", (), False),
    ("viewer", "POST", "/api/run", (), False),
    ("analyst", "POST", "/api/context/save", (), True),
    ("analyst", "POST", "/api/review/decide", (), False),
    ("reviewer", "POST", "/api/review/decide-batch", (), True),
    ("reviewer", "POST", "/api/intake/prepare", (), False),
    ("reviewer", "POST", "/api/run", (), False),
    ("admin", "POST", "/api/settings/models", (), True),
    ("analyst", "POST", "/api/context/enable", (), False),
    ("admin", "POST", "/api/context/enable", (), True),
    ("admin", "POST", "/api/exports/download", (), True),
    ("reviewer", "POST", "/api/exports/download", (), False),
    ("viewer", "POST", "/api/exports/download", ("export",), True),
    ("analyst", "POST", "/api/integrations/ack", (), False),
    ("viewer", "POST", "/api/integrations/ack", ("export",), False),
    ("analyst", "POST", "/api/integrations/ack", ("export",), True),
    ("admin", "DELETE", "/api/data", (), False),
    ("admin", "POST", "/api/unknown", (), False),
    ("admin", "GET", "/api/jobs/recover", (), False),
    ("admin", "GET", "/api/jobs/web-1234abcd", (), True),
    ("admin", "GET", "/api/jobs/web-" + "a" * 32, (), True),
    ("owner", "GET", "/api/data", (), False),
    ("admin", "GET", "/api/data", ("all",), False),
])
def test_explicit_role_permissions(role, method, path, capabilities, expected):
    assert permitted(Principal("synthetic-subject", role, frozenset(capabilities)), method, path) is expected


def test_valid_signature_cannot_override_role_permissions(config):
    handler = request(config, method="POST", target="/api/review/decide", body=b"{}", role="analyst")
    with pytest.raises(AccessError, match="role does not permit") as error:
        authorize_request(handler, body_bytes=b"{}", config=config, now=NOW)
    assert error.value.status == 403


def test_loopback_alone_never_authenticates_an_api(config):
    handler = request(config)
    for name in list(handler.headers):
        if name.startswith("X-Atlas-"):
            del handler.headers[name]
    with pytest.raises(AccessError, match="identity is required"):
        authorize_request(handler, config=config, now=NOW)
    handler.path = "/"
    assert authorize_request(handler, config=config, now=NOW).mode == "public"


def test_nonloopback_peer_and_unrecognized_host_are_rejected(config):
    handler = request(config)
    handler.client_address = ("192.0.2.10", 32100)  # RFC 5737 documentation-only peer.
    assert not peer_request_allowed(handler, config=config)
    with pytest.raises(AccessError, match="loopback backend"):
        authorize_request(handler, config=config, now=NOW)
    handler.client_address = ("127.0.0.1", 32100)
    handler.headers.replace_header("Host", "atlas.example.org")
    assert peer_request_allowed(handler, config=config)
    handler.headers.replace_header("Host", "untrusted.example.org")
    assert not peer_request_allowed(handler, config=config)


@pytest.mark.parametrize("origin", [None, "https://untrusted.example.org", "http://atlas.example.org"])
def test_post_requires_unchanged_matching_public_origin(config, origin):
    handler = request(config, method="POST", target="/api/run", body=b"{}", role="analyst")
    del handler.headers["Origin"]
    if origin:
        handler.headers["Origin"] = origin
    assert not allowed_origin(handler, config=config)
    with pytest.raises(AccessError, match="Origin"):
        authorize_request(handler, body_bytes=b"{}", config=config, now=NOW)


def test_duplicate_headers_and_missing_raw_body_fail(config):
    handler = request(config)
    handler.headers["x-atlas-role"] = "admin"
    with pytest.raises(AccessError, match="Duplicate"):
        authorize_request(handler, config=config, now=NOW)
    post = request(config, method="POST", target="/api/run", role="analyst")
    with pytest.raises(AccessError, match="exact request body"):
        authorize_request(post, config=config, now=NOW)


def test_adapter_strips_spoofed_identity_and_preserves_original_origin(config):
    output = proxy_adapter_headers(
        {"X-Atlas-Subject": "forged", "X-Atlas-Signature": "forged", "Cookie": "synthetic-session",
         "Authorization": "synthetic-browser-header", "Origin": "https://untrusted.example.org",
         "Content-Type": "application/json", "Host": "forged.example.org"},
        secret=SECRET, public_origin=ORIGIN, subject="verified-session-subject", role="analyst",
        method="POST", target="/api/run", body=b"{}",
    )
    assert output["X-Atlas-Subject"] == "verified-session-subject"
    assert output["Origin"] == "https://untrusted.example.org"
    assert output["Host"] == "127.0.0.1:8080" and output["Content-Length"] == "2"
    assert "Cookie" not in output and "Authorization" not in output
    handler = request(config, method="POST", target="/api/run", role="analyst")
    handler.headers = output
    with pytest.raises(AccessError, match="Origin"):
        authorize_request(handler, body_bytes=b"{}", config=config)


def test_proxy_configuration_requires_https_and_private_server_key(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_ACCESS_MODE", "proxy")
    monkeypatch.setenv("ATLAS_PUBLIC_ORIGIN", "http://atlas.example.org")
    monkeypatch.setenv("ATLAS_PROXY_HMAC_SECRET", SECRET.decode())
    monkeypatch.setenv("ATLAS_PROXY_NONCE_DB", str(tmp_path / "nonces.sqlite3"))
    with pytest.raises(AccessError, match="HTTPS"):
        load_access_config()
    monkeypatch.setenv("ATLAS_PUBLIC_ORIGIN", ORIGIN)
    monkeypatch.setenv("ATLAS_PROXY_HMAC_SECRET", "short")
    with pytest.raises(AccessError, match="32 bytes"):
        load_access_config()
    monkeypatch.setenv("ATLAS_PROXY_HMAC_SECRET", SECRET.decode())
    config = load_access_config()
    assert config.mode == "proxy" and config.nonce_db == tmp_path / "nonces.sqlite3"
    assert SECRET.decode() not in repr(config)
