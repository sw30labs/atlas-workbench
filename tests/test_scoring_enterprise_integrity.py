"""Regression witnesses for audit identity, missing losses and current portfolios."""

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from workbench_atlas import compound_join
from workbench_atlas.compound_graduate import (
    _build_graduation_prompt,
    eligible_compounds,
    migrate_graduations,
)
from workbench_atlas.config import Settings
from workbench_atlas.scoring.evaluate import evaluation_verdicts
from workbench_atlas.scoring.models import IncidentEvent, RiskAtom
from workbench_atlas.scoring.r1 import build_signal_series, run_r1_eval
from workbench_atlas.scoring.scorer import DetScorer, ScorerConfig
from workbench_atlas.scoring.synthetic import aggregate_incident_features


FAST = ScorerConfig(mc_draws=200, interval_draws=100, evt_enabled=False)


@pytest.fixture
def signal_store(tmp_path, monkeypatch):
    """Approved synthetic signals, with independent temporary output stores."""
    monkeypatch.setattr(compound_join, "TELEMETRY_PATH", tmp_path / "telemetry.jsonl")
    risks = [
        {
            "risk_id": f"risk-{i}", "issue": f"Cloud risk {i}",
            "channels": ["cloud"], "review_status": "approved",
            "source_type": "synthetic",
            "evidence_refs": [f"E{i}Q{q}" for q in range(8)],
        }
        for i in range(6)
    ]
    risks_path = tmp_path / "risks.json"
    risks_path.write_text(json.dumps(risks))
    evidence_db = tmp_path / "evidence.db"
    with sqlite3.connect(evidence_db) as conn:
        conn.execute(
            "CREATE TABLE evidence_events (event_id TEXT, source_date TEXT, "
            "loss_amount_usd REAL, review_status TEXT DEFAULT 'approved')"
        )
        conn.executemany(
            "INSERT INTO evidence_events(event_id, source_date, loss_amount_usd) VALUES(?,?,?)",
            [(f"E{i}Q{q}", f"{2024 + q // 4}-{1 + 3 * (q % 4):02d}-15", None)
             for i in range(6) for q in range(8)],
        )
    return Settings(
        scenario_db_path=tmp_path / "atlas.db",
        evidence_db_path=evidence_db, emerging_risks_path=risks_path,
    )


def _toy_score(events, config=FAST):
    atoms = [RiskAtom("a", "A"), RiskAtom("b", "B")]
    return DetScorer(config).fit(events, atoms, 8).score_pair("a", "b")


def _events():
    return [IncidentEvent("e1", "a", 0, 100), IncidentEvent("e2", "a", 1, 100),
            IncidentEvent("e3", "b", 1, 100), IncidentEvent("e4", "b", 2, 100)]


def test_realization_identity_tracks_config_severity_and_within_quarter_counts():
    events = _events()
    base = _toy_score(events)
    config_changed = _toy_score(events, replace(FAST, shrink_kappa=100))
    loss_changed = _toy_score([replace(event, loss_amount=10_000) for event in events])
    extra_same_quarter = _toy_score(events + [IncidentEvent("extra", "a", 0, 200)])
    assert config_changed.p_hat != base.p_hat
    assert loss_changed.loss_mean == pytest.approx(base.loss_mean * 100)
    assert len({s.realization_id for s in
                (base, config_changed, loss_changed, extra_same_quarter)}) == 4
    assert len({s.data_hash for s in (base, loss_changed, extra_same_quarter)}) == 3
    # Input order and unused holdout observations do not change a fitted result.
    assert _toy_score(list(reversed(events))).to_dict() == base.to_dict()
    assert _toy_score(events + [IncidentEvent("holdout", "a", 8, 1e9)]).to_dict() == base.to_dict()


def test_unknown_severity_roundtrips_and_does_not_become_a_dollar():
    event = IncidentEvent.from_dict({"event_id": "e", "risk_id": "a", "period": 0,
                                    "loss_amount": None})
    assert event.to_dict()["loss_amount"] is None
    score = _toy_score([replace(e, loss_amount=None) for e in _events()])
    assert score.loss_mean is None
    assert all(value is None for value in score.loss_quantiles.values())
    assert score.components["mc"]["loss_semantics"] == "activation_units"
    assert score.components["mc"]["hit_count"] > 0
    assert score.components["mc"]["loss_sample_count"] == 0
    assert all(a["n"] == 2 and a["n_severity"] == 0 for a in score.components["atoms"])


@pytest.mark.parametrize("amount", [-1, float("nan"), float("inf")])
def test_invalid_reported_losses_are_rejected(amount):
    with pytest.raises(ValueError, match="finite non-negative"):
        IncidentEvent("e", "a", 0, amount)


