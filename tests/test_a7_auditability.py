"""A7 production-auditability tests (HYBRID_SCORING_PROPOSAL.md, wave 1).

Covers: stable pair_id, quarter-hashed horizon, data_hash, dual-realization
identity keys, config-hash coverage of new knobs, append-only realization
history with the current view on top, regime-aware loss suppression, the
k-uniform graduation attestation, provenance-based urgency exclusion,
and derived graduation metadata.
"""

import json
import sqlite3
from pathlib import Path

import pytest

from workbench_atlas.compound_graduate import (
    _derive_compound_metadata,
    eligible_compounds,
    migrate_graduations,
    run_compound_graduation,
)
from workbench_atlas.compound_join import migrate_compound, run_compound_join
from workbench_atlas.config import Settings
from workbench_atlas.db import upsert_scenario
from workbench_atlas.schema import migrate
from workbench_atlas.scoring.inventory import slugify
from workbench_atlas.scoring.models import (
    IncidentEvent,
    RiskAtom,
    pair_id_for,
)
from workbench_atlas.scoring.scorer import DetScorer, ScorerConfig

FAST = ScorerConfig(mc_draws=300, interval_draws=300)


# ---------------------------------------------------------------------------
# Identity keys
# ---------------------------------------------------------------------------


def test_pair_id_stable_and_membership_sensitive():
    a = pair_id_for(["risk-b", "risk-a"])
    b = pair_id_for(["risk-a", "risk-b"])
    assert a == b  # order-independent
    assert a.startswith("pair-")
    assert pair_id_for(["risk-a", "risk-c"]) != a  # membership-sensitive
    # k-agnostic: a triple has its own stable id, distinct from any pair
    assert pair_id_for(["risk-a", "risk-b", "risk-c"]) not in (a,)


def _fit_scorer(events, atoms, n_periods, **cfg_kw):
    cfg = ScorerConfig(mc_draws=50, interval_draws=50, **cfg_kw)
    return DetScorer(cfg).fit(events, atoms, n_periods), cfg


def _toy_atoms():
    return [
        RiskAtom(risk_id="atom-a", title="atom a", channels=["ch"]),
        RiskAtom(risk_id="atom-b", title="atom b", channels=["ch"]),
    ]


def _toy_events(extra: bool = False):
    events = [
        IncidentEvent("e1", "atom-a", 0, 100.0),
        IncidentEvent("e2", "atom-a", 1, 100.0),
        IncidentEvent("e3", "atom-b", 1, 100.0),
        IncidentEvent("e4", "atom-b", 2, 100.0),
    ]
    if extra:
        events.append(IncidentEvent("e5", "atom-a", 3, 100.0))
    return events


def test_horizon_hashed_in_quarters_not_floor_years():
    """5 quarters must NOT collapse to the 1y identity of 4 quarters."""
    atoms = _toy_atoms()
    scorer4, _ = _fit_scorer(_toy_events(), atoms, 8, horizon_quarters=4)
    scorer5, _ = _fit_scorer(_toy_events(), atoms, 8, horizon_quarters=5)
    rlz4 = scorer4.score_pair("atom-a", "atom-b")
    rlz5 = scorer5.score_pair("atom-a", "atom-b")
    assert rlz4.state_hash != rlz5.state_hash  # was identical ("1y") pre-A7


def test_data_hash_tracks_fitted_series():
    atoms = _toy_atoms()
    scorer_a, _ = _fit_scorer(_toy_events(), atoms, 8)
    scorer_b, _ = _fit_scorer(_toy_events(extra=True), atoms, 8)
    scorer_c, _ = _fit_scorer(_toy_events(), atoms, 8)
    assert scorer_a.data_hash
    assert scorer_a.data_hash != scorer_b.data_hash  # extra train event
    assert scorer_a.data_hash == scorer_c.data_hash  # identical data


