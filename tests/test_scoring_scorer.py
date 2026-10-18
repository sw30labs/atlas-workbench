"""Tests for the deterministic scorer."""

from contingency_atlas.scoring.channels import build_channel_graph
from contingency_atlas.scoring.scorer import DetScorer, ScorerConfig
from contingency_atlas.scoring.synthetic import R0Config, generate_r0

FAST = ScorerConfig(mc_draws=500, interval_draws=500)


def _fitted(seed=20260801):
    ds = generate_r0(R0Config(seed=seed))
    scorer = DetScorer(FAST).fit(ds.events, ds.atoms, 32, graph_hash="g")
    return ds, scorer


def test_fit_ignores_holdout_periods():
    ds = generate_r0(R0Config())
    s1 = DetScorer(FAST).fit(ds.events, ds.atoms, 32)
    # events in the holdout window must not change the fit
    truncated = [e for e in ds.events if e.period < 32]
    s2 = DetScorer(FAST).fit(truncated, ds.atoms, 32)
    for rid in s1.marginals:
        assert s1.marginals[rid].q_quarterly == s2.marginals[rid].q_quarterly
        assert s1.marginals[rid].n_events == s2.marginals[rid].n_events
    assert s1.cooccur == s2.cooccur


def test_same_channel_pair_beats_independence():
    ds, scorer = _fitted()
    planted = [tuple(sorted(e)) for e in ds.truth["planted_edges"]]
    # the planted pair with the most train support must outscore its
    # independence product — dependence is real on R0
    best = max(planted, key=lambda p: scorer.cooccur.get(p, 0))
    q_ij, n_ij = scorer.joint_quarterly(*best)
    assert n_ij >= 2
    ma, mb = scorer.marginals[best[0]], scorer.marginals[best[1]]
    assert q_ij > ma.q_quarterly * mb.q_quarterly


def test_score_realization_components_complete():
    ds, scorer = _fitted()
    pair = tuple(sorted(ds.truth["planted_edges"][0]))
    rlz = scorer.score_pair(*pair)
    d = rlz.to_dict()
    for key in (
        "realization_id", "scenario_id", "state_hash", "algorithm_version",
        "model_config_hash", "rng_seed", "p_hat", "p_interval",
        "loss_quantiles", "loss_mean", "components", "uncertainty_class",
        "disagreement", "novelty", "evidence_support",
    ):
        assert key in d, key
    comp = d["components"]
    assert len(comp["atoms"]) == 2
    for atom in comp["atoms"]:
        for key in ("risk_id", "lambda", "sev_family", "sev_params", "n", "source"):
            assert key in atom, key
    assert comp["dependence"]["type"] == "shrunk_empirical_joint"
    assert comp["mc"]["M"] == FAST.mc_draws
    assert comp["evt"] is None
    assert comp["judgment"] == []
    # audit blob contains no timestamps or prose (hash-stable fields only)
    assert 0.0 <= d["p_hat"] <= 1.0
    lo, hi = d["p_interval"]
    assert 0.0 <= lo <= hi <= 1.0


def test_mc_reproducible_and_consistent_with_analytic():
    ds, scorer = _fitted()
    pair = tuple(sorted(ds.truth["planted_edges"][0]))
    r1 = scorer.score_pair(*pair)
    scorer2 = DetScorer(FAST).fit(ds.events, ds.atoms, 32, graph_hash="g")
    r2 = scorer2.score_pair(*pair)
    assert r1.components["mc"]["p_hat_mc"] == r2.components["mc"]["p_hat_mc"]
    assert r1.loss_quantiles == r2.loss_quantiles
    # MC hit rate should track the analytic p_hat within noise
    assert abs(r1.components["mc"]["p_hat_mc"] - r1.p_hat) < 0.10


def test_state_hash_ignores_seed_and_scores():
    ds, scorer = _fitted()
    pair = tuple(sorted(ds.truth["planted_edges"][0]))
    r1 = scorer.score_pair(*pair)
    other = DetScorer(ScorerConfig(mc_draws=500, interval_draws=500, seed=99)).fit(
        ds.events, ds.atoms, 32, graph_hash="g"
    )
    r2 = other.score_pair(*pair)
    assert r1.state_hash == r2.state_hash  # same state
    assert r1.realization_id != r2.realization_id  # different realization


def test_loss_quantiles_ordered_when_hits():
    ds, scorer = _fitted()
    # find the highest-p pair to guarantee MC hits
    pairs = [tuple(sorted(e)) for e in ds.truth["planted_edges"]]
    best = max(pairs, key=lambda p: scorer.score_pair(*p).p_hat)
    rlz = scorer.score_pair(*best)
    q = rlz.loss_quantiles
    if rlz.components["mc"]["hit_count"] > 0:
        values = [q[k] for k in ("0.5", "0.9", "0.95", "0.99")]
        assert all(v is not None and v > 0 for v in values)
        assert values == sorted(values)
        assert rlz.loss_mean is not None and rlz.loss_mean >= values[0] * 0.5


def test_independence_baseline_lower_for_coupled_pair():
    ds, scorer = _fitted()
    planted = [tuple(sorted(e)) for e in ds.truth["planted_edges"]]
    best = max(planted, key=lambda p: scorer.cooccur.get(p, 0))
    rlz = scorer.score_pair(*best)
    assert rlz.p_hat > scorer.independence_joint_annual(*best)


def test_graph_hash_changes_state_hash():
    ds = generate_r0(R0Config())
    graph = build_channel_graph(ds.atoms)
    s1 = DetScorer(FAST).fit(ds.events, ds.atoms, 32, graph_hash=graph.graph_hash)
    s2 = DetScorer(FAST).fit(ds.events, ds.atoms, 32, graph_hash="other")
    pair = tuple(sorted(ds.truth["planted_edges"][0]))
    assert s1.score_pair(*pair).state_hash != s2.score_pair(*pair).state_hash
