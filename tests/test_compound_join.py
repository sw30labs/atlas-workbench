"""Tests for the monthly compound join step and its pipeline node."""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from contingency_atlas.compound_join import (
    is_due,
    migrate_compound,
    run_compound_join,
)
from contingency_atlas.config import Settings
from contingency_atlas.scoring.scorer import ScorerConfig

FAST = ScorerConfig(mc_draws=300, interval_draws=300)


def _make_settings(tmp_path, risks, evidence_rows):
    risks_path = tmp_path / "emerging_risks.json"
    risks_path.write_text(json.dumps(risks))
    ev_db = tmp_path / "event_evidence.db"
    conn = sqlite3.connect(ev_db)
    conn.execute(
        "CREATE TABLE evidence_events (event_id TEXT, source_date TEXT, loss_amount_usd REAL, review_status TEXT DEFAULT 'approved')"
    )
    conn.executemany("INSERT INTO evidence_events (event_id, source_date, loss_amount_usd) VALUES (?,?,?)", evidence_rows)
    conn.commit()
    conn.close()
    return Settings(
        scenario_db_path=tmp_path / "atlas.db",
        evidence_db_path=ev_db,
        emerging_risks_path=risks_path,
    )


def _dense_fixture(tmp_path):
    """6 risks, one linked evidence event per quarter across 8 quarters.

    Two channel groups so the join has blocked pairs to score:
    identity (0,1,3) and cloud (2,4,5)."""
    channels = {0: "identity", 1: "identity", 2: "cloud", 3: "identity", 4: "cloud", 5: "cloud"}
    risks = [
        {
            "issue": f"compound test risk {i} ({channels[i]})",
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
    rows = []
    for i in range(6):
        for q in range(8):
            if q == (i % 8):  # each risk is silent in one quarter
                continue
            month = q * 3 + 2
            year = 2024 + (month - 1) // 12
            month = (month - 1) % 12 + 1
            rows.append((f"E{i}q{q}", f"{year}-{month:02d}-15", 10_000.0 * (i + 1)))
    return risks, rows


# ---------------------------------------------------------------------------
# Due gating
# ---------------------------------------------------------------------------


def test_due_logic(tmp_path):
    db = str(tmp_path / "atlas.db")
    migrate_compound(db)
    assert is_due(db)  # never ran
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO compound_join_runs VALUES (?,?,?,?,?,?,?,?,?)",
        ("r1", "2026-07-01T00:00:00+00:00", "2026-07-01T01:00:00+00:00", "ok", 0, 0, 0, 0, ""),
    )
    conn.commit()
    conn.close()
    assert not is_due(db, today=datetime(2026, 7, 15, tzinfo=timezone.utc))
    assert is_due(db, today=datetime(2026, 8, 1, tzinfo=timezone.utc))


def test_not_due_run_is_skipped_without_writes(tmp_path):
    risks, rows = _dense_fixture(tmp_path)
    settings = _make_settings(tmp_path, risks, rows)
    first = run_compound_join(settings, force=True, scorer_config=FAST)
    assert first.status == "ok"
    second = run_compound_join(settings, scorer_config=FAST)
    assert second.status == "skipped_not_due"
    # no new run recorded for a skip
    conn = sqlite3.connect(settings.scenario_db_path)
    n_runs = conn.execute("SELECT COUNT(*) FROM compound_join_runs").fetchone()[0]
    conn.close()
    assert n_runs == 1


# ---------------------------------------------------------------------------
# Sparse honesty
# ---------------------------------------------------------------------------


def test_sparse_data_records_insufficiency_not_fake_scores(tmp_path):
    risks = [
        {"issue": "lonely risk", "description": "sparse", "evidence_refs": ["E1"],
         "review_status": "approved", "source_type": "synthetic"}
    ]
    rows = [("E1", "2024-02-15", 1000.0)]
    settings = _make_settings(tmp_path, risks, rows)
    result = run_compound_join(settings, force=True, scorer_config=FAST)
    assert result.status == "insufficient_data"
    assert result.note
    conn = sqlite3.connect(settings.scenario_db_path)
    n = conn.execute("SELECT COUNT(*) FROM compound_scenarios").fetchone()[0]
    conn.close()
    assert n == 0  # nothing fabricated
    assert not (tmp_path / "compound_scenarios.json").exists()


# ---------------------------------------------------------------------------
# Scored path
# ---------------------------------------------------------------------------


def test_scored_path_writes_rows_and_artifact(tmp_path):
    risks, rows = _dense_fixture(tmp_path)
    settings = _make_settings(tmp_path, risks, rows)
    result = run_compound_join(settings, force=True, scorer_config=FAST)
    assert result.status == "ok", result.note
    assert result.n_written > 0
    conn = sqlite3.connect(settings.scenario_db_path)
    compounds = conn.execute(
        "SELECT compound_id, risk_ids, p_hat, uncertainty_class, algorithm_version "
        "FROM compound_scenarios"
    ).fetchall()
    conn.close()
    assert len(compounds) == result.n_written
    for cid, risk_ids, p_hat, uclass, algo in compounds:
        assert cid.startswith("scn-")
        assert len(json.loads(risk_ids)) == 2
        assert 0.0 <= p_hat <= 1.0
        assert uclass in ("statistical", "mixed", "judgment_dominant")
        assert algo
    artifact = json.loads(Path(result.artifact).read_text())
    assert artifact["data_regime"] == "R1"
    assert len(artifact["compounds"]) == result.n_written

    # re-run is idempotent (INSERT OR REPLACE, no duplicates)
    run_compound_join(settings, force=True, scorer_config=FAST)
    conn = sqlite3.connect(settings.scenario_db_path)
    n = conn.execute("SELECT COUNT(*) FROM compound_scenarios").fetchone()[0]
    conn.close()
    assert n == result.n_written


# ---------------------------------------------------------------------------
# Pipeline integration
# ---------------------------------------------------------------------------


def test_pipeline_includes_compound_join_node(tmp_workspace):
    from contingency_atlas.pipeline import run_pipeline
    from contingency_atlas.config import Settings

    settings = Settings(
        scenario_db_path=tmp_workspace / "atlas.db",
        evidence_db_path=tmp_workspace / "event_evidence.db",
        emerging_risks_path=tmp_workspace / "emerging_risks.json",
        scenario_export_path=tmp_workspace / "scenario_export.json",
    )
    result = run_pipeline(settings, skip_llm=True)
    cj = result.state.get("compound_join")
    assert cj is not None
    # tmp_workspace has 1 risk + 12 evidence rows: sparse -> honest insufficiency
    assert cj["status"] in ("insufficient_data", "ok")
    summary = result.summary()
    assert "compound_join" in summary["phases"]