def test_realization_identity_carries_dual_realization_keys():
    atoms = _toy_atoms()
    scorer, _ = _fit_scorer(_toy_events(), atoms, 8)
    rlz = scorer.score_pair("atom-a", "atom-b")
    assert rlz.judgment_version == "det"
    assert rlz.data_hash == scorer.data_hash
    blob = rlz.to_dict()
    assert blob["data_hash"] == scorer.data_hash
    assert blob["judgment_version"] == "det"
    # same state, different fitted data => different realization id (the
    # dual-realization key: no collision under one state hash)
    scorer2, _ = _fit_scorer(_toy_events(extra=True), atoms, 8)
    rlz2 = scorer2.score_pair("atom-a", "atom-b")
    assert rlz2.realization_id != rlz.realization_id


def test_config_hash_covers_new_knobs():
    base = ScorerConfig()
    assert base.config_hash() != ScorerConfig(interval_draws=100).config_hash()
    assert base.config_hash() != ScorerConfig(beta_floor=1e-5).config_hash()
    assert base.config_hash() != ScorerConfig(judgment_mode="rank_only").config_hash()


def test_judgment_mode_socket_raises_above_off():
    atoms = _toy_atoms()
    scorer, _ = _fit_scorer(_toy_events(), atoms, 8, judgment_mode="priors")
    with pytest.raises(NotImplementedError):
        scorer.score_pair("atom-a", "atom-b")


# ---------------------------------------------------------------------------
# Join fixtures
# ---------------------------------------------------------------------------


def _make_settings(tmp_path, risks, evidence_rows):
    risks_path = tmp_path / "emerging_risks.json"
    risks_path.write_text(json.dumps(risks))
    ev_db = tmp_path / "event_evidence.db"
    conn = sqlite3.connect(ev_db)
    conn.execute(
        "CREATE TABLE evidence_events (event_id TEXT, source_date TEXT, loss_amount_usd REAL,"
        " review_status TEXT DEFAULT 'approved')"
    )
    conn.executemany(
        "INSERT INTO evidence_events (event_id, source_date, loss_amount_usd) VALUES (?,?,?)",
        evidence_rows,
    )
    conn.commit()
    conn.close()
    return Settings(
        scenario_db_path=tmp_path / "atlas.db",
        evidence_db_path=ev_db,
        emerging_risks_path=risks_path,
    )