def test_aggregation_counts_occurrences_separately_from_known_losses():
    rows = aggregate_incident_features([
        IncidentEvent("e1", "a", 0, None), IncidentEvent("e2", "a", 0, 100),
        IncidentEvent("e3", "b", 0, None),
    ])
    assert (rows[0].count, rows[0].n_severity) == (2, 1)
    assert rows[0].severity_quantiles["0.5"] == 100
    assert (rows[1].count, rows[1].n_severity, rows[1].severity_quantiles) == (1, 0, None)


def test_holdout_loss_cannot_qualify_unknown_training_losses(signal_store):
    with sqlite3.connect(signal_store.evidence_db_path) as conn:
        conn.execute("UPDATE evidence_events SET loss_amount_usd = 1000000 WHERE event_id = 'E0Q7'")
    result = compound_join.run_compound_join(signal_store, force=True, scorer_config=FAST)
    assert result.status == "ok", result.errors
    report = json.loads(Path(result.artifact).read_text())
    assert report["loss_units"] == "activation"
    assert all(row["loss_quantiles"] is None for row in report["compounds"])
    with sqlite3.connect(signal_store.scenario_db_path) as conn:
        assert conn.execute(
            "SELECT count(*) FROM compound_scenarios WHERE loss_q95 IS NOT NULL"
        ).fetchone()[0] == 0
        history = conn.execute("SELECT realization_json FROM score_realizations").fetchall()
    assert all(json.loads(row[0])["loss_mean"] is None for row in history)


def test_loss_sufficiency_is_per_constituent(signal_store):
    with sqlite3.connect(signal_store.evidence_db_path) as conn:
        conn.execute(
            "UPDATE evidence_events SET loss_amount_usd = 10000 "
            "WHERE event_id IN ('E0Q0','E0Q1','E1Q0','E1Q1')"
        )
    result = compound_join.run_compound_join(signal_store, force=True, scorer_config=FAST)
    assert result.status == "ok", result.errors
    with sqlite3.connect(signal_store.scenario_db_path) as conn:
        rows = conn.execute("SELECT risk_ids, loss_units, loss_q95 FROM compound_scenarios").fetchall()
    monetary = [(json.loads(ids), q95) for ids, units, q95 in rows if units == "dollars"]
    assert len(monetary) == 1
    assert monetary[0][0] == ["risk-0", "risk-1"]
    assert monetary[0][1] > 0
    assert all(q95 is None for _ids, units, q95 in rows if units == "activation")


@pytest.mark.parametrize("persisted", [True, False])
def test_same_title_cannot_transfer_approval_or_evidence(signal_store, persisted):
    rows = [
        {"issue": "Shared title", "review_status": "approved", "evidence_refs": ["E0Q0"]},
        {"issue": "Shared title", "review_status": "pending", "evidence_refs": ["E1Q0"]},
        {"issue": "Shared title", "review_status": "approved", "evidence_refs": ["E2Q0"]},
    ]
    if persisted:
        for i, row in enumerate(rows):
            row["risk_id"] = f"id-{i}"
    signal_store.emerging_risks_path.write_text(json.dumps(rows))
    atoms, events, _ = build_signal_series(signal_store.emerging_risks_path, signal_store.evidence_db_path)
    expected = {"id-0", "id-2"} if persisted else {"shared-title", "shared-title-3"}
    assert {a.risk_id for a in atoms} == expected
    assert {e.risk_id for e in events} == expected
    assert len(events) == 2


def test_graph_growth_supersedes_current_pairs_and_preserves_graduations(signal_store):
    first = compound_join.run_compound_join(signal_store, force=True, scorer_config=FAST)
    assert first.status == "ok"
    migrate_graduations(str(signal_store.scenario_db_path))
    with sqlite3.connect(signal_store.scenario_db_path) as conn:
        old_id, pair_id = conn.execute("SELECT compound_id, pair_id FROM compound_scenarios LIMIT 1").fetchone()
        conn.execute(
            "INSERT INTO compound_graduations "
            "(compound_id, scenario_id, run_id, created_at, eligibility, judge_score) VALUES(?,?,?,?,?,?)",
            (old_id, "existing-scenario", "first", "2026-01-01", "{}", 1),
        )
    risks = json.loads(signal_store.emerging_risks_path.read_text())
    risks.append({"risk_id": "unrelated", "issue": "Unrelated", "channels": ["other"],
                  "review_status": "approved", "evidence_refs": ["E0Q0"]})
    signal_store.emerging_risks_path.write_text(json.dumps(risks))
    second = compound_join.run_compound_join(signal_store, force=True, scorer_config=FAST)
    assert second.status == "ok", second.errors
    with sqlite3.connect(signal_store.scenario_db_path) as conn:
        assert conn.execute("SELECT count(*) FROM compound_scenarios WHERE status = 'candidate'").fetchone()[0] == 15
        assert conn.execute("SELECT count(*) FROM compound_scenarios WHERE status = 'superseded'").fetchone()[0] == 15
        assert conn.execute("SELECT count(*) FROM score_realizations").fetchone()[0] == 30
    eligible, _ = eligible_compounds(signal_store)
    assert len(eligible) == 14
    assert pair_id not in {row["pair_id"] for row in eligible}


