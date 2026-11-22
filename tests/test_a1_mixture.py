"""A1 mixture challenger tests (HYBRID_SCORING_PROPOSAL.md, wave 3).

The two-component spike-and-slab mixture must: hold the skeptic's null
where zeros prove nothing (zero-support rare ≈ q_ind), keep the markdown
where zeros are informative (common pairs), and respond decisively to
observed coupling (supported-rare) — the three behaviors no single Beta
can produce at once. Ships as challenger: the primary dependence_mode
stays "shrunk_empirical".
"""

import pytest

from workbench_atlas.scoring.ablations import run_sensitivity
from workbench_atlas.scoring.models import IncidentEvent, RiskAtom
from workbench_atlas.scoring.scorer import (
    DetScorer,
    ScorerConfig,
    mixture_posterior,
)
from workbench_atlas.scoring.synthetic import R0Config, generate_r0

FAST = ScorerConfig(mc_draws=300, interval_draws=300)
T = 32


# ---------------------------------------------------------------------------
# The proposal's verified behavior table (T=32, c=1, pi0=0.9)
# ---------------------------------------------------------------------------


def test_mixture_holds_skeptics_null_for_zero_support_rare():
    mean, w = mixture_posterior(0, 0.0025, T, 0.9, 1.0)
    assert mean == pytest.approx(0.0024, abs=2e-4)  # ≈ q_ind, not 0.11x
    assert w > 0.99  # zeros barely move the independence weight


def test_mixture_responds_to_supported_rare():
    # N_S = 2 for a rare pair is ~170x likelier under coupling than under
    # independence: the naive c-fix crushes it to 0.007; the mixture must not
    mean, w = mixture_posterior(2, 0.0025, T, 0.9, 1.0)
    assert mean == pytest.approx(0.0397, abs=2e-3)
    assert w < 0.65  # decisive shift toward the coupling slab
    assert 1.0 - w > 0.35  # audited dependence-evidence scalar


def test_mixture_keeps_markdown_for_zero_support_common():
    mean, _ = mixture_posterior(0, 0.09, T, 0.9, 1.0)
    assert mean == pytest.approx(0.0233, abs=2e-3)


def test_mixture_tracks_empirical_for_supported_common():
    mean, _ = mixture_posterior(3, 0.09, T, 0.9, 1.0)
    assert mean == pytest.approx(0.0935, abs=2e-3)  # empirical 3/32 ≈ 0.094


def test_mixture_monotone_in_support():
    means = [mixture_posterior(n, 0.01, T, 0.9, 1.0)[0] for n in range(5)]
    assert means == sorted(means)


# ---------------------------------------------------------------------------
# Scorer integration
# ---------------------------------------------------------------------------


def _rare_pair_fixture(n_shared: int):
    """Two atoms, 32 quarters, each firing twice; n_shared quarters shared."""
    atoms = [
        RiskAtom(risk_id="ra", title="ra", channels=["x"]),
        RiskAtom(risk_id="rb", title="rb", channels=["x"]),
    ]
    events = []
    for i in range(2):
        events.append(IncidentEvent(f"a{i}", "ra", i, 100.0))
    for j in range(2):
        period = j if j < n_shared else 10 + j
        events.append(IncidentEvent(f"b{j}", "rb", period, 100.0))
    return atoms, events


def test_scorer_mixture_matches_closed_form_and_coupling_weight():
    atoms, events = _rare_pair_fixture(n_shared=2)
    cfg = ScorerConfig(
        mc_draws=50, interval_draws=50, dependence_mode="mixture", eb_strength=0.0
    )
    scorer = DetScorer(cfg).fit(events, atoms, T)
    q_s, n_s = scorer.joint_quarterly_combo(["ra", "rb"])
    assert n_s == 2
    q_ind = scorer.marginals["ra"].q_quarterly * scorer.marginals["rb"].q_quarterly
    expect, w = mixture_posterior(2, q_ind, T, cfg.mixture_pi0, cfg.mixture_c)
    hi = min(m.q_quarterly for m in scorer.marginals.values())
    assert q_s == pytest.approx(min(expect, hi))
    # audited dependence-evidence scalar lands in the dependence block
    rlz = scorer.score_pair("ra", "rb")
    dep = rlz.components["dependence"]
    assert dep["type"] == "mixture_spike_slab"
    assert dep["posterior_coupling_weight"] == pytest.approx(1.0 - w, abs=1e-6)
    assert dep["params"]["pi0"] == 0.9 and dep["params"]["c"] == 1.0


