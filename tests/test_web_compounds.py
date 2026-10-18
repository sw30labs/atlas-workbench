"""Tests for the Compounds dashboard page payload."""

import json
import sqlite3

import pytest

from contingency_atlas.compound_graduate import migrate_graduations
from contingency_atlas.compound_join import migrate_compound
from contingency_atlas.config import Settings
from contingency_atlas.schema import migrate, migrate_evidence_events
from contingency_atlas.web import server


@pytest.fixture()
def compound_settings(tmp_path, monkeypatch):
    risks = [
        {"issue": "identity plane concentration", "description": "identity",
         "evidence_refs": ["EA1", "EA2", "EA3", "EA4", "EA5", "EA6"], "source_type": "synthetic"},
        {"issue": "directory replication conflict", "description": "identity directory",
         "evidence_refs": ["EB1", "EB2", "EB3", "EB4", "EB5", "EB6"], "source_type": "synthetic"},
        {"issue": "cloud region failover congestion", "description": "cloud region",
         "evidence_refs": ["EC1"], "source_type": "synthetic"},
    ]
    risks = [{**r, "review_status": r.get("review_status", "approved")} for r in risks]
    (tmp_path / "emerging_risks.json").write_text(json.dumps(risks))
    ev_db = tmp_path / "event_evidence.db"
    conn = sqlite3.connect(ev_db)
    conn.execute("CREATE TABLE evidence_events (event_id TEXT, source_date TEXT, loss_amount_usd REAL, review_status TEXT DEFAULT 'approved')")
    quarters = [(2024, 2), (2024, 5), (2024, 8), (2024, 11), (2025, 2), (2025, 5)]
    for i, (y, m) in enumerate(quarters):
        conn.execute("INSERT INTO evidence_events (event_id, source_date, loss_amount_usd) VALUES (?,?,?)", (f"EA{i + 1}", f"{y}-{m:02d}-15", 1000.0))
        conn.execute("INSERT INTO evidence_events (event_id, source_date, loss_amount_usd) VALUES (?,?,?)", (f"EB{i + 1}", f"{y}-{m:02d}-15", 1000.0))
    conn.execute("INSERT INTO evidence_events (event_id, source_date, loss_amount_usd) VALUES (?,?,?)", ("EC1", "2024-02-15", 1000.0))
    conn.commit()
    conn.close()

    db = tmp_path / "atlas.db"
    migrate(str(db))
    migrate_evidence_events(str(ev_db))
    migrate_compound(str(db))
    migrate_graduations(str(db))
    conn = sqlite3.connect(db)
    # compound that co-occurs in holdout (A+B share last two quarters)
    conn.execute(
        "INSERT INTO compound_scenarios (compound_id, risk_ids, state_hash,"
        " p_hat, p_lo, p_hi, loss_q50, loss_q95, loss_mean, uncertainty_class,"
        " dependence_support, evidence_support, data_regime, status, run_id,"
        " scored_at, algorithm_version, provenance_note, loss_units)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("scn-aaa", json.dumps(["directory-replication-conflict", "identity-plane-concentration"]),
         "hash1", 0.5, 0.3, 0.7, 1e5, 9e5, 3e5, "mixed", 5, 0.8, "R1",
         "candidate", "run-1", "2026-08-01T00:00:00+00:00", "0.1.0", "test", "dollars"),
    )
    # compound without holdout co-occurrence (cloud only fires in Q1 2024)
    conn.execute(
        "INSERT INTO compound_scenarios (compound_id, risk_ids, state_hash,"
        " p_hat, p_lo, p_hi, loss_q50, loss_q95, loss_mean, uncertainty_class,"
        " dependence_support, evidence_support, data_regime, status, run_id,"
        " scored_at, algorithm_version, provenance_note, loss_units)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("scn-bbb", json.dumps(["cloud-region-failover-congestion", "identity-plane-concentration"]),
         "hash2", 0.2, 0.1, 0.4, 1e5, 5e5, 2e5, "mixed", 1, 0.3, "R1",
         "candidate", "run-1", "2026-08-01T00:00:00+00:00", "0.1.0", "test", "dollars"),
    )
    conn.execute(
        "INSERT INTO compound_graduations "
        "(compound_id, scenario_id, run_id, created_at, eligibility, judge_score) "
        "VALUES (?,?,?,?,?,?)",
        ("scn-aaa", "ATLAS-CMP-AAA", "grad-1", "2026-08-01T01:00:00+00:00", "{}", 0.85),
    )
    conn.execute(
        "INSERT INTO compound_join_runs VALUES (?,?,?,?,?,?,?,?,?)",
        ("run-1", "2026-08-01T00:00:00+00:00", "2026-08-01T00:01:00+00:00",
         "ok", 3, 3, 3, 2, "scored 3 pairs"),
    )
    conn.commit()
    conn.close()

    settings = Settings(
        scenario_db_path=db,
        evidence_db_path=ev_db,
        emerging_risks_path=tmp_path / "emerging_risks.json",
    )
    monkeypatch.setattr(server, "get_settings", lambda: settings)
    return settings


def test_payload_compounds_section(compound_settings):
    payload = server.build_payload()
    compounds = payload["compounds"]
    assert len(compounds) == 2
    # sorted by p_hat desc
    assert compounds[0]["compound_id"] == "scn-aaa"
    top = compounds[0]
    assert top["member_titles"] == [
        "directory replication conflict",
        "identity plane concentration",
    ]
    assert top["holdout_survived"] is True
    assert top["graduated_scenario_id"] == "ATLAS-CMP-AAA"
    assert top["judge_score"] == 0.85
    second = compounds[1]
    assert second["holdout_survived"] is False
    assert second["graduated_scenario_id"] is None


def test_payload_join_runs_ledger(compound_settings):
    payload = server.build_payload()
    runs = payload["compound_join_runs"]
    assert len(runs) == 1
    assert runs[0]["run_id"] == "run-1"
    assert runs[0]["status"] == "ok"


def test_payload_compounds_empty_before_first_join(tmp_path, monkeypatch):
    db = tmp_path / "atlas.db"
    ev = tmp_path / "event_evidence.db"
    migrate(str(db))
    migrate_evidence_events(str(ev))
    settings = Settings(
        scenario_db_path=db,
        evidence_db_path=ev,
        emerging_risks_path=tmp_path / "missing.json",
    )
    monkeypatch.setattr(server, "get_settings", lambda: settings)
    payload = server.build_payload()
    assert payload["compounds"] == []
    assert payload["compound_join_runs"] == []


def test_index_has_compounds_view():
    html = open("contingency_atlas/web/static/index.html").read()
    assert 'data-view="compounds"' in html
    assert 'id="view-compounds"' in html
    assert '"compounds"' in html  # in VIEWS list
    assert "renderCompounds" in html
