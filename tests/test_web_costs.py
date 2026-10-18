"""Tests for the Costs / telemetry page."""

import http.client
import json
import sqlite3
import threading
from contextlib import contextmanager
from http.server import ThreadingHTTPServer

import pytest

from contingency_atlas.compound_join import migrate_compound
from contingency_atlas.config import Settings
from contingency_atlas.schema import migrate
from contingency_atlas.scoring.telemetry import TelemetryLogger
from contingency_atlas.web import server
from contingency_atlas.web.server import DashboardHandler, build_cost_summary


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


def _get(port: int, path: str):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path)
    resp = conn.getresponse()
    body = resp.read().decode("utf-8")
    conn.close()
    return resp.status, body


@pytest.fixture()
def costs_dir(tmp_path, monkeypatch):
    logger = TelemetryLogger(
        tmp_path / "telemetry.jsonl",
        run_id="cost-test",
        pricing={
            "version": "test-1",
            "models": {
                "local/judge": {"input_per_mtok": 1.0, "output_per_mtok": 2.0,
                                "shadow_input_per_mtok": 0.05, "shadow_output_per_mtok": 0.05},
            },
        },
    )
    # 3 judge calls with tokens (scenario-attributed) + 1 det event + 1 deny
    for scn in ("scn-a", "scn-a", "scn-b"):
        logger.emit(
            node="score_jdg", phase="evaluation", provider="mlx",
            model_id="local/judge", scenario_id=scn,
            input_tokens=100, output_tokens=50, latency_ms=200,
        )
    logger.emit(node="score_det", phase="evaluation", provider="none", model_id="det-scorer")
    logger.emit(node="egress", phase="control", status="denied_egress")

    # compound register drives n_promoted now
    db = tmp_path / "atlas.db"
    migrate(str(db))
    migrate_compound(str(db))
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO compound_scenarios (compound_id, risk_ids, state_hash,"
        " p_hat, p_lo, p_hi, loss_q50, loss_q95, loss_mean, uncertainty_class,"
        " dependence_support, evidence_support, data_regime, status, run_id,"
        " scored_at, algorithm_version, provenance_note)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("scn-a", '["a","b"]', "h1", 0.4, 0.2, 0.6, 1e5, 9e5, 3e5, "mixed", 4, 0.7,
         "R1", "candidate", "r", "2026-08-01", "0.1.0", "t"),
    )
    conn.commit()
    conn.close()

    settings = Settings(
        scenario_db_path=db,
        evidence_db_path=tmp_path / "ev.db",
        emerging_risks_path=tmp_path / "risks.json",
    )
    monkeypatch.setattr(server, "get_settings", lambda: settings)
    monkeypatch.setenv("ATLAS_TELEMETRY", str(tmp_path / "telemetry.jsonl"))
    return tmp_path


def test_cost_summary_aggregation(costs_dir):
    d = build_cost_summary()
    assert d["n_events"] == 5
    expected = 3 * (100 / 1e6 * 1.0 + 50 / 1e6 * 2.0)
    assert abs(d["spend_usd"] - expected) < 0.001
    assert d["spend_shadow_usd"] > 0
    assert d["n_promoted"] == 1  # from the compound register
    assert abs(d["cost_per_promoted_usd"] - expected) < 0.001
    assert d["tokens"] == {"input": 300, "output": 150, "cache_read": 0, "cache_write": 0}
    assert d["cache_hit_rate"] == 0.0
    assert d["egress_denies"] == 1
    judge = d["by_model"]["local/judge"]
    assert judge["n_events"] == 3 and judge["latency_avg_ms"] == 200
    # buckets carry the in/out token split so the client can re-price them
    assert judge["input_tokens"] == 300 and judge["output_tokens"] == 150
    attr = {a["scenario_id"]: a for a in d["attributed"]}
    assert attr["scn-a"]["cost_usd"] > attr["scn-b"]["cost_usd"]
    # token tallies are DIRECT only, split input/output (priced differently):
    # scn-a has 2 judge events (2x 100 in, 2x 50 out), scn-b has 1
    assert attr["scn-a"]["input_tokens"] == 200
    assert attr["scn-a"]["output_tokens"] == 100
    assert attr["scn-b"]["input_tokens"] == 100
    assert attr["scn-b"]["output_tokens"] == 50
    # the det + egress events carry no tokens and land in the unattributed row
    assert d["unattributed"]["input_tokens"] == 0
    assert d["unattributed"]["output_tokens"] == 0
    assert d["recent"][0]["status"] == "denied_egress"
    assert d["pricing_versions"] == ["test-1"]


def test_cost_summary_none_without_telemetry(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_TELEMETRY", str(tmp_path / "none.jsonl"))
    assert build_cost_summary() is None


def test_costs_api_and_page(costs_dir):
    with _server() as port:
        status, body = _get(port, "/api/costs/summary")
        assert status == 200
        assert json.loads(body)["n_events"] == 5
        # the standalone page is retired: /costs redirects to the deck view
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/costs")
        resp = conn.getresponse()
        resp.read()
        location = resp.getheader("Location")
        conn.close()
        assert resp.status == 302
        assert location == "/#costs"
        # shareable what-if rates survive the redirect
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/costs?pin=0.15&pout=0.60")
        resp = conn.getresponse()
        resp.read()
        location = resp.getheader("Location")
        conn.close()
        assert resp.status == 302
        assert location == "/?pin=0.15&pout=0.60#costs"
        status, body = _get(port, "/")
        assert status == 200 and 'id="view-costs"' in body


def test_costs_view_lives_in_the_deck():
    """Costs & Telemetry is a Command Deck view (the nav frame stays) —
    the standalone page is gone, its panels and what-if controls ported."""
    from pathlib import Path

    assert not (Path(server.STATIC_DIR) / "costs.html").exists()
    html = (Path(server.STATIC_DIR) / "index.html").read_text()
    for marker in (
        'id="view-costs"',
        'data-view="costs"',
        'id="cost-kpis"',
        'id="whatif-panel"',
        'id="by-phase"',
        'id="attributed"',
        "Costs",
    ):
        assert marker in html


def test_costs_page_has_whatif_pricing():
    """Client-side what-if: user-entered $/1M-token rates re-price the
    metered token stream (amber, never overwriting metered figures)."""
    from pathlib import Path

    html = (Path(server.STATIC_DIR) / "index.html").read_text()
    for marker in (
        'id="whatif-panel"',
        'id="wi-in"',
        'id="wi-out"',
        'id="wi-clear"',
        "wiCost(",               # the re-pricing math
        "localStorage",           # rates persist
        "?pin=",                  # shareable URL rates
        "atlas-costs-whatif",
        "no metered tokens",      # zero-token (deterministic) rows stay honest
        "k-sub wi",               # KPI tallies carry an amber what-if line
    ):
        assert marker in html


def test_costs_api_404_without_telemetry(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_TELEMETRY", str(tmp_path / "none.jsonl"))
    with _server() as port:
        status, _ = _get(port, "/api/costs/summary")
        assert status == 404


def test_scenario_routes_gone(costs_dir):
    """The loop-era scenario detail page and API are removed with the loop."""
    with _server() as port:
        for path in ("/scenario", "/api/multiverse/scenarios/scn-a"):
            status, _ = _get(port, path)
            assert status == 404, path
