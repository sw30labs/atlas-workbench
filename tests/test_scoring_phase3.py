"""Phase 3 tests: Task A calibration harness, R1 signal series, model
inventory."""

import json
import shutil
import sqlite3
from pathlib import Path

import pytest
import yaml

from workbench_atlas.scoring.calibration import (
    _poisson_cdf,
    _poisson_quantile,
    calibrate_atoms,
)
from workbench_atlas.provenance import is_approved_record
from workbench_atlas.scoring.channels import build_channel_graph
from workbench_atlas.scoring.models import IncidentEvent
from workbench_atlas.scoring.r1 import (
    SMALL_SAMPLE_MAX_POSITIVES,
    build_signal_series,
    run_r1_eval,
)
from workbench_atlas.scoring.scorer import DetScorer, ScorerConfig
from workbench_atlas.scoring.synthetic import R0Config, generate_r0

FAST = ScorerConfig(mc_draws=300, interval_draws=300)
REPO = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Task A calibration
# ---------------------------------------------------------------------------


def test_poisson_helpers():
    assert _poisson_quantile(0.1, 0.5) == 0
    assert _poisson_quantile(3.0, 0.95) >= 5
    assert abs(_poisson_cdf(2.0, 2) - 0.6767) < 0.001


def test_calibration_runs_on_r0_and_reports_all_views():
    ds = generate_r0(R0Config())
    res = calibrate_atoms(
        ds.events, [a.risk_id for a in ds.atoms],
        train_quarters=32, n_periods=40,
    )
    r = res.report
    assert r["n_atoms"] == 15
    assert r["n_observations"] == 15 * 8
    assert 0.0 <= r["coverage_90"] <= 1.0
    assert len(r["pit_histogram"]) == 10
    assert "brier_model" in r and "brier_naive" in r
    # the sparse-regime caveats must be surfaced, not hidden
    assert r["interpretation"]


def test_calibration_perfect_model_beats_naive():
    """i.i.d. Poisson cells with heterogeneous rates: per-atom fits must beat
    the pooled naive rate on deviance (the harness can pass when the atom
    model is right)."""
    import math
    import random

    def poisson_draw(lam: float, rng: random.Random) -> int:
        # Knuth sampler, fine for small lam
        threshold = math.exp(-lam)
        k, p = 0, 1.0
        while p > threshold:
            k += 1
            p *= rng.random()
        return k - 1

    rng = random.Random(7)
    events = []
    seq = 0
    rates = {"a": 2.0, "b": 0.2}
    for t in range(60):
        for r, lam in rates.items():
            for _ in range(poisson_draw(lam, rng)):
                seq += 1
                events.append(IncidentEvent(f"e{seq}", r, t, 100.0))

    res = calibrate_atoms(events, ["a", "b"], train_quarters=50, n_periods=60)
    assert res.report["nll_beats_naive"] is True
    assert res.report["brier_beats_naive"] is True


# ---------------------------------------------------------------------------
# R1 signal series
# ---------------------------------------------------------------------------


def test_r1_repo_data_small_sample_caveat_tracks_the_holdout(tmp_path):
    """The real store: the machinery runs, and the directional-only caveat is
    applied exactly when the holdout is too small to read as a verdict.

    The register is live runtime data (ADR-012), so this asserts the caveat is
    *consistent with* the holdout size rather than freezing the size itself.
    The original version hardcoded a tiny holdout and broke the moment the
    register grew past the threshold — which also hid that the caveat had
    stopped applying, and with it a real pass/fail verdict.
    """
    # Same gitignored runtime artifacts as test_load_risk_atoms_repo_file
    # (ADR-012): absent on a clean checkout, so absent in CI. This test has
    # been red on main since 2026-08-04 for exactly this reason.
    register = REPO / "emerging_risks.json"
    evidence = REPO / "event_evidence.db"
    if not register.exists() or not evidence.exists():
        pytest.skip("register/evidence store are gitignored runtime artifacts")

    evidence_copy = tmp_path / "event_evidence.db"
    shutil.copy2(evidence, evidence_copy)
    result = run_r1_eval(register, evidence_copy)
    report = result.report
    if report.get("status") == "insufficient_data":
        assert report["reasons"]
    else:
        assert report["data_regime"] == "R1"
        n_pos = report["task_b"]["n_holdout_positives"]
        if n_pos < SMALL_SAMPLE_MAX_POSITIVES:
            assert report["small_sample"] is True
            assert report["recommendation"] == "iterate"
            assert "directional" in report["interpretation"]
        else:
            # Enough holdout signal to read normally. The caveat must NOT be
            # asserted here, or a real verdict would ship dressed as
            # directional — the failure mode this test exists to prevent.
            assert report.get("small_sample") is not True
            assert report["recommendation"] in {"go", "iterate", "abandon"}
            assert isinstance(report["task_b"]["pass"], bool)
    # Approved-only contract: the signal series consumes only records that
    # have completed human review (same invariant as phases 3/5/6), so the
    # expectation is the approved subset, not the whole register.
    raw = json.loads((REPO / "emerging_risks.json").read_text())
    expected_n_risks = sum(1 for r in raw if is_approved_record(r))
    assert report["diagnostics"]["n_risks"] == expected_n_risks
    assert report["diagnostics"]["n_activations"] >= 0
    if expected_n_risks == 0:
        assert report.get("status") == "insufficient_data"