def test_scorer_zero_support_rare_mixture_stays_near_independence():
    atoms, events = _rare_pair_fixture(n_shared=0)
    cfg = ScorerConfig(
        mc_draws=50, interval_draws=50, dependence_mode="mixture", eb_strength=0.0
    )
    scorer = DetScorer(cfg).fit(events, atoms, T)
    q_mix, n_s = scorer.joint_quarterly_combo(["ra", "rb"])
    assert n_s == 0
    q_ind = scorer.marginals["ra"].q_quarterly * scorer.marginals["rb"].q_quarterly
    assert q_mix == pytest.approx(q_ind, rel=0.5)
    # ... where the current primary collapses to ~11% of it
    cfg_shrunk = ScorerConfig(mc_draws=50, interval_draws=50, eb_strength=0.0)
    q_shrunk, _ = DetScorer(cfg_shrunk).fit(events, atoms, T).joint_quarterly_combo(["ra", "rb"])
    assert q_shrunk < 0.5 * q_mix


def test_mixture_kway_combo():
    atoms = [RiskAtom(risk_id=r, title=r, channels=["x"]) for r in ("a", "b", "c")]
    events = [
        IncidentEvent("e1", "a", 0, 1.0),
        IncidentEvent("e2", "b", 0, 1.0),
        IncidentEvent("e3", "c", 0, 1.0),
    ]
    cfg = ScorerConfig(mc_draws=50, interval_draws=50, dependence_mode="mixture")
    scorer = DetScorer(cfg).fit(events, atoms, 16)
    q_s, n_s = scorer.joint_quarterly_combo(["a", "b", "c"])
    assert n_s == 1
    assert 0.0 < q_s <= min(m.q_quarterly for m in scorer.marginals.values())


def test_mixture_config_hash_and_default_primary():
    base = ScorerConfig()
    assert base.dependence_mode == "shrunk_empirical"  # primary unchanged
    assert base.config_hash() != ScorerConfig(mixture_pi0=0.8).config_hash()
    assert base.config_hash() != ScorerConfig(mixture_c=2.0).config_hash()
    assert base.config_hash() != ScorerConfig(dependence_mode="mixture").config_hash()


def test_mixture_intervals_are_valid_and_flag_less():
    ds = generate_r0(R0Config())
    n_total = max(e.period for e in ds.events) + 1
    flagged = {}
    for mode in ("shrunk_empirical", "mixture"):
        cfg = ScorerConfig(mc_draws=300, interval_draws=800, dependence_mode=mode)
        scorer = DetScorer(cfg).fit(ds.events, ds.atoms, n_total - 8)
        flagged[mode] = 0
        for a, b in (
            ("r0-identity-01", "r0-identity-02"),
            ("r0-cloud-01", "r0-cloud-02"),
            ("r0-payments-01", "r0-payments-02"),
        ):
            rlz = scorer.score_pair(a, b)
            iv = rlz.components["interval"]
            assert iv["p_lo"] <= iv["p_median"] <= iv["p_hi"]
            assert 0.0 <= iv["p_lo"] <= iv["p_hi"] <= 1.0
            flagged[mode] += int(iv["p_hat_above_hi"])
    # model-averaged draws remove (never worsen) the alpha << 1 regime
    assert flagged["mixture"] <= flagged["shrunk_empirical"]


def test_sensitivity_rows_and_challenger_table():
    ds = generate_r0(R0Config())
    sens = run_sensitivity(
        ds.atoms, ds.events, ds.truth, holdout_quarters=8, scorer_config=FAST
    )
    kinds = [r["kind"] for r in sens["rows"]]
    assert kinds.count("kappa") == 3
    assert kinds.count("mixture") == 9
    for r in sens["rows"]:
        assert 0.0 <= r["coverage_90"] <= 1.0
        assert r["ap_b3"] is not None
        assert 0.0 <= r["supported_rare_coverage"] <= 1.0
    cmp_modes = sens["challenger_vs_primary"]
    assert set(cmp_modes) == {"shrunk_empirical", "mixture"}
    # the challenger's claim, checked: zero-support pairs are no longer
    # crushed below truth the way the single-Beta primary crushes them
    zs_mix = cmp_modes["mixture"]["strata"]["zero_support"]
    zs_pri = cmp_modes["shrunk_empirical"]["strata"]["zero_support"]
    assert zs_mix["mean_bias"] >= zs_pri["mean_bias"]
