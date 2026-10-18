"""Tests for the pre-registered ablation runner (evaluation-plan.md §5),
post loop-removal: A1 and A3 runnable; A2, A4, A6 preserved as historical
rows; A5 gated on the modifier path."""

import json

import pytest

from contingency_atlas.scoring.ablations import run_ablations, write_ablation_outputs
from contingency_atlas.scoring.scorer import DetScorer, ScorerConfig
from contingency_atlas.scoring.synthetic import R0Config, generate_r0

FAST = ScorerConfig(mc_draws=300, interval_draws=300)


@pytest.fixture(scope="module")
def ablation_result():
    ds = generate_r0(R0Config())
    return run_ablations(ds.atoms, ds.events, truth=ds.truth, scorer_config=FAST)


def test_a1_independence_collapses_joints():
    ds = generate_r0(R0Config())
    scorer = DetScorer(ScorerConfig(mc_draws=100, interval_draws=100, dependence_mode="independence"))
    scorer.fit(ds.events, ds.atoms, 32)
    planted = tuple(sorted(ds.truth["planted_edges"][0]))
    q, _ = scorer.joint_quarterly(*planted)
    ma, mb = scorer.marginals[planted[0]], scorer.marginals[planted[1]]
    assert q == pytest.approx(ma.q_quarterly * mb.q_quarterly)
    rlz = scorer.score_pair(*planted)
    assert rlz.components["dependence"]["type"] == "independence"
    assert rlz.uncertainty_class != "statistical"
    # mode is part of the config hash (different scores, different config)
    assert scorer.config.config_hash() != ScorerConfig().config_hash()


def test_a1_dependence_removal_drops_ap(ablation_result):
    a1 = ablation_result.report["ablations"]["A1"]
    assert a1["observed"]["ap_drop"] > 0
    assert a1["consistent"] is True
    assert not a1.get("historical")


def test_a3_evt_overlay_stabilizes_tail(ablation_result):
    a3 = ablation_result.report["ablations"]["A3"]
    obs = a3["observed"]
    assert obs["q99_cv_with_evt"] is not None
    assert obs["q99_cv_with_evt"] <= obs["q99_cv_without_evt"] * 1.05
    assert a3["consistent"] is True
    assert not a3.get("historical")


def test_loop_ablations_preserved_as_historical(ablation_result):
    """A2/A4/A6 measured against the removed loop: kept visible with their
    last results, marked historical, never re-run."""
    for key, expected in (
        ("A2", "n_scored_ablated"),
        ("A6", "decision"),
        ("A4", "coherence_measured_with_judge"),
    ):
        entry = ablation_result.report["ablations"][key]
        assert entry["historical"], key
        assert expected in entry["observed"]
        assert entry["consistent"] is True
    assert ablation_result.report["ablations"]["A6"]["observed"]["decision"] == "keep_loop"


def test_a5_reported_not_implemented(ablation_result):
    entry = ablation_result.report["ablations"]["A5"]
    assert entry["consistent"] is None
    assert entry["note"].startswith("not_implemented")
    # full pre-registered matrix always visible
    assert ablation_result.report["ablation_order"] == ["A1", "A2", "A6", "A4", "A3", "A5"]
    s = ablation_result.report["summary"]
    assert s["n_runnable"] == 2
    assert s["n_consistent"] == 2
    assert s["n_historical"] == 3
    assert s["n_not_implemented"] == 1


def test_write_ablation_outputs(ablation_result, tmp_path):
    paths = write_ablation_outputs(ablation_result, tmp_path)
    report = json.loads(paths["json"].read_text())
    assert report["summary"]["n_consistent"] == 2
    md = paths["md"].read_text()
    assert "A1" in md and "historical" in md
