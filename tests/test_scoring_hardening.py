"""Tests for the EVT layer (4c), EB shrinkage (4a), and the R0' count
regime (4b)."""

import random

import pytest

from contingency_atlas.scoring.calibration import calibrate_atoms
from contingency_atlas.scoring.evt import evt_overlay, fit_gpd_mom, gpd_tail_quantile
from contingency_atlas.scoring.scorer import DetScorer, ScorerConfig
from contingency_atlas.scoring.synthetic import R0Config, generate_r0


# ---------------------------------------------------------------------------
# EVT
# ---------------------------------------------------------------------------


def _lognormal_sample(n: int, mu: float, sigma: float, seed: int = 7) -> list[float]:
    rng = random.Random(seed)
    return sorted(rng.lognormvariate(mu, sigma) for _ in range(n))


def test_gpd_fit_recovers_heavy_tail():
    sample = _lognormal_sample(4000, 12.0, 1.2)
    overlay = evt_overlay(sample, min_excess=20)
    assert overlay is not None
    assert overlay["used"] is True
    assert overlay["xi"] > 0  # lognormal is in the Frechet class
    assert overlay["stable"] is True
    # EVT tail quantiles should bracket the empirical ones sensibly
    emp_q99 = sample[int(0.99 * len(sample)) - 1]
    assert 0.3 < overlay["q99_evt"] / emp_q99 < 3.0


def test_evt_requires_min_excess():
    sample = _lognormal_sample(100, 10.0, 1.0)
    # 100 points -> ~10 excesses over q90 < 20
    assert evt_overlay(sample, min_excess=20) is None
    overlay = evt_overlay(sample, min_excess=5)
    assert overlay is not None


def test_evt_threshold_sensitivity_downgrades_unstable(monkeypatch):
    # craft a sample whose MoM xi flips sign across thresholds
    sample = sorted([1.0] * 90 + [2.0] * 8 + [100.0, 300.0])
    overlay = evt_overlay(sample, min_excess=2)
    assert overlay is not None
    assert overlay["stable"] is False
    assert "downgraded" in overlay["note"]
    assert "q99_evt" not in overlay  # no EVT quantiles when unstable


def test_gpd_tail_quantile_exponential_limit():
    q = gpd_tail_quantile(0.99, u=100.0, n_u=50, n=500, xi=0.0, beta=50.0)
    assert q > 100.0


def test_evt_in_scorer_components():
    ds = generate_r0(R0Config())
    scorer = DetScorer(ScorerConfig(mc_draws=3000, interval_draws=200)).fit(ds.events, ds.atoms, 32)
    planted = [tuple(sorted(e)) for e in ds.truth["planted_edges"]]
    best = max(planted, key=lambda p: scorer.cooccur.get(p, 0))
    rlz = scorer.score_pair(*best)
    evt = rlz.components["evt"]
    if rlz.components["mc"]["hit_count"] >= 200:
        assert evt is not None and evt["used"] is True
        assert "xi" in evt and "xi_sensitivity" in evt
    # disabled -> always None
    scorer_off = DetScorer(
        ScorerConfig(mc_draws=3000, interval_draws=200, evt_enabled=False)
    ).fit(ds.events, ds.atoms, 32)
    assert scorer_off.score_pair(*best).components["evt"] is None


# ---------------------------------------------------------------------------
# EB shrinkage (4a)
# ---------------------------------------------------------------------------


def test_eb_shrinks_rates_toward_pool():
    ds = generate_r0(R0Config())
    raw = DetScorer(ScorerConfig(eb_strength=0.0, mc_draws=100, interval_draws=100)).fit(ds.events, ds.atoms, 32)
    eb = DetScorer(ScorerConfig(eb_strength=8.0, mc_draws=100, interval_draws=100)).fit(ds.events, ds.atoms, 32)
    raw_rates = [m.q_quarterly for m in raw.marginals.values()]
    eb_rates = [m.q_quarterly for m in eb.marginals.values()]
    raw_spread = max(raw_rates) - min(raw_rates)
    eb_spread = max(eb_rates) - min(eb_rates)
    assert eb_spread < raw_spread
    # and EB strength is part of the config hash
    assert raw.config.config_hash() != eb.config.config_hash()


def test_nb_family_in_calibration_report():
    ds = generate_r0(R0Config(count_model="poisson_counts"))
    res = calibrate_atoms(
        ds.events, [a.risk_id for a in ds.atoms],
        train_quarters=32, n_periods=40,
    )
    r = res.report
    assert r["n_nb_atoms"] > 0  # R0' cells are overdispersed
    assert "nll_model" in r and "nll_naive" in r
    assert r["nll_beats_naive"] is True


# ---------------------------------------------------------------------------
# R0' count regime (4b)
# ---------------------------------------------------------------------------


def test_r0_prime_count_regime():
    ds0 = generate_r0(R0Config())
    ds1 = generate_r0(R0Config(count_model="poisson_counts"))
    assert len(ds1.events) > len(ds0.events)
    assert ds1.truth["regime"] == "R0'"
    assert ds1.truth["count_model"] == "poisson_counts"
    assert "count_lambda" in ds1.truth
    # determinism
    ds1b = generate_r0(R0Config(count_model="poisson_counts"))
    assert [e.to_dict() for e in ds1.events] == [e.to_dict() for e in ds1b.events]
    # richer severity samples: more events per firing atom
    from collections import Counter

    per_atom = Counter(e.risk_id for e in ds1.events)
    assert max(per_atom.values()) >= 10
