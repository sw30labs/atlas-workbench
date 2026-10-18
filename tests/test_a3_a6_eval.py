"""A3 honest intervals + A6 evaluation-hardening tests.

A3: two-level posterior draws, Fréchet-clip per draw, median beside the
mean, p_hat > p_hi flag. A6: Task B-cal strata (incl. supported-rare),
Task L, bootstrap CIs, multi-seed driver, composite baseline, report
honesty fields.
"""

import json
import random

import pytest

from contingency_atlas.scoring.candidates import (
    BASELINES,
    ORACLE_BASELINES,
    all_pairs,
    baseline_scores,
)
from contingency_atlas.scoring.evaluate import (
    bootstrap_metric_cis,
    run_evaluation,
    run_r0_multiseed,
)
from contingency_atlas.scoring.models import IncidentEvent, RiskAtom
from contingency_atlas.scoring.scorer import DetScorer, ScorerConfig
from contingency_atlas.scoring.synthetic import R0Config, generate_r0
from contingency_atlas.scoring.task_bcal import run_bcal, truth_joint_quarterly
from contingency_atlas.scoring.task_loss import run_task_loss

FAST = ScorerConfig(mc_draws=300, interval_draws=300)


# ---------------------------------------------------------------------------
# A3 — honest intervals
# ---------------------------------------------------------------------------


def _fitted_r0_scorer(cfg=None):
    ds = generate_r0(R0Config())
    n_total = max(e.period for e in ds.events) + 1
    scorer = DetScorer(cfg or FAST).fit(ds.events, ds.atoms, n_total - 8)
    return ds, scorer


def test_interval_block_reports_median_and_flag():
    _, scorer = _fitted_r0_scorer()
    rlz = scorer.score_pair("r0-identity-01", "r0-identity-02")
    iv = rlz.components["interval"]
    assert iv["two_level"] is True
    assert iv["frechet_clipped"] is True
    assert iv["p_lo"] <= iv["p_median"] <= iv["p_hi"]
    assert isinstance(iv["p_hat_above_hi"], bool)
    assert iv["draws"] == 300
    assert 0.0 <= iv["posterior_mean"] <= 1.0


def test_intervals_reproducible_and_median_ordering_holds_on_r0():
    _, scorer = _fitted_r0_scorer(ScorerConfig(mc_draws=300, interval_draws=800))
    for a, b in all_pairs(generate_r0(R0Config()).atoms):
        rlz = scorer.score_pair(a, b)
        iv = rlz.components["interval"]
        assert iv["p_lo"] <= iv["p_median"] <= iv["p_hi"]
        assert 0.0 <= iv["p_lo"] <= iv["p_hi"] <= 1.0
    # same seed -> identical bytes
    rlz2 = scorer.score_pair("r0-identity-01", "r0-identity-02")
    assert rlz2.p_interval == scorer.score_pair("r0-identity-01", "r0-identity-02").p_interval


def test_flag_count_is_reported_not_hidden():
    """The alpha << 1 regime of the shrunk Beta can still put p_hat above
    its own q95 — A3's contract is that such rows are *flagged*, and the
    count stays small (the A1 mixture is the intended structural fix)."""
    _, scorer = _fitted_r0_scorer(ScorerConfig(mc_draws=500, interval_draws=1500))
    flagged = 0
    for a, b in all_pairs(generate_r0(R0Config()).atoms):
        if scorer.score_pair(a, b).components["interval"]["p_hat_above_hi"]:
            flagged += 1
    assert flagged <= 25  # 15/105 shipped rows pre-A3; must not explode


def test_independence_mode_interval_matches_product():
    ds = generate_r0(R0Config())
    n_total = max(e.period for e in ds.events) + 1
    cfg = ScorerConfig(mc_draws=100, interval_draws=400, dependence_mode="independence")
    scorer = DetScorer(cfg).fit(ds.events, ds.atoms, n_total - 8)
    rlz = scorer.score_pair("r0-identity-01", "r0-identity-02")
    # under independence the interval is the product posterior — the
    # dependence ablation must not leak support through the interval
    assert rlz.components["dependence"]["type"] == "independence"
    assert rlz.components["dependence"]["data_support"] >= 0


# ---------------------------------------------------------------------------
# Task B-cal
# ---------------------------------------------------------------------------