def _dense_fixture(tmp_path, *, with_losses: bool):
    """6 risks x 8 quarters, two channel groups; losses optional."""
    channels = {0: "identity", 1: "identity", 2: "cloud", 3: "identity", 4: "cloud", 5: "cloud"}
    risks = [
        {
            "issue": f"a7 test risk {i} ({channels[i]})",
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
            if q == (i % 8):
                continue
            month = q * 3 + 2
            year = 2024 + (month - 1) // 12
            month = (month - 1) % 12 + 1
            loss = 10_000.0 * (i + 1) if with_losses else None
            rows.append((f"E{i}q{q}", f"{year}-{month:02d}-15", loss))
    return risks, rows


# ---------------------------------------------------------------------------
# Append-only history + current view
# ---------------------------------------------------------------------------


def test_join_writes_history_under_current_view(tmp_path):
    risks, rows = _dense_fixture(tmp_path, with_losses=True)
    settings = _make_settings(tmp_path, risks, rows)
    result = run_compound_join(settings, force=True, scorer_config=FAST)
    assert result.status == "ok", result.note

    conn = sqlite3.connect(settings.scenario_db_path)
    conn.row_factory = sqlite3.Row
    history = conn.execute("SELECT * FROM score_realizations").fetchall()
    current = conn.execute("SELECT * FROM compound_scenarios").fetchall()
    assert len(history) == result.n_written == len(current)
    for row in history:
        assert row["pair_id"] and row["pair_id"].startswith("pair-")
        assert row["judgment_version"] == "det"
        assert row["data_hash"]
        blob = json.loads(row["realization_json"])
        assert blob["realization_id"] == row["realization_id"]
        assert blob["components"]["mc"]["loss_semantics"] == "dollars"
    for row in current:
        assert row["pair_id"] and row["data_hash"]
        assert row["loss_units"] == "dollars"

    # append-only is enforced, not conventional
    rid = history[0]["realization_id"]
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE score_realizations SET p_hat = 0.99 WHERE realization_id = ?",
            (rid,),
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "DELETE FROM score_realizations WHERE realization_id = ?", (rid,)
        )
    conn.close()

    # forced re-run with identical data: no duplicate history rows
    again = run_compound_join(settings, force=True, scorer_config=FAST)
    assert again.status == "ok"
    conn = sqlite3.connect(settings.scenario_db_path)
    n_hist = conn.execute("SELECT COUNT(*) FROM score_realizations").fetchone()[0]
    n_cur = conn.execute("SELECT COUNT(*) FROM compound_scenarios").fetchone()[0]
    conn.close()
    assert n_hist == result.n_written
    assert n_cur == result.n_written

    artifact = json.loads(Path(result.artifact).read_text())
    assert artifact["loss_units"] == "dollars"
    for row in artifact["compounds"]:
        assert row["pair_id"].startswith("pair-")
        assert row["judgment_version"] == "det"
        assert row["data_hash"]
        assert row["loss_units"] == "dollars"
        assert row["loss_quantiles"] is not None


# ---------------------------------------------------------------------------
# Regime-aware loss suppression
# ---------------------------------------------------------------------------


def test_activation_regime_suppresses_dollar_losses(tmp_path):
    risks, rows = _dense_fixture(tmp_path, with_losses=False)
    settings = _make_settings(tmp_path, risks, rows)
    result = run_compound_join(settings, force=True, scorer_config=FAST)
    assert result.status == "ok", result.note

    conn = sqlite3.connect(settings.scenario_db_path)
    conn.row_factory = sqlite3.Row
    current = conn.execute("SELECT * FROM compound_scenarios").fetchall()
    assert current, "expected scored compounds"
    for row in current:
        assert row["loss_q50"] is None
        assert row["loss_q95"] is None
        assert row["loss_mean"] is None
        # C-08: the current view carries the export spelling, not the
        # internal regime name ("activation_units" was the drift spelling).
        assert row["loss_units"] == "activation"
    conn.close()

    artifact = json.loads(Path(result.artifact).read_text())
    assert artifact["loss_units"] == "activation"
    for row in artifact["compounds"]:
        assert row["loss_quantiles"] is None
        assert row["loss_units"] == "activation"


def test_dollars_regime_keeps_losses(tmp_path):
    risks, rows = _dense_fixture(tmp_path, with_losses=True)
    settings = _make_settings(tmp_path, risks, rows)
    result = run_compound_join(settings, force=True, scorer_config=FAST)
    assert result.status == "ok", result.note
    conn = sqlite3.connect(settings.scenario_db_path)
    conn.row_factory = sqlite3.Row
    current = conn.execute("SELECT * FROM compound_scenarios").fetchall()
    conn.close()
    assert all(row["loss_units"] == "dollars" for row in current)
    assert any(row["loss_q95"] is not None for row in current)


# ---------------------------------------------------------------------------
# k-uniform graduation attestation
# ---------------------------------------------------------------------------

_TRIPLE_RISKS = [
    {"issue": "triple risk alpha", "description": "a",
     "evidence_refs": [f"TA{i}" for i in range(1, 7)], "source_type": "synthetic"},
    {"issue": "triple risk beta", "description": "b",
     "evidence_refs": [f"TB{i}" for i in range(1, 7)], "source_type": "synthetic"},
    {"issue": "triple risk gamma", "description": "c",
     "evidence_refs": [f"TG{i}" for i in range(1, 7)], "source_type": "synthetic"},
]

_QUARTERS = [(2024, 2), (2024, 5), (2024, 8), (2024, 11), (2025, 2), (2025, 5)]


def _triple_news(cooccur_in_holdout: bool):
    """All three atoms fire every quarter; without holdout co-firing, gamma
    goes silent in the last two (holdout) quarters."""
    rows = []
    for i, (y, m) in enumerate(_QUARTERS):
        rows.append((f"TA{i + 1}", f"{y}-{m:02d}-15", 1000.0))
        rows.append((f"TB{i + 1}", f"{y}-{m:02d}-15", 1000.0))
        if cooccur_in_holdout or i < 4:
            rows.append((f"TG{i + 1}", f"{y}-{m:02d}-15", 1000.0))
    return rows


def _triple_ids():
    return sorted(slugify(r["issue"]) for r in _TRIPLE_RISKS)


def _triple_settings(tmp_path, cooccur_in_holdout: bool):
    tmp_path = Path(tmp_path)
    tmp_path.mkdir(parents=True, exist_ok=True)
    risks = [
        {**r, "review_status": "approved", "signal_maturity": "established",
         "momentum": "stable"}
        for r in _TRIPLE_RISKS
    ]
    settings = _make_settings(tmp_path, risks, _triple_news(cooccur_in_holdout))
    migrate(str(settings.scenario_db_path))
    migrate_compound(str(settings.scenario_db_path))
    migrate_graduations(str(settings.scenario_db_path))
    return settings


def _insert_candidate(settings, risk_ids, compound_id="scn-triple1"):
    conn = sqlite3.connect(settings.scenario_db_path)
    conn.execute(
        "INSERT INTO compound_scenarios (compound_id, risk_ids, state_hash,"
        " p_hat, p_lo, p_hi, loss_q50, loss_q95, loss_mean, uncertainty_class,"
        " dependence_support, evidence_support, data_regime, status, run_id,"
        " scored_at, algorithm_version, provenance_note, pair_id, loss_units)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            compound_id, json.dumps(sorted(risk_ids)), "triplehash",
            0.3, 0.1, 0.5, 1e5, 9e5, 3e5, "mixed", 3, 0.6, "R1",
            "candidate", "run-t", "2026-08-01T00:00:00+00:00",
            "0.1.0", "test", pair_id_for(sorted(risk_ids)), "dollars",
        ),
    )
    conn.commit()
    conn.close()


