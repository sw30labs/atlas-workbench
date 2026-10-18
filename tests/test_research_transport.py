"""Offline resource-limit, DNS-pinning and transport-deadline regressions."""

from __future__ import annotations

import http.server
import ipaddress
import multiprocessing
import socket
import threading
import time
from contextlib import contextmanager

import httpx
import pytest

from contingency_atlas import research
from contingency_atlas import research_transport as transport_module
from contingency_atlas.research_transport import PinnedPublicTransport, fetch_public


class CountingBody(httpx.SyncByteStream):
    def __init__(self, total=4096, chunk_size=32):
        self.total = total
        self.chunk_size = chunk_size
        self.received = 0
        self.closed = False

    def __iter__(self):
        while self.received < self.total:
            chunk = b"x" * min(self.chunk_size, self.total - self.received)
            self.received += len(chunk)
            yield chunk

    def close(self):
        self.closed = True


def test_page_stream_stops_at_limit_and_closes_response():
    body = CountingBody()
    transport = httpx.MockTransport(lambda request: httpx.Response(
        200, headers={"content-type": "text/plain"}, stream=body,
    ))
    with pytest.raises(ValueError, match="byte limit"):
        research.fetch_page("https://example.org/page", max_bytes=64, transport=transport)
    assert body.received == 96
    assert body.closed


def test_declared_oversize_is_rejected_before_reading():
    body = CountingBody()
    transport = httpx.MockTransport(lambda request: httpx.Response(
        200, headers={"content-type": "text/plain", "content-length": "4096"}, stream=body,
    ))
    with pytest.raises(ValueError, match="byte limit"):
        research.fetch_page("https://example.org/page", max_bytes=64, transport=transport)
    assert body.received == 0
    assert body.closed


@pytest.mark.parametrize("encoding", ["gzip", "br", "deflate"])
def test_compressed_body_is_rejected_without_reading_or_decoding(encoding):
    body = CountingBody()
    def respond(request):
        assert request.headers["accept-encoding"] == "identity"
        return httpx.Response(
            200, headers={"content-type": "text/plain", "content-encoding": encoding},
            stream=body,
        )
    with pytest.raises(ValueError, match="content-encoding"):
        research.fetch_page("https://example.org/page", transport=httpx.MockTransport(respond))
    assert body.received == 0
    assert body.closed


def test_redirect_body_is_never_consumed():
    body = CountingBody()
    seen = []
    def respond(request):
        seen.append(str(request.url))
        if len(seen) == 1:
            return httpx.Response(302, headers={"location": "/final"}, stream=body)
        return httpx.Response(200, headers={"content-type": "text/plain"}, text="ok")
    page = research.fetch_page("https://example.org/start", transport=httpx.MockTransport(respond))
    assert page["url"] == "https://example.org/final"
    assert body.received == 0
    assert body.closed
    assert len(seen) == 2


def test_redirect_limit_stops_before_another_request():
    seen = []
    def respond(request):
        seen.append(str(request.url))
        return httpx.Response(302, headers={"location": f"/hop-{len(seen)}"})
    with pytest.raises(ValueError, match="too many redirects"):
        research.fetch_page("https://example.org/start", transport=httpx.MockTransport(respond))
    assert len(seen) == research.MAX_REDIRECTS + 1


def test_search_response_is_bounded(monkeypatch):
    monkeypatch.setattr(research, "MAX_SEARCH_BYTES", 64)
    body = CountingBody()
    transport = httpx.MockTransport(lambda request: httpx.Response(
        200, headers={"content-type": "text/html"}, stream=body,
    ))
    with pytest.raises(ValueError, match="byte limit"):
        research.ddg_search("synthetic query", transport=transport)
    assert body.received == 96
    assert body.closed


def test_pdf_uses_its_own_cap_and_is_not_parsed_when_oversized(monkeypatch):
    monkeypatch.setattr(research, "MAX_PDF_BYTES", 64)
    parsed = []
    monkeypatch.setattr(research, "_pdf_text", lambda raw: (parsed.append(raw) or "text", "title"))
    def response(size):
        return httpx.MockTransport(lambda request: httpx.Response(
            200, headers={"content-type": "application/pdf"}, content=b"x" * size,
        ))
    page = research.fetch_page("https://example.org/a.pdf", max_bytes=16, transport=response(64))
    assert page["bytes"] == 64
    with pytest.raises(ValueError, match="byte limit"):
        research.fetch_page("https://example.org/a.pdf", max_bytes=16, transport=response(65))
    assert len(parsed) == 1


def _answer(address, port):
    return (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, port))


# Synthetic RFC 1918 address constructed from its reserved range; never a target.
@pytest.mark.parametrize("addresses", [["127.0.0.1"], ["8.8.8.8", str(ipaddress.IPv4Address(10 << 24) + 1)]])
def test_production_transport_rejects_private_dns_before_connect(monkeypatch, addresses):
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, **kwargs: [
        _answer(address, port) for address in addresses
    ])
    def forbidden_connect(*args, **kwargs):
        pytest.fail("private DNS answers must never reach connection setup")
    monkeypatch.setattr(transport_module, "_connect_pinned", forbidden_connect)
    transport = PinnedPublicTransport(deadline=time.monotonic() + 2, max_bytes=64, pdf_max_bytes=None)
    with pytest.raises(ValueError, match="non-public network"):
        fetch_public(
            "http://public-looking.example.org/a", timeout=2, max_bytes=64,
            max_redirects=3, user_agent="synthetic-test", transport=transport,
        )


