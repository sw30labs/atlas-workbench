"""Tests for the Phase 7 (compound join) dashboard integration."""

import json
import sqlite3
import time

import pytest

from workbench_atlas.config import Settings
from workbench_atlas.web import runner


@pytest.fixture()
def dense_settings(tmp_path, monkeypatch):
    """Dense-enough fixture for a successful compound join, wired into the
    runner's settings lookup."""
    channels = {0: "identity", 1: "identity", 2: "cloud", 3: "identity", 4: "cloud", 5: "cloud"}
    risks = [
        {
            "issue": f"web join risk {i} ({channels[i]})",
            "review_status": "approved",
            "description": f"synthetic risk touching {channels[i]} infrastructure",
            "status": "monitoring",
            "signal_maturity": "established",
            "momentum": "stable",
            "evidence_refs": [f"E{i}q{q}" for q in range(8)],
            "source_type": "synthetic",
        }
        for i in range(6)
    ]
    (tmp_path / "emerging_risks.json").write_text(json.dumps(risks))
    ev_db = tmp_path / "event_evidence.db"
    conn = sqlite3.connect(ev_db)
    conn.execute("CREATE TABLE evidence_events (event_id TEXT, source_date TEXT, loss_amount_usd REAL, review_status TEXT DEFAULT 'approved')")
    for i in range(6):
        for q in range(8):
            if q == (i % 8):
                continue
            month = q * 3 + 2
            year = 2024 + (month - 1) // 12
            month = (month - 1) % 12 + 1
            conn.execute("INSERT INTO evidence_events (event_id, source_date, loss_amount_usd) VALUES (?,?,?)", (f"E{i}q{q}", f"{year}-{month:02d}-15", 10_000.0))
    conn.commit()
    conn.close()
    settings = Settings(
        scenario_db_path=tmp_path / "atlas.db",
        evidence_db_path=ev_db,
        emerging_risks_path=tmp_path / "emerging_risks.json",
    )
    monkeypatch.setattr(runner, "get_settings", lambda: settings)
    return settings


def test_compound_join_is_a_valid_phase():
    assert "compound_join" in runner.VALID_PHASES
    job_id, err = runner.start_job("not-a-phase", {})
    assert job_id is None and err


def test_execute_compound_join_phase(dense_settings):
    result = runner.execute_phase("compound_join", {})
    assert result["status"] == "ok", result.get("note")
    assert result["n_written"] > 0
    assert result["artifact"]


def test_start_job_compound_join_completes(dense_settings):
    job_id, err = runner.start_job("compound_join", {})
    assert err is None
    deadline = time.time() + 15
    while time.time() < deadline:
        job = runner.get_job(job_id)
        if job["status"] != "running":
            break
        time.sleep(0.1)
    job = runner.get_job(job_id)
    assert job["status"] == "done"
    assert job["result"]["status"] == "ok"
    assert job["result"]["n_written"] > 0


def test_index_page_has_phase7_card():
    html = open("workbench_atlas/web/static/index.html").read()
    assert "COMPOUND JOIN" in html
    assert 'data-phase="${p.id || "phase" + p.n}"' in html
    assert 'compound_join: { name: "Phase 7 · Compound Join"' in html