def test_small_sample_caveat_is_applied_on_a_sparse_register(tmp_path):
    """Deterministic cover for the caveat itself.

    A tiny fixture may exercise this by accident;
    it no longer is, so the override that turns a thin holdout into
    "directional, not a verdict" needs a fixture that is small on purpose.
    """
    n_risks, n_quarters = 5, 12
    risks = [
        {
            "issue": f"risk {i}",
            "description": "synthetic",
            "review_status": "approved",
            "evidence_refs": [f"E{i}q{q}" for q in range(n_quarters)],
        }
        for i in range(n_risks)
    ]
    (tmp_path / "risks.json").write_text(json.dumps(risks))
    db = tmp_path / "ev.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE evidence_events (event_id TEXT, source_date TEXT, loss_amount_usd REAL,"
        " review_status TEXT DEFAULT 'approved')"
    )
    for i in range(n_risks):
        for q in range(n_quarters):
            month = 1 + (q % 4) * 3
            year = 2022 + q // 4
            conn.execute(
                "INSERT INTO evidence_events (event_id, source_date, loss_amount_usd)"
                " VALUES (?,?,?)",
                (f"E{i}q{q}", f"{year}-{month:02d}-15", 1000.0),
            )
    conn.commit()
    conn.close()

    report = run_r1_eval(tmp_path / "risks.json", db).report
    # Must actually score: an insufficient_data short-circuit would make the
    # assertions below vacuous and leave the override uncovered again.
    assert report.get("status") != "insufficient_data"
    assert 0 < report["task_b"]["n_holdout_positives"] < SMALL_SAMPLE_MAX_POSITIVES
    assert report["small_sample"] is True
    assert report["recommendation"] == "iterate"
    assert "directional" in report["interpretation"]


def test_r1_series_building_on_dense_fixture(tmp_path):
    risks = [
        {"issue": f"risk {i}", "description": "synthetic", "review_status": "approved",
         "evidence_refs": [f"E{i}a", f"E{i}b"]}
        for i in range(6)
    ]
    (tmp_path / "risks.json").write_text(json.dumps(risks))
    db = tmp_path / "ev.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE evidence_events (event_id TEXT, source_date TEXT, loss_amount_usd REAL,"
        " review_status TEXT DEFAULT 'approved')"
    )
    # 6 risks x 8 quarters of activations
    for i in range(6):
        for q in range(8):
            month = 1 + q * 3
            year = 2024 + (month - 1) // 12
            month = (month - 1) % 12 + 1
            for suffix in ("a", "b"):
                conn.execute(
                    "INSERT INTO evidence_events (event_id, source_date, loss_amount_usd) VALUES (?,?,?)",
                    (f"E{i}{suffix}" if q == 0 else f"E{i}{suffix}{q}",
                     f"{year}-{month:02d}-15", 1000.0),
                )
    # fix refs to point at existing rows: give every risk events each quarter
    conn.commit()
    conn.close()
    atoms, events, diag = build_signal_series(tmp_path / "risks.json", db)
    # only first-quarter refs resolve (refs E{i}a/E{i}b) — linkage is exact
    assert diag["n_activations"] == 12
    assert {e.period for e in events} == {0}


# ---------------------------------------------------------------------------
# Model inventory
# ---------------------------------------------------------------------------


def test_model_inventory_entry():
    data = yaml.safe_load((REPO / "docs/research/redesignv1/model-inventory.yaml").read_text())
    for key in ("model_id", "name", "version", "tier", "owner", "purpose", "restrictions", "validation"):
        assert key in data, key
    assert data["tier"] == "research"
    assert any("workshop" in r for r in data["restrictions"])
    assert data["validation"]["status"]