@contextmanager
def _http_server(respond):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            respond(self)

        def log_message(self, *_args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def _test_socket(monkeypatch, server, targets_seen):
    def connect(targets, **kwargs):
        targets_seen.append(targets)
        connection = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        connection.connect(("127.0.0.1", server.server_port))
        return connection
    monkeypatch.setattr(transport_module, "_connect_pinned", connect)


def test_pinned_request_preserves_host_and_tls_name_without_second_dns_lookup(monkeypatch):
    hosts, names, lookups, targets = [], [], [], []
    def respond(handler):
        hosts.append(handler.headers["Host"])
        assert handler.headers["Accept-Encoding"] == "identity"
        handler.send_response(200)
        handler.send_header("Content-Type", "text/plain")
        handler.send_header("Content-Length", "2")
        handler.end_headers()
        handler.wfile.write(b"ok")
    def resolve(host, port, **kwargs):
        lookups.append(host)
        return [_answer("8.8.8.8", port)]
    class TestTLSContext:
        def wrap_socket(self, network_socket, *, server_hostname):
            names.append(server_hostname)
            return network_socket
    with _http_server(respond) as server:
        monkeypatch.setattr(socket, "getaddrinfo", resolve)
        monkeypatch.setattr(transport_module.ssl, "create_default_context", TestTLSContext)
        _test_socket(monkeypatch, server, targets)
        transport = PinnedPublicTransport(deadline=time.monotonic() + 2, max_bytes=64, pdf_max_bytes=None)
        result = fetch_public(
            "https://public.example.org/a", timeout=2, max_bytes=64,
            max_redirects=3, user_agent="synthetic-test", transport=transport,
        )
    assert result.body == b"ok"
    assert hosts == names == lookups == ["public.example.org"]
    assert targets == [((socket.AF_INET, ("8.8.8.8", 443)),)]


def test_redirect_revalidates_dns_and_blocks_private_hostname(monkeypatch):
    lookups, targets = [], []
    def respond(handler):
        handler.send_response(302)
        handler.send_header("Location", "http://private-looking.example.net/internal")
        handler.end_headers()
    def resolve(host, port, **kwargs):
        lookups.append(host)
        return [_answer("8.8.8.8" if len(lookups) == 1 else "127.0.0.1", port)]
    with _http_server(respond) as server:
        monkeypatch.setattr(socket, "getaddrinfo", resolve)
        _test_socket(monkeypatch, server, targets)
        transport = PinnedPublicTransport(deadline=time.monotonic() + 2, max_bytes=64, pdf_max_bytes=None)
        with pytest.raises(ValueError, match="non-public network"):
            fetch_public(
                "http://public.example.org/a", timeout=2, max_bytes=64,
                max_redirects=3, user_agent="synthetic-test", transport=transport,
            )
    assert lookups == ["public.example.org", "private-looking.example.net"]
    assert len(targets) == 1


def test_pinned_body_reads_only_one_sentinel_byte_beyond_cap(monkeypatch):
    targets, reads = [], []
    original_read = http.client.HTTPResponse.read1
    def track_read(response, amount):
        result = original_read(response, amount)
        reads.append(len(result))
        return result
    def respond(handler):
        handler.send_response(200)
        handler.send_header("Content-Type", "text/plain")
        handler.end_headers()
        handler.wfile.write(b"x" * 4096)
    with _http_server(respond) as server:
        monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, **kwargs: [_answer("8.8.8.8", port)])
        monkeypatch.setattr(http.client.HTTPResponse, "read1", track_read)
        _test_socket(monkeypatch, server, targets)
        transport = PinnedPublicTransport(deadline=time.monotonic() + 2, max_bytes=64, pdf_max_bytes=None)
        with pytest.raises(ValueError, match="byte limit"):
            fetch_public(
                "http://public.example.org/a", timeout=2, max_bytes=64,
                max_redirects=3, user_agent="synthetic-test", transport=transport,
            )
    assert sum(reads) == 65


def _blocked_worker(sender, url, kwargs):
    time.sleep(10)


def test_production_deadline_kills_and_reaps_blocked_worker(monkeypatch):
    monkeypatch.setattr(transport_module, "_worker", _blocked_worker)
    started = time.monotonic()
    with pytest.raises(ValueError, match="timeout"):
        fetch_public(
            "https://example.org/page", timeout=0.2, max_bytes=64,
            max_redirects=3, user_agent="synthetic-test",
        )
    assert time.monotonic() - started < 2
    assert not any(child.name == "atlas-research-fetch" for child in multiprocessing.active_children())
    assert not any(thread.name == "atlas-research-recv" for thread in threading.enumerate())
