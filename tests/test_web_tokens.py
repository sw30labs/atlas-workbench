"""Tests for the situation-room token usage chart endpoint."""

import http.client
import json
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer

import pytest

from contingency_atlas.scoring.telemetry import TelemetryLogger
from contingency_atlas.web.server import DashboardHandler, build_token_usage


@contextmanager
def _server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), DashboardHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.fixture()
def usage_events(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    logger = TelemetryLogger(tmp_path / "tok.jsonl", run_id="tok-test")
    for days_ago, (inp, outp) in ((0, (100, 50)), (2, (200, 80)), (20, (400, 100))):
        ts = (now - timedelta(days=days_ago)).isoformat()
        logger.emit(node="x", phase="evaluation", model_id="m",
                    input_tokens=inp, output_tokens=outp)
        # backdate the event
        path = tmp_path / "tok.jsonl"
        lines = path.read_text().splitlines()
        row = json.loads(lines[-1])
        row["ts"] = ts
        lines[-1] = json.dumps(row, sort_keys=True)
        path.write_text("\n".join(lines) + "\n")
    monkeypatch.setenv("ATLAS_TELEMETRY", str(tmp_path / "tok.jsonl"))


def test_daily_buckets(usage_events):
    d = build_token_usage("7d")
    assert d["period"] == "7d"
    assert len(d["buckets"]) == 7
    today = d["buckets"][-1]
    assert today["input"] == 100 and today["output"] == 50
    two_days = d["buckets"][-3]
    assert two_days["input"] == 200 and two_days["output"] == 80
    # the 20-day-old event must NOT appear in the 7d window
    assert d["totals"] == {"input": 300, "output": 130}


def test_weekly_buckets_include_older_events(usage_events):
    d = build_token_usage("7w")
    assert d["period"] == "7w"
    assert len(d["buckets"]) == 7
    assert all(b["label"].startswith("W") for b in d["buckets"])
    # all three events fall inside the 7-week window
    assert d["totals"] == {"input": 700, "output": 230}


def test_endpoint(usage_events):
    with _server() as port:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/tokens/usage?period=7w")
        resp = conn.getresponse()
        body = json.loads(resp.read().decode())
        conn.close()
    assert resp.status == 200
    assert body["period"] == "7w"
    assert body["totals"]["input"] == 700


def test_overview_has_token_chart():
    html = open("contingency_atlas/web/static/index.html").read()
    assert 'id="ch-tokens"' in html
    assert 'data-period="7d"' in html and 'data-period="7w"' in html
    assert "drawTokens" in html and "loadTokenUsage" in html
