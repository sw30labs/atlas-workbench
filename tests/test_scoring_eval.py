"""Tests for the evaluation harness: metrics, labels, and the R0
end-to-end Task B / Task C run (the Phase 1 go/no-go)."""

import pytest

from workbench_atlas.scoring.evaluate import (
    average_precision,
    auroc,
    pair_labels,
    run_evaluation,
    topk_precision,
    write_eval_outputs,
)
from workbench_atlas.scoring.models import IncidentEvent
from workbench_atlas.scoring.scorer import ScorerConfig
from workbench_atlas.scoring.synthetic import R0Config, generate_r0


def _labels_scores():
    labels = {("a", "b"): 1, ("c", "d"): 0, ("e", "f"): 1, ("g", "h"): 0}
    return labels


def test_metrics_perfect_ranking():
    labels = _labels_scores()
    scores = {("a", "b"): 0.9, ("e", "f"): 0.8, ("c", "d"): 0.2, ("g", "h"): 0.1}
    assert average_precision(labels, scores) == 1.0
    assert auroc(labels, scores) == 1.0
    assert topk_precision(labels, scores, 2) == 1.0


def test_metrics_reversed_ranking():
    labels = _labels_scores()
    scores = {("a", "b"): 0.1, ("e", "f"): 0.2, ("c", "d"): 0.8, ("g", "h"): 0.9}
    assert auroc(labels, scores) == 0.0
    assert average_precision(labels, scores) < 0.5
    assert topk_precision(labels, scores, 2) == 0.0


def test_metrics_degenerate_inputs():
    assert average_precision({("a", "b"): 0}, {("a", "b"): 1.0}) is None
    assert auroc({("a", "b"): 1}, {("a", "b"): 1.0}) is None


def test_pair_labels_holdout_only():
    events = [
        IncidentEvent("e1", "a", 0, 1.0),
        IncidentEvent("e2", "b", 0, 1.0),  # train co-occurrence: no label
        IncidentEvent("e3", "a", 5, 1.0),
        IncidentEvent("e4", "b", 6, 1.0),  # different test quarters: no label
        IncidentEvent("e5", "a", 7, 1.0),
        IncidentEvent("e6", "b", 7, 1.0),  # same test quarter: positive
    ]
    labels = pair_labels(events, test_start=4, n_periods=8, pairs=[("a", "b")])
    assert labels[("a", "b")] == 1
    labels_train_only = pair_labels(events, test_start=0, n_periods=4, pairs=[("a", "b")])
    assert labels_train_only[("a", "b")] == 1  # sanity: window 0..3 has the pair
    labels_gap = pair_labels(events, test_start=5, n_periods=7, pairs=[("a", "b")])
    assert labels_gap[("a", "b")] == 0  # quarters 5,6 fire on different days


@pytest.fixture(scope="module")
def r0_eval():
    ds = generate_r0(R0Config())  # default seed = pre-registered R0 dataset
    result = run_evaluation(
        ds.atoms,
        ds.events,
        truth=ds.truth,
        config=ScorerConfig(mc_draws=500, interval_draws=500),
    )
    return ds, result


def test_task_b_dependence_lifts_ap(r0_eval):
    """Pre-registered Phase 1 gate: B3 must lift AP >= 15% relative over B1
    on the R0 holdout, else the redesign scoring thesis fails here."""
    _, result = r0_eval
    tb = result.report["task_b"]
    assert tb["n_holdout_positives"] > 0
    ap_b1 = tb["baselines"]["B1_independence"]["average_precision"]
    ap_b3 = tb["baselines"]["B3_det_batch"]["average_precision"]
    assert tb["ap_lift_b3_vs_b1_relative"] == pytest.approx(
        (ap_b3 - ap_b1) / ap_b1, abs=1e-2
    )
    assert tb["pass"] is True
    assert tb["ap_lift_b3_vs_b1_relative"] >= 0.15


def test_task_c_structure_recovery(r0_eval):
    """Pre-registered Phase 1 gate: planted edges recovered at precision
    >= 0.7, and planted pairs fill the top decile of the U ranking."""
    _, result = r0_eval
    tc = result.report["task_c"]
    assert tc["n_planted_edges"] == 30
    assert tc["precision_at_planted_count"] >= 0.7
    assert tc["top_decile_planted_fraction"] >= 0.7
    assert tc["pass"] is True


def test_recommendation_go_on_preregistered_r0(r0_eval):
    _, result = r0_eval
    assert result.report["recommendation"] == "go"
    assert result.report["algorithm_version"]
    assert result.report["git_sha"]


def test_label_shuffle_null_control(r0_eval):
    """Adversarial null (evaluation-plan.md section 7.2): shuffled labels
    must not let any baseline achieve AP far above the base rate."""
    import random

    ds, result = r0_eval
    from workbench_atlas.scoring.candidates import all_pairs, baseline_scores
    from workbench_atlas.scoring.channels import build_channel_graph
    from workbench_atlas.scoring.scorer import DetScorer

    pairs = all_pairs(ds.atoms)
    labels = pair_labels(ds.events, 32, 40, pairs)
    base_rate = sum(labels.values()) / len(labels)
    rng = random.Random(0)
    scorer = DetScorer(ScorerConfig(mc_draws=200, interval_draws=200)).fit(
        ds.events, ds.atoms, 32, graph_hash=build_channel_graph(ds.atoms).graph_hash
    )
    scores = baseline_scores("B3_det_batch", pairs, scorer, ds.atoms)
    aps = []
    for _ in range(10):
        shuffled = dict(zip(labels, rng.sample(list(labels.values()), len(labels))))
        ap = average_precision(shuffled, scores)
        aps.append(ap)
    assert max(aps) < base_rate + 0.25


def test_write_eval_outputs(tmp_path, r0_eval):
    _, result = r0_eval
    paths = write_eval_outputs(result, tmp_path)
    import json

    report = json.loads(paths["json"].read_text())
    assert report["recommendation"] == "go"
    assert "task_b" in report and "task_c" in report
    md = paths["md"].read_text()
    assert "Task B" in md and "Recommendation" in md
