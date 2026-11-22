"""Tests for the opt-in, proposal-only public-source scouting boundary."""

from __future__ import annotations

import http.server
import multiprocessing
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

import workbench_atlas.scouting as scouting
from workbench_atlas.scouting import (
    SOURCE_CATEGORIES,
    FetchFailure,
    FetchResponse,
    NetworkOptInRequired,
    PinnedFeedTransport,
    RegistryValidationError,
    ScoutingLimits,
    collect_proposals,
    load_registry,
    parse_feed_items,
    registry_summary,
    validate_registry_data,
)

ROOT = Path(__file__).resolve().parents[1]


def _sleeping_process_target():
    time.sleep(60)


def _partial_pipe_frame_target(sender):
    import os
    import struct

    # multiprocessing.connection uses a four-byte network-order length header
    # for ordinary frames. Advertise a body and then stall before writing it.
    os.write(sender.fileno(), struct.pack("!i", 20_000))
    time.sleep(60)


def _silent_pipe_target(sender):
    time.sleep(60)


def _registry_data():
    return {
        "schema_version": "atlas-scouting-registry-v1",
        "network_collection_default": False,
        "last_reviewed_on": "2026-07-29",
        "sources": [
            {
                "id": "test_regulator",
                "name": "Example Public Regulator",
                "category": "regulator",
                "feed_url": "https://updates.example.org/feed.xml",
                "homepage_url": "https://updates.example.org/",
                "allowed_hosts": ["updates.example.org"],
                "enabled": True,
                "jurisdiction": "Exampleland",
                "notes": "Synthetic test registry entry.",
            }
        ],
    }