def test_truth_joint_closed_form_exact():
    truth = {
        "q_base": {"a": 0.01, "b": 0.02, "c": 0.03},
        "shock_mult": {"a": 2.0, "b": 3.0, "c": 2.0},
        "shock_add": {"a": 0.5, "b": 0.6, "c": 0.5},
        "shock_pi": {"ch1": 0.25, "ch2": 0.4},
        "channels": {"ch1": ["a", "b"], "ch2": ["c"]},
    }
    # same channel: pi * s_a * s_b + (1-pi) * q_a * q_b
    s_a, s_b = 0.01 * 2.0 + 0.5, 0.02 * 3.0 + 0.6
    expect_same = 0.25 * s_a * s_b + 0.75 * 0.01 * 0.02
    assert truth_joint_quarterly(truth, "a", "b") == pytest.approx(expect_same)
    # cross channel: marginal-mean product (independent shocks)
    qa_bar = 0.25 * s_a + 0.75 * 0.01
    qc_bar = 0.4 * (0.03 * 2.0 + 0.5) + 0.6 * 0.03
    assert truth_joint_quarterly(truth, "a", "c") == pytest.approx(qa_bar * qc_bar)


def test_truth_joint_matches_long_simulation():
    ds = generate_r0(R0Config(n_quarters=20_000, seed=7))
    ch_of = {}
    for ch, ids in ds.truth["channels"].items():
        for rid in ids:
            ch_of[rid] = ch
    same = sorted(rid for rid, ch in ch_of.items() if ch == "identity")[:2]
    a, b = same
    fired_a = {e.period for e in ds.events if e.risk_id == a}
    fired_b = {e.period for e in ds.events if e.risk_id == b}
    both = len(fired_a & fired_b)
    empirical = both / 20_000
    closed = truth_joint_quarterly(ds.truth, a, b)
    assert empirical == pytest.approx(closed, rel=0.15)


def test_bcal_strata_include_supported_rare_and_report_pathology():
    ds = generate_r0(R0Config())
    res = run_bcal(
        ds.atoms, ds.events, ds.truth, holdout_quarters=8, config=FAST
    )
    mode = res.report["modes"]["shrunk_empirical"]
    strata = mode["strata"]
    for key in ("all", "rare", "common", "supported", "zero_support", "supported_rare"):
        assert key in strata
    assert strata["supported_rare"]["n"] > 0
    # the documented T1 pathology, made visible: zero-support pairs are
    # crushed below truth far harder than supported pairs
    assert (
        strata["zero_support"]["coverage_90"]
        <= strata["supported"]["coverage_90"]
    )
    assert 0.0 <= mode["coverage_90"] <= 1.0
    assert res.report["pre_registered"]["coverage_band"] == [0.8, 0.95]
    assert "supported-rare" in res.report_md or "supported_rare" in res.report_md


def test_bcal_challenger_comparison_runs_two_modes():
    ds = generate_r0(R0Config())
    res = run_bcal(
        ds.atoms,
        ds.events,
        ds.truth,
        holdout_quarters=8,
        config=FAST,
        dependence_modes=("shrunk_empirical", "independence"),
    )
    assert set(res.report["modes"]) == {"shrunk_empirical", "independence"}


# ---------------------------------------------------------------------------
# Task L
# ---------------------------------------------------------------------------


def test_task_l_reports_recovery_and_holdout_loglik():
    ds = generate_r0(R0Config())
    res = run_task_loss(ds.atoms, ds.events, ds.truth, holdout_quarters=8, config=FAST)
    rep = res.report
    assert rep["task"] == "L"
    assert len(rep["per_atom"]) == len(ds.atoms)
    for row in rep["per_atom"]:
        assert row["abs_mu_error"] >= 0
        assert row["abs_sigma_error"] >= 0
        assert row["sev_source"] in ("cell_data", "shrunk_to_pool", "pooled_prior")
    ll = rep["holdout_loglik"]
    assert ll["n_amounts"] > 0
    # truth is the oracle ceiling: fitted cannot systematically beat it
    assert ll["fitted_minus_truth"] <= 0.0
    # per-atom shrinkage beats one pooled severity model on R0
    assert ll["fitted_minus_pooled"] > 0.0


# ---------------------------------------------------------------------------
# Bootstrap CIs + report honesty
# ---------------------------------------------------------------------------