def test_config_and_data_changes_append_history(signal_store):
    for cfg in (FAST, replace(FAST, shrink_kappa=10)):
        assert compound_join.run_compound_join(signal_store, force=True, scorer_config=cfg).status == "ok"
    with sqlite3.connect(signal_store.evidence_db_path) as conn:
        conn.execute("UPDATE evidence_events SET loss_amount_usd = 1000")
    assert compound_join.run_compound_join(signal_store, force=True, scorer_config=FAST).status == "ok"
    with sqlite3.connect(signal_store.scenario_db_path) as conn:
        assert conn.execute("SELECT count(*) FROM score_realizations").fetchone()[0] == 45
        assert conn.execute("SELECT count(*) FROM compound_scenarios WHERE status = 'candidate'").fetchone()[0] == 15


def test_legacy_current_duplicates_are_retained_as_superseded(signal_store):
    assert compound_join.run_compound_join(signal_store, force=True, scorer_config=FAST).status == "ok"
    with sqlite3.connect(signal_store.scenario_db_path) as conn:
        conn.execute("DROP INDEX idx_compound_current_pair")
        conn.execute(
            "INSERT INTO compound_scenarios "
            "(compound_id, risk_ids, state_hash, pair_id, status, scored_at) "
            "SELECT 'legacy-duplicate', risk_ids, state_hash, pair_id, 'candidate', '1900-01-01' "
            "FROM compound_scenarios LIMIT 1"
        )
    compound_join.migrate_compound(str(signal_store.scenario_db_path))
    with sqlite3.connect(signal_store.scenario_db_path) as conn:
        assert conn.execute(
            "SELECT status FROM compound_scenarios WHERE compound_id = 'legacy-duplicate'"
        ).fetchone()[0] == "superseded"
        assert conn.execute("SELECT count(*) FROM compound_scenarios").fetchone()[0] == 16
        assert conn.execute("SELECT count(*) FROM compound_scenarios WHERE status = 'candidate'").fetchone()[0] == 15


def test_identity_collision_rolls_back_current_portfolio(signal_store, monkeypatch):
    assert compound_join.run_compound_join(signal_store, force=True, scorer_config=FAST).status == "ok"
    with sqlite3.connect(signal_store.scenario_db_path) as conn:
        before = conn.execute("SELECT * FROM compound_scenarios ORDER BY compound_id").fetchall()
    original = DetScorer.score_combo

    def conflicting_payload(self, risk_ids):
        result = original(self, risk_ids)
        result.p_hat /= 2  # different payload falsely claiming the prior identity
        return result

    monkeypatch.setattr(DetScorer, "score_combo", conflicting_payload)
    result = compound_join.run_compound_join(signal_store, force=True, scorer_config=FAST)
    assert result.status == "error"
    assert "identity collision" in result.note
    with sqlite3.connect(signal_store.scenario_db_path) as conn:
        assert conn.execute("SELECT * FROM compound_scenarios ORDER BY compound_id").fetchall() == before
        assert conn.execute("SELECT count(*) FROM score_realizations").fetchone()[0] == 15


def test_calibration_failure_is_not_hidden_by_ranking_success():
    verdicts = evaluation_verdicts(True, {"modes": {"test": {"pass": False, "coverage_90": 0.39}}}, "test")
    assert verdicts["ranking"]["status"] == "pass"
    assert verdicts["calibration"]["status"] == "fail"
    assert verdicts["enterprise_readiness"]["status"] == "fail"
    unvalidated = evaluation_verdicts(True, None, "test")
    assert unvalidated["enterprise_readiness"]["status"] == "not_validated"


def test_small_sample_markdown_matches_scoped_verdicts(signal_store):
    result = run_r1_eval(signal_store.emerging_risks_path, signal_store.evidence_db_path, config=FAST)
    assert result.report["small_sample"] is True
    assert result.report["verdicts"]["ranking"]["status"] == "not_validated"
    assert result.report["recommendation_scope"] == "ranking_and_structure_only"
    assert "ITERATE" in result.report_md
    assert "NOT_VALIDATED" in result.report_md
    assert "directional" in result.report_md


def test_graduation_prompt_does_not_claim_operational_causality():
    prompt = _build_graduation_prompt(
        {"risk_ids": '["a", "b"]', "uncertainty_class": "mixed", "dependence_support": 3},
        {"a": "A", "b": "B"},
    )
    text = " ".join(message["content"] for message in prompt)
    assert "PROVEN" not in text
    assert "explicitly unverified" in text
    assert "does not establish operational co-materialization" in text