class _GradPassClient:
    """Draft + judge pass, no network."""

    def __init__(self):
        self.settings = Settings()

    def chat_json(self, messages, **kwargs):
        user = messages[-1]["content"]
        if "Write the scenario document" in user:
            return {"title": "triple compound", "description": "doc"}
        return {"score": 0.9, "passed": True, "feedback": "ok"}


def test_k3_holdout_survival_is_computed_not_bypassed(tmp_path):
    # surviving triple: all three fire together in a holdout quarter
    settings = _triple_settings(tmp_path / "surviving", cooccur_in_holdout=True)
    _insert_candidate(settings, _triple_ids())
    eligible, _ = eligible_compounds(settings)
    assert len(eligible) == 1
    evidence = eligible[0]["eligibility_evidence"]
    assert evidence["k"] == 3
    assert evidence["holdout_survival"] is True
    assert evidence["holdout_matching_quarters"]

    # non-surviving triple: gamma silent in holdout => NOT eligible
    # (pre-A7 the k=3 row bypassed the check entirely)
    settings2 = _triple_settings(tmp_path / "nonsurviving", cooccur_in_holdout=False)
    _insert_candidate(settings2, _triple_ids())
    eligible2, _ = eligible_compounds(settings2)
    assert eligible2 == []


def test_k3_graduation_records_honest_attestation(tmp_path):
    settings = _triple_settings(tmp_path, cooccur_in_holdout=True)
    _insert_candidate(settings, _triple_ids())
    result = run_compound_graduation(settings, _GradPassClient())
    assert result.status == "ok", result.errors

    conn = sqlite3.connect(settings.scenario_db_path)
    row = conn.execute(
        "SELECT eligibility FROM compound_graduations WHERE compound_id = 'scn-triple1'"
    ).fetchone()
    conn.close()
    attestation = json.loads(row[0])
    assert attestation["holdout_survival"] is True
    assert attestation["k"] == 3
    assert attestation["holdout_window_quarters"] == 2
    assert attestation["holdout_matching_quarters"]
    assert attestation["promoted"] is True

    # graduated scenario carries derived metadata, never hardcoded, and the
    # constituent records all agree on established/stable here
    conn = sqlite3.connect(settings.scenario_db_path)
    conn.row_factory = sqlite3.Row
    scen = conn.execute(
        "SELECT * FROM scenarios WHERE scenario_id LIKE 'ATLAS-CMP-%'"
    ).fetchone()
    conn.close()
    assert scen is not None
    assert scen["signal_maturity"] == "established"
    assert scen["momentum"] == "stable"
    assert scen["evidence_strength"] in ("limited", "moderate", "strong")
    assert scen["financial_impact_band"] == "Limited"  # q95 9e5 banded per docs/reference/TAXONOMY.md (C-01)