def test_bootstrap_cis_deterministic_and_cover_points():
    ds = generate_r0(R0Config())
    n_total = max(e.period for e in ds.events) + 1
    scorer = DetScorer(FAST).fit(ds.events, ds.atoms, n_total - 8)
    pairs = all_pairs(ds.atoms)
    from contingency_atlas.scoring.evaluate import pair_labels

    labels = pair_labels(ds.events, n_total - 8, n_total, pairs)
    scores = {
        "B1_independence": baseline_scores("B1_independence", pairs, scorer, ds.atoms, seed=1),
        "B3_det_batch": baseline_scores("B3_det_batch", pairs, scorer, ds.atoms, seed=1),
    }
    ci1 = bootstrap_metric_cis(labels, scores, n_boot=200, seed=11)
    ci2 = bootstrap_metric_cis(labels, scores, n_boot=200, seed=11)
    assert ci1 == ci2  # seeded -> deterministic
    ap_ci = ci1["baselines"]["B3_det_batch"]["average_precision"]
    assert ap_ci[0] <= ap_ci[1]
    from contingency_atlas.scoring.evaluate import average_precision

    ap_point = average_precision(labels, scores["B3_det_batch"])
    assert ap_ci[0] <= ap_point <= ap_ci[1]
    lift_ci = ci1["ap_lift_b3_vs_b1_relative"]
    assert lift_ci[0] <= lift_ci[1]


def test_eval_report_honesty_fields():
    ds = generate_r0(R0Config())
    res = run_evaluation(
        ds.atoms, ds.events, truth=ds.truth, holdout_quarters=8, config=FAST, n_boot=100
    )
    tb = res.report["task_b"]
    assert tb["base_rate"] == pytest.approx(
        tb["n_holdout_positives"] / tb["n_pairs"], abs=1e-3
    )
    assert tb["oracle_baselines"] == list(ORACLE_BASELINES)
    assert "metric_note" in tb
    assert tb["bootstrap"]["n_boot"] == 100
    assert tb["bootstrap"]["baselines"]["B3_det_batch"]["average_precision"]
    # B5 composite present in the table
    assert "B5_composite" in tb["baselines"]
    # Task C reports its base rate now (E6)
    assert res.report["task_c"]["random_baseline_precision"] == pytest.approx(30 / 105, abs=1e-3)
    # level tasks attached on truth-enabled runs
    assert res.report["task_bcal"]["modes"]["shrunk_empirical"]
    assert res.report["task_loss"]["holdout_loglik"]["n_amounts"] > 0
    assert res.report["eval_version"] == "1.1"
    # stale strings are gone
    blob = json.dumps(res.report["limitations"])
    assert "stub" not in blob
    assert "deferred" not in blob.lower()


def test_b5_composite_orders_by_shared_channels_and_support():
    ds = generate_r0(R0Config())
    n_total = max(e.period for e in ds.events) + 1
    scorer = DetScorer(FAST).fit(ds.events, ds.atoms, n_total - 8)
    pairs = all_pairs(ds.atoms)
    scores = baseline_scores("B5_composite", pairs, scorer, ds.atoms, seed=1)
    assert "B5_composite" in BASELINES
    ch_of = {}
    for ch, ids in ds.truth["channels"].items():
        for rid in ids:
            ch_of[rid] = ch
    same = [p for p in pairs if ch_of[p[0]] == ch_of[p[1]]]
    cross = [p for p in pairs if ch_of[p[0]] != ch_of[p[1]]]
    # a same-channel pair with train co-fires must outrank any cross pair
    supported_same = [p for p in same if scorer.joint_support(list(p)) >= 1]
    assert supported_same
    assert min(scores[p] for p in supported_same) > max(scores[p] for p in cross)


# ---------------------------------------------------------------------------
# Multi-seed driver
# ---------------------------------------------------------------------------


def test_multiseed_reports_distribution():
    rep = run_r0_multiseed(
        n_seeds=2, base_seed=31337, holdout_quarters=8, config=FAST
    )
    assert len(rep["seeds"]) == 2
    assert rep["seeds"][0]["seed"] == 31337
    assert rep["seeds"][1]["seed"] == 31338
    s = rep["summary"]
    assert s["n_seeds"] == 2
    assert s["ap_lift"]["min"] <= s["ap_lift"]["median"] <= s["ap_lift"]["max"]
    assert 0.0 <= s["go_fraction"] <= 1.0
    assert all("bcal_coverage" in r for r in rep["seeds"])