class FixtureTransport:
    def __init__(
        self,
        body: bytes,
        *,
        final_url: str = "https://updates.example.org/feed.xml",
        status_code: int = 200,
    ):
        self.body = body
        self.final_url = final_url
        self.status_code = status_code
        self.calls = []

    def fetch(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FetchResponse(
            requested_url=url,
            final_url=self.final_url,
            status_code=self.status_code,
            headers={"Content-Type": "application/rss+xml; charset=utf-8"},
            body=self.body,
        )


@pytest.fixture
def rss_fixture():
    return b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <item>
      <title>Draft resilience guidance</title>
      <link>https://updates.example.org/notices/one#overview</link>
      <description><![CDATA[<p>A public consultation was opened.</p>]]></description>
      <pubDate>Tue, 28 Jul 2026 12:00:00 GMT</pubDate>
    </item>
    <item>
      <title>Second notice</title>
      <link>https://updates.example.org/notices/two</link>
      <description>Another public notice.</description>
    </item>
  </channel>
</rss>
"""


def test_packaged_registry_is_disabled_by_default_and_covers_all_categories():
    registry = load_registry()
    summary = registry_summary(registry)

    assert summary["network_collection_default"] is False
    assert summary["missing_categories"] == []
    assert set(summary["category_counts"]) == SOURCE_CATEGORIES
    assert summary["source_count"] >= len(SOURCE_CATEGORIES)


def test_collection_requires_explicit_opt_in_without_calling_transport(rss_fixture):
    registry = validate_registry_data(_registry_data())
    transport = FixtureTransport(rss_fixture)

    with pytest.raises(NetworkOptInRequired):
        collect_proposals(registry, transport=transport)

    assert transport.calls == []


def test_injected_transport_produces_bounded_pending_proposals(rss_fixture):
    registry = validate_registry_data(_registry_data())
    transport = FixtureTransport(rss_fixture)
    limits = ScoutingLimits(max_items_per_source=1, max_total_items=1)

    report = collect_proposals(
        registry,
        allow_network=True,
        limits=limits,
        transport=transport,
        now=datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc),
    )

    assert report["proposal_count"] == 1
    assert report["error_count"] == 0
    assert report["pipeline_invoked"] is False
    assert report["evidence_records_created"] == 0
    assert report["human_review_required"] is True
    proposal = report["proposals"][0]
    assert proposal["review_status"] == "pending_human_review"
    assert proposal["evidence_status"] == "not_evidence"
    assert proposal["pipeline_eligible"] is False
    assert proposal["rights_status"] == "unreviewed"
    assert proposal["source_url"] == "https://updates.example.org/notices/one"
    assert proposal["summary_excerpt"] == "A public consultation was opened."
    assert proposal["collection"]["retrieved_at"] == "2026-07-29T12:00:00Z"
    assert transport.calls[0][1]["timeout_seconds"] == 10.0
    assert transport.calls[0][1]["max_bytes"] == 1_000_000


def test_disallowed_transport_final_url_is_rejected(rss_fixture):
    registry = validate_registry_data(_registry_data())
    transport = FixtureTransport(
        rss_fixture,
        final_url="https://outside.example.net/feed.xml",
    )

    report = collect_proposals(
        registry,
        allow_network=True,
        transport=transport,
    )

    assert report["proposal_count"] == 0
    assert report["errors"][0]["code"] == "response_url_not_allowed"
    assert report["source_results"][0]["status"] == "error"


def test_response_size_is_enforced_even_for_injected_transport():
    registry = validate_registry_data(_registry_data())
    transport = FixtureTransport(b"x" * 101)

    report = collect_proposals(
        registry,
        allow_network=True,
        limits=ScoutingLimits(max_response_bytes=100),
        transport=transport,
    )

    assert report["proposal_count"] == 0
    assert report["errors"][0]["code"] == "response_too_large"


def test_registry_rejects_non_public_feed_and_host_mismatch():
    private = _registry_data()
    private["sources"][0]["feed_url"] = "".join(
        ("http://127.", "0.0.1/feed.xml")
    )
    with pytest.raises(RegistryValidationError):
        validate_registry_data(private)

    mismatch = _registry_data()
    mismatch["sources"][0]["feed_url"] = "https://other.example.org/feed.xml"
    with pytest.raises(RegistryValidationError):
        validate_registry_data(mismatch)


def test_excluded_hosts_are_rejected_at_registry_and_fetch_admission():
    denied_host = "".join(("o", "rx", ".org"))
    excluded = _registry_data()
    excluded["sources"][0]["feed_url"] = f"https://{denied_host}/feed.xml"
    excluded["sources"][0]["homepage_url"] = f"https://{denied_host}/"
    excluded["sources"][0]["allowed_hosts"] = [denied_host]

    with pytest.raises(RegistryValidationError, match="excluded"):
        validate_registry_data(excluded)
    assert not scouting.is_allowed_source_url(
        f"https://{denied_host}/redirect",
        [denied_host],
    )
    with pytest.raises(FetchFailure) as caught:
        scouting._check_fetch_target(
            f"https://{denied_host}/redirect",
            frozenset({denied_host}),
        )
    assert caught.value.code == "url_not_allowed"


def test_registry_rejects_impossible_review_date_and_url_controls():
    invalid_date = _registry_data()
    invalid_date["last_reviewed_on"] = "2026-02-30"
    with pytest.raises(RegistryValidationError):
        validate_registry_data(invalid_date)

    control_url = _registry_data()
    control_url["sources"][0]["feed_url"] = (
        "https://updates.example.org/feed.xml\nignored"
    )
    with pytest.raises(RegistryValidationError):
        validate_registry_data(control_url)


def test_xml_entities_are_rejected_even_after_a_long_prefix():
    body = (
        b"<?xml version='1.0'?><!--"
        + (b"x" * 5_000)
        + b"--><!DOCTYPE rss [<!ENTITY x 'unsafe'>]><rss/>"
    )
    with pytest.raises(ValueError, match="entities"):
        parse_feed_items(body)


def test_cli_collect_is_network_disabled_without_explicit_flag():
    result = subprocess.run(
        [sys.executable, "scripts/scout_sources.py", "collect"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "network scouting is disabled" in result.stderr


def test_dns_resolution_is_deadline_bounded(monkeypatch):
    def delayed_resolution(*args, **kwargs):
        time.sleep(0.2)
        return []

    monkeypatch.setattr(socket, "getaddrinfo", delayed_resolution)
    started = time.monotonic()
    with pytest.raises(FetchFailure) as caught:
        scouting._resolve_public_addresses(
            "updates.example.org",
            443,
            deadline=time.monotonic() + 0.02,
        )

    assert caught.value.code == "timeout"
    assert time.monotonic() - started < 0.15


def test_dns_resolution_rejects_any_private_answer(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("8.8.8.8", 443),
            ),
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("127.0.0.1", 443),
            ),
        ],
    )

    with pytest.raises(FetchFailure) as caught:
        scouting._resolve_public_addresses(
            "updates.example.org",
            443,
            deadline=time.monotonic() + 1,
        )

    assert caught.value.code == "non_public_dns"


def _serve(handler_type):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler_type)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _pin_to_test_server(monkeypatch, server):
    monkeypatch.setattr(
        scouting,
        "_resolve_public_addresses",
        lambda *args, **kwargs: (
            (
                socket.AF_INET,
                ("127.0.0.1", server.server_address[1]),
            ),
        ),
    )


def test_transport_rejects_compressed_responses(monkeypatch):
    class CompressedHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/rss+xml")
            self.send_header("Content-Encoding", "gzip")
            self.end_headers()
            self.wfile.write(b"compressed bytes are never decoded")

        def log_message(self, format, *args):
            pass

    server = _serve(CompressedHandler)
    _pin_to_test_server(monkeypatch, server)
    try:
        with pytest.raises(FetchFailure) as caught:
            PinnedFeedTransport(isolate_process=False).fetch(
                "http://updates.example.org/feed.xml",
                allowed_hosts=frozenset({"updates.example.org"}),
                timeout_seconds=1,
                max_bytes=1_000,
                max_redirects=0,
            )
    finally:
        server.shutdown()
        server.server_close()

    assert caught.value.code == "unexpected_content_encoding"


def test_transport_enforces_wall_clock_deadline_on_dribble(monkeypatch):
    class DribbleHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/rss+xml")
            self.end_headers()
            try:
                for byte in b"<rss><channel><title>slow</title></channel></rss>":
                    self.wfile.write(bytes([byte]))
                    self.wfile.flush()
                    time.sleep(0.02)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, format, *args):
            pass

    server = _serve(DribbleHandler)
    _pin_to_test_server(monkeypatch, server)
    started = time.monotonic()
    try:
        with pytest.raises(FetchFailure) as caught:
            PinnedFeedTransport(isolate_process=False).fetch(
                "http://updates.example.org/feed.xml",
                allowed_hosts=frozenset({"updates.example.org"}),
                timeout_seconds=0.08,
                max_bytes=1_000,
                max_redirects=0,
            )
        elapsed = time.monotonic() - started
    finally:
        server.shutdown()
        server.server_close()

    assert caught.value.code == "timeout"
    assert elapsed < 0.3


def test_isolated_worker_cleanup_kills_and_reaps_process():
    context = multiprocessing.get_context("spawn")
    process = context.Process(target=_sleeping_process_target)
    process.start()
    assert process.is_alive()

    scouting._stop_fetch_process(process)

    assert process not in multiprocessing.active_children()


def test_isolated_transport_cleans_up_when_caller_is_interrupted(monkeypatch):
    class StubConnection:
        def __init__(self, *, block=False):
            self.block = block
            self.closed = False
            self.released = threading.Event()

        def recv(self):
            if self.block:
                self.released.wait(timeout=1)
            raise EOFError

        def close(self):
            self.closed = True
            self.released.set()

    class StubProcess:
        def __init__(self):
            self.pid = None
            self.alive = False
            self.killed = False
            self.closed = False

        def start(self):
            self.pid = 123
            self.alive = True

        def is_alive(self):
            return self.alive

        def kill(self):
            self.killed = True
            self.alive = False

        def terminate(self):
            self.alive = False

        def join(self, timeout=None):
            pass

        def close(self):
            self.closed = True

    receiver = StubConnection(block=True)
    sender = StubConnection()
    process = StubProcess()

    class StubContext:
        def Pipe(self, duplex):
            return receiver, sender

        def Process(self, **kwargs):
            return process

    monkeypatch.setattr(
        scouting.multiprocessing,
        "get_context",
        lambda method: StubContext(),
    )
    def interrupt_deadline(deadline):
        raise KeyboardInterrupt

    monkeypatch.setattr(
        scouting,
        "_remaining_seconds",
        interrupt_deadline,
    )

    with pytest.raises(KeyboardInterrupt):
        PinnedFeedTransport().fetch(
            "https://updates.example.org/feed.xml",
            allowed_hosts=frozenset({"updates.example.org"}),
            timeout_seconds=1,
            max_bytes=1_000,
            max_redirects=0,
        )

    assert process.killed is True
    assert process.closed is True
    assert receiver.closed is True
    assert sender.closed is True
    assert not any(
        thread.name == "atlas-scout-recv"
        for thread in threading.enumerate()
    )


def test_isolated_transport_fails_closed_in_daemonic_parent(monkeypatch):
    class DaemonicProcess:
        daemon = True

    monkeypatch.setattr(
        scouting.multiprocessing,
        "current_process",
        lambda: DaemonicProcess(),
    )

    with pytest.raises(FetchFailure) as caught:
        PinnedFeedTransport().fetch(
            "https://updates.example.org/feed.xml",
            allowed_hosts=frozenset({"updates.example.org"}),
            timeout_seconds=1,
            max_bytes=1_000,
            max_redirects=0,
        )

    assert caught.value.code == "process_isolation_unavailable"


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Uses POSIX pipe framing to simulate a partial message",
)
def test_isolated_transport_deadline_covers_partial_pipe_frame(monkeypatch):
    real_context = multiprocessing.get_context("spawn")

    class PartialFrameContext:
        def Pipe(self, duplex):
            return real_context.Pipe(duplex=duplex)

        def Process(self, **kwargs):
            return real_context.Process(
                target=_partial_pipe_frame_target,
                args=(kwargs["args"][0],),
                name=kwargs["name"],
                daemon=kwargs["daemon"],
            )

    monkeypatch.setattr(
        scouting.multiprocessing,
        "get_context",
        lambda method: PartialFrameContext(),
    )
    started = time.monotonic()

    with pytest.raises(FetchFailure) as caught:
        PinnedFeedTransport().fetch(
            "https://updates.example.org/feed.xml",
            allowed_hosts=frozenset({"updates.example.org"}),
            timeout_seconds=0.1,
            max_bytes=50_000,
            max_redirects=0,
        )

    assert caught.value.code == "timeout"
    assert time.monotonic() - started < 0.5
    assert not multiprocessing.active_children()
    assert not any(
        thread.name == "atlas-scout-recv"
        for thread in threading.enumerate()
    )


def test_isolated_transport_deadline_covers_silent_worker(monkeypatch):
    real_context = multiprocessing.get_context("spawn")

    class SilentContext:
        def Pipe(self, duplex):
            return real_context.Pipe(duplex=duplex)

        def Process(self, **kwargs):
            return real_context.Process(
                target=_silent_pipe_target,
                args=(kwargs["args"][0],),
                name=kwargs["name"],
                daemon=kwargs["daemon"],
            )

    monkeypatch.setattr(
        scouting.multiprocessing,
        "get_context",
        lambda method: SilentContext(),
    )
    started = time.monotonic()

    with pytest.raises(FetchFailure) as caught:
        PinnedFeedTransport().fetch(
            "https://updates.example.org/feed.xml",
            allowed_hosts=frozenset({"updates.example.org"}),
            timeout_seconds=0.1,
            max_bytes=1_000,
            max_redirects=0,
        )

    assert caught.value.code == "timeout"
    assert time.monotonic() - started < 0.5
    assert not multiprocessing.active_children()
    assert not any(
        thread.name == "atlas-scout-recv"
        for thread in threading.enumerate()
    )