def test_activation_regime_graduation_omits_dollar_band(tmp_path):
    settings = _triple_settings(tmp_path, cooccur_in_holdout=True)
    conn = sqlite3.connect(settings.scenario_db_path)
    conn.execute(
        "INSERT INTO compound_scenarios (compound_id, risk_ids, state_hash,"
        " p_hat, p_lo, p_hi, loss_q50, loss_q95, loss_mean, uncertainty_class,"
        " dependence_support, evidence_support, data_regime, status, run_id,"
        " scored_at, algorithm_version, provenance_note, pair_id, loss_units)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "scn-act1", json.dumps(_triple_ids()), "acthash",
            0.3, 0.1, 0.5, None, None, None, "mixed", 3, 0.6, "R1",
            "candidate", "run-t", "2026-08-01T00:00:00+00:00",
            "0.1.0", "test", pair_id_for(_triple_ids()), "activation_units",
        ),
    )
    conn.commit()
    conn.close()
    result = run_compound_graduation(settings, _GradPassClient())
    assert result.status == "ok", result.errors
    conn = sqlite3.connect(settings.scenario_db_path)
    conn.row_factory = sqlite3.Row
    scen = conn.execute(
        "SELECT * FROM scenarios WHERE scenario_id LIKE 'ATLAS-CMP-%'"
    ).fetchone()
    conn.close()
    assert scen is not None
    assert scen["financial_impact_band"] is None  # never "$0.0M"


# ---------------------------------------------------------------------------
# Phase-4 exclusion + derived metadata
# ---------------------------------------------------------------------------


def test_phase4_excludes_graduated_compounds(tmp_workspace, mock_omlox_client):
    from workbench_atlas.phases.phase4_prioritize import run_prioritization_cycle

    db = str(tmp_workspace / "atlas.db")
    upsert_scenario(db, {
        "scenario_id": "ATLAS-TEST-001",
        "origin": "operator_authored",
        "status": "active",
        "title": "ordinary scenario",
        "likelihood": "High",
        "financial_impact_band": "Severe",
    })
    upsert_scenario(db, {
        "scenario_id": "GRADUATED-WITH-NEUTRAL-ID",
        "origin": "compound_graduation",
        "status": "active",
        "title": "graduated compound",
        "likelihood": "p̂ 0.420 [0.200–0.600] (research)",
    })
    result = run_prioritization_cycle(db, mock_omlox_client)
    assert result.updated >= 1

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    rows = {
        r["scenario_id"]: r
        for r in conn.execute(
            "SELECT scenario_id, urgency_score, last_scored FROM scenarios"
        ).fetchall()
    }
    conn.close()
    assert rows["ATLAS-TEST-001"]["urgency_score"] is not None
    # graduated compounds keep their research scores; legacy urgency never
    # touches them (no fallback-default laundering)
    assert rows["GRADUATED-WITH-NEUTRAL-ID"]["urgency_score"] is None
    assert rows["GRADUATED-WITH-NEUTRAL-ID"]["last_scored"] is None


def test_derive_compound_metadata_rules():
    rec_a = {"signal_maturity": "established", "momentum": "stable",
             "evidence_refs": ["E1", "E2"]}
    rec_b = {"signal_maturity": "established", "momentum": "stable",
             "evidence_refs": ["E2", "E3"]}
    meta = _derive_compound_metadata([rec_a, rec_b])
    assert meta["signal_maturity"] == "established"
    assert meta["momentum"] == "stable"
    assert meta["evidence_strength"] == "strong"  # 3 distinct refs

    # disagreement omits rather than invents
    rec_c = {"signal_maturity": "weak", "momentum": "slowing", "evidence_refs": []}
    meta2 = _derive_compound_metadata([rec_a, rec_c])
    assert "signal_maturity" not in meta2
    assert "momentum" not in meta2
    assert meta2["evidence_strength"] == "moderate"  # E1, E2

    # missing values omit
    assert "signal_maturity" not in _derive_compound_metadata([{}])
