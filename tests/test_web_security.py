"""Security boundaries for the local dashboard's mutation endpoint."""

from __future__ import annotations

import http.client
import json
import threading
from contextlib import contextmanager
from http.server import ThreadingHTTPServer

from workbench_atlas.web import runner
from workbench_atlas.web.server import (
    MAX_REQUEST_BYTES,
    DashboardHandler,
    _is_loopback_bind_host,
)


@contextmanager
def _dashboard_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), DashboardHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _post(port: int, body: bytes, headers: dict[str, str]):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        connection.request("POST", "/api/run", body=body, headers=headers)
        response = connection.getresponse()
        payload = json.loads(response.read())
        return response.status, payload
    finally:
        connection.close()


def test_run_accepts_same_origin_json(monkeypatch):
    monkeypatch.setattr(runner, "start_job", lambda phase, body: ("job-1", None))
    with _dashboard_server() as port:
        body = b'{"phase":"phase4"}'
        status, payload = _post(
            port,
            body,
            {
                "Content-Type": "application/json",
                "Origin": f"http://127.0.0.1:{port}",
            },
        )
    assert status == 202
    assert payload == {"job_id": "job-1"}


def test_server_bind_is_restricted_to_loopback():
    assert _is_loopback_bind_host("127.0.0.1")
    assert _is_loopback_bind_host("::1")
    assert _is_loopback_bind_host("localhost")
    assert not _is_loopback_bind_host("0.0.0.0")
    assert not _is_loopback_bind_host("dashboard.example.org")


def test_api_get_rejects_non_loopback_host_header():
    with _dashboard_server() as port:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        try:
            connection.request(
                "GET",
                "/api/health",
                headers={"Host": "attacker.invalid"},
            )
            response = connection.getresponse()
            response.read()
        finally:
            connection.close()
    assert response.status == 403


def test_run_rejects_cross_origin_request(monkeypatch):
    called = False

    def _unexpected_start(phase, body):
        nonlocal called
        called = True
        return "job-1", None

    monkeypatch.setattr(runner, "start_job", _unexpected_start)
    with _dashboard_server() as port:
        status, _ = _post(
            port,
            b'{"phase":"phase4"}',
            {
                "Content-Type": "application/json",
                "Origin": "https://attacker.invalid",
            },
        )
    assert status == 403
    assert called is False


def test_run_rejects_simple_text_body(monkeypatch):
    monkeypatch.setattr(runner, "start_job", lambda phase, body: ("job-1", None))
    with _dashboard_server() as port:
        status, _ = _post(
            port,
            b'{"phase":"phase4"}',
            {
                "Content-Type": "text/plain",
                "Origin": f"http://127.0.0.1:{port}",
            },
        )
    assert status == 415


def test_run_rejects_oversized_body_before_dispatch(monkeypatch):
    called = False

    def _unexpected_start(phase, body):
        nonlocal called
        called = True
        return "job-1", None

    monkeypatch.setattr(runner, "start_job", _unexpected_start)
    with _dashboard_server() as port:
        try:
            status, _ = _post(
                port,
                b"{" + (b" " * MAX_REQUEST_BYTES),
                {
                    "Content-Type": "application/json",
                    "Origin": f"http://127.0.0.1:{port}",
                },
            )
        except (ConnectionResetError, BrokenPipeError):
            # Refusing before draining the body means the socket can close
            # while the client is still writing, which surfaces as a reset
            # instead of a readable 413. Either way the body was refused.
            status = None
    assert status in (413, None)
    assert called is False
