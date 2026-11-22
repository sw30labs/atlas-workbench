"""Deterministic hybrid scorer v0 (redesign algorithm-spec.md).

Layer stack implemented here (Phase 1 subset):

* Layers 1-2: marginal frequency (quarterly occurrence, smoothed) and
  lognormal severity per atom, with shrinkage to a pooled prior when the
  cell is thin (n < n_min).
* Layer 3: dependence from shrunk empirical joint co-occurrence
  ``(N_S + kappa * prod q_i) / (T + kappa)`` — pseudo-count prior centred
  on independence, clipped to Frechet bounds. Pairs and (Phase 2) k-way
  combinations share the same estimator. This is the data-supported
  default for sparse R0/R1 data (spec section 4.2, first rung).
* Layer 4: Monte Carlo propagation over quarterly buckets, compound
  definition ``all_must_fire`` within a quarter, annual horizon.
* Layer 5 (EVT): GPD overlay on the aggregate MC loss sample when enough
  positive hits exist (``evt.py``; falsification on threshold stability).
* Layers 6-7 (judgment / pooling): empty channels in Phase 1; fields are
  present so the audit blob matches algorithm-spec section 9.

Intervals are two-level posterior draws (marginals, then joint given the
drawn marginals), Fréchet-clipped per draw, with the posterior median
reported alongside the mean and any ``p_hat > p_hi`` row flagged (A3).

No LLM anywhere in this file. No floats enter structural hashes.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

from . import ALGORITHM_VERSION
from .channels import cooccurrence_counts, occurrence_series
from .models import (
    IncidentEvent,
    RiskAtom,
    ScoreRealization,
    hash_object,
    scenario_state_hash,
    sha256_hex,
)

QUANTILE_LEVELS = ("0.5", "0.9", "0.95", "0.99")


@dataclass
class ScorerConfig:
    horizon_quarters: int = 4  # 1y horizon in quarterly buckets
    mc_draws: int = 20_000
    shrink_kappa: float = 4.0  # pseudo-count (in quarters) toward independence
    n_min: int = 30  # min events for a parametric severity claim
    min_severity_events: int = 2  # measured positive losses per constituent
    pair_support_min: int = 3  # min co-occurring quarters for "statistical"
    marginal_smoothing: float = 0.5  # add-half smoothing on occurrence
    interval_draws: int = 4_000  # posterior draws for p_interval
    # floor for Beta pseudo-counts: cells firing every period (or pairs
    # co-firing every period) would otherwise degenerate the Beta. Surfaced
    # to config (and hashed) per A3 — was a buried constant.
    beta_floor: float = 1e-6
    # empirical-Bayes shrinkage of atom occurrence rates toward the pooled
    # mean (strength in pseudo-quarters; 0 disables and falls back to
    # add-half Laplace smoothing). Recommended by the Task A calibration
    # report on R0: pooled rates beat noisy per-atom MLEs when the true
    # rates are homogeneous.
    eb_strength: float = 4.0
    # Layer 5 (EVT): GPD overlay on the aggregate MC loss sample when
    # enough positive hits exist (spec section 6)
    evt_enabled: bool = True
    evt_min_hits: int = 200
    evt_threshold_quantile: float = 0.9
    seed: int = 20260801
    algorithm_version: str = ALGORITHM_VERSION
    # ablation A1 (evaluation-plan.md section 5): "independence" removes the
    # dependence layer — every joint collapses to the product of marginals
    dependence_mode: str = "shrunk_empirical"  # | "independence" | "mixture"
    # A1 challenger (HYBRID_SCORING_PROPOSAL.md): two-component spike-and-slab
    # mixture. Ships as challenger only — the primary estimator stays
    # "shrunk_empirical" until the pre-registered B-cal gate favors it.
    # Pre-registered sensitivity: pi0 in {0.8, 0.9, 0.95}, c in {0.5, 1, 2}.
    mixture_pi0: float = 0.9
    mixture_c: float = 1.0
    # Judgment-channel socket (HYBRID_SCORING_PROPOSAL.md influence ladder):
    # every rung is ablatable to the rung below via this switch, and "off"
    # must reproduce the deterministic realization byte-for-byte. Rungs
    # above "off" are not implemented yet; the field is hashed now so the
    # socket exists in the config identity.
    judgment_mode: str = "off"  # | "rank_only" | "priors" | "pooled"

    def config_hash(self) -> str:
        return hash_object(
            {
                "horizon_quarters": self.horizon_quarters,
                "mc_draws": self.mc_draws,
                "shrink_kappa": self.shrink_kappa,
                "n_min": self.n_min,
                "min_severity_events": self.min_severity_events,
                "pair_support_min": self.pair_support_min,
                "marginal_smoothing": self.marginal_smoothing,
                "interval_draws": self.interval_draws,
                "beta_floor": self.beta_floor,
                "eb_strength": self.eb_strength,
                "evt_enabled": self.evt_enabled,
                "evt_min_hits": self.evt_min_hits,
                "evt_threshold_quantile": self.evt_threshold_quantile,
                "dependence_mode": self.dependence_mode,
                "mixture_pi0": self.mixture_pi0,
                "mixture_c": self.mixture_c,
                "judgment_mode": self.judgment_mode,
                "algorithm_version": self.algorithm_version,
            }
        )


@dataclass
class MarginalFit:
    risk_id: str
    n_periods_fired: int
    q_quarterly: float  # smoothed P(atom fires in a quarter)
    p_annual: float  # 1 - (1 - q) ^ horizon
    n_events: int
    n_severity: int
    sev_family: str
    sev_mu: float
    sev_sigma: float
    sev_source: str  # "cell_data" | "shrunk_to_pool" | "pooled_prior"

    def atom_component(self) -> dict:
        return {
            "risk_id": self.risk_id,
            "lambda": round(self.q_quarterly * 4, 6),  # annual occurrence rate
            "q_quarterly": round(self.q_quarterly, 6),
            "p_annual": round(self.p_annual, 6),
            "n_periods_fired": self.n_periods_fired,
            "sev_family": self.sev_family,
            "sev_params": {"mu": round(self.sev_mu, 6), "sigma": round(self.sev_sigma, 6)},
            "n": self.n_events,
            "n_severity": self.n_severity,
            "source": self.sev_source,
        }


def _fit_lognormal(values: list[float]) -> tuple[float, float]:
    logs = [math.log(v) for v in values if v > 0]
    if not logs:
        return 0.0, 1.0
    mu = sum(logs) / len(logs)
    if len(logs) < 2:
        return mu, 1.0
    var = sum((x - mu) ** 2 for x in logs) / (len(logs) - 1)
    return mu, max(math.sqrt(var), 1e-6)


def _annual(q_quarterly: float, horizon_quarters: int) -> float:
    return 1.0 - (1.0 - q_quarterly) ** horizon_quarters


def _log_beta(a: float, b: float) -> float:
    return math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)


def _beta_binomial_log_marginal(n: int, t: int, a: float, b: float) -> float:
    """log P(N = n) under the Beta(a, b)-Binomial(t) predictive."""
    return (
        math.lgamma(t + 1)
        - math.lgamma(n + 1)
        - math.lgamma(t - n + 1)
        + _log_beta(n + a, t - n + b)
        - _log_beta(a, b)
    )


def mixture_posterior(
    n_s: int,
    q_ind: float,
    t: int,
    pi0: float,
    c: float,
    floor: float = 1e-12,
) -> tuple[float, float]:
    """Two-component (spike-and-slab) dependence posterior — the A1
    challenger (HYBRID_SCORING_PROPOSAL.md, review T1).

    * independence spike: ``Beta(S * q_ind, S * (1 - q_ind))`` with
      ``S = c / q_ind`` — effective sample size c quarters, so zero support
      barely moves the center (zeros are ~uninformative for rare pairs);
    * diffuse coupling slab: ``Beta(1, 1)``;
    * combined by Beta-Binomial marginal likelihoods with prior weight
      ``pi0`` on independence — observed support shifts weight to the slab
      decisively (N_S = 2 for a rare pair is ~170x likelier under coupling).

    No single Beta can do both (conjugate-family shape limit); the mixture
    is closed form. Returns ``(posterior_mean_q, w_independence)``.
    """
    q_ind = min(max(q_ind, floor), 1.0 - floor)
    s = c / q_ind
    a_spike, b_spike = s * q_ind, s * (1.0 - q_ind)  # == (c, s - c)
    lm_spike = math.log(pi0) + _beta_binomial_log_marginal(n_s, t, a_spike, b_spike)
    lm_slab = math.log(1.0 - pi0) + _beta_binomial_log_marginal(n_s, t, 1.0, 1.0)
    hi = max(lm_spike, lm_slab)
    e_spike = math.exp(lm_spike - hi)
    w = e_spike / (e_spike + math.exp(lm_slab - hi))
    mean = w * (a_spike + n_s) / (a_spike + b_spike + t) + (1.0 - w) * (
        1.0 + n_s
    ) / (2.0 + t)
    return mean, w


@dataclass
class DetScorer:
    """Fits marginals on training events and scores compound pairs."""

    config: ScorerConfig = field(default_factory=ScorerConfig)
    marginals: dict[str, MarginalFit] = field(default_factory=dict)
    cooccur: dict[tuple[str, str], int] = field(default_factory=dict)
    series: dict[str, set[int]] = field(default_factory=dict)
    n_periods: int = 0
    graph_hash: str = ""
    feature_registry_hash: str = ""
    # A7 realization identity: hash of the fitted signal series + train
    # bounds; the pooled occurrence rate is kept for the two-level interval
    # draws (A3).
    data_hash: str = ""
    pooled_rate: float = 0.0

    # -- fitting ----------------------------------------------------------

    def fit(
        self,
        events: list[IncidentEvent],
        atoms: list[RiskAtom],
        n_periods: int,
        *,
        graph_hash: str = "",
        feature_registry_hash: str = "",
    ) -> "DetScorer":
        """Fit on a training window only. ``n_periods`` counts train periods;
        events with period >= n_periods are ignored (holdout discipline)."""
        self.n_periods = n_periods
        self.graph_hash = graph_hash
        self.feature_registry_hash = feature_registry_hash
        ids = sorted(a.risk_id for a in atoms)
        atom_ids = set(ids)
        train = sorted(
            (e for e in events if 0 <= e.period < n_periods and e.risk_id in atom_ids),
            key=lambda e: (
                e.risk_id, e.period, e.event_id,
                e.loss_amount is not None,
                e.loss_amount if e.loss_amount is not None else 0.0,
            ),
        )
        series = occurrence_series(train, n_periods)
        self.series = series
        self.marginals.clear()
        self.cooccur = cooccurrence_counts(series, ids)
        self.data_hash = hash_object(
            {
                "series": {rid: sorted(series.get(rid, set())) for rid in ids},
                "events": [e.to_dict() for e in train],
                "n_periods": n_periods,
                "train_last": n_periods - 1,
            }
        )

        amounts_by_atom: dict[str, list[float]] = {}
        observations_by_atom: dict[str, int] = {}
        for e in train:
            observations_by_atom[e.risk_id] = observations_by_atom.get(e.risk_id, 0) + 1
            if e.loss_amount is not None and e.loss_amount > 0:
                amounts_by_atom.setdefault(e.risk_id, []).append(e.loss_amount)
        pool_amounts = [v for amounts in amounts_by_atom.values() for v in amounts]
        mu_pool, sigma_pool = _fit_lognormal(pool_amounts)

        cfg = self.config
        total_fired = sum(len(series.get(rid, set())) for rid in ids)
        pooled_rate = total_fired / max(len(ids) * n_periods, 1)
        self.pooled_rate = pooled_rate
        for rid in ids:
            n_fired = len(series.get(rid, set()))
            if cfg.eb_strength > 0:
                q = (n_fired + cfg.eb_strength * pooled_rate) / (
                    n_periods + cfg.eb_strength
                )
            else:
                q = (n_fired + cfg.marginal_smoothing) / (
                    n_periods + 2 * cfg.marginal_smoothing
                )
            amounts = amounts_by_atom.get(rid, [])
            n_severity = len(amounts)
            if n_severity >= cfg.n_min:
                mu, sigma = _fit_lognormal(amounts)
                source = "cell_data"
            elif n_severity >= 2:
                mu_i, sigma_i = _fit_lognormal(amounts)
                w = n_severity / (n_severity + cfg.n_min)
                mu = w * mu_i + (1 - w) * mu_pool
                sigma = w * sigma_i + (1 - w) * sigma_pool
                source = "shrunk_to_pool"
            else:
                mu, sigma = mu_pool, sigma_pool
                source = "pooled_prior" if pool_amounts else "unavailable"
            self.marginals[rid] = MarginalFit(
                risk_id=rid,
                n_periods_fired=n_fired,
                q_quarterly=q,
                p_annual=_annual(q, cfg.horizon_quarters),
                n_events=observations_by_atom.get(rid, 0),
                n_severity=n_severity,
                sev_family="lognormal",
                sev_mu=mu,
                sev_sigma=sigma,
                sev_source=source,
            )
        return self

    # -- joints -------------------------------------------------------------

    def joint_support(self, risk_ids: list[str]) -> int:
        """Number of train quarters in which ALL given atoms fired."""
        sets = [self.series.get(r, set()) for r in risk_ids]
        if not sets or any(not s for s in sets):
            return 0
        return len(set.intersection(*sets))

    def joint_quarterly_combo(self, risk_ids: list[str]) -> tuple[float, int]:
        """Shrunk empirical joint occurrence probability (quarterly) for a
        combination of k >= 2 atoms: pseudo-count prior centred on
        independence, clipped to the Frechet upper bound min(q_i).

        Ablation A1 (``dependence_mode="independence"``) removes the
        dependence layer: the joint is the product of marginals with zero
        empirical support recorded. The A1 challenger
        (``dependence_mode="mixture"``) replaces the single-Beta shrinkage
        with the two-component spike-and-slab mixture: zeros stay ~at
        independence, observed support responds decisively.
        """
        ms = [self.marginals[r] for r in risk_ids]
        n_s = self.joint_support(risk_ids)
        q_ind = 1.0
        for m in ms:
            q_ind *= m.q_quarterly
        if self.config.dependence_mode == "independence":
            return q_ind, n_s
        hi = min(m.q_quarterly for m in ms)
        if self.config.dependence_mode == "mixture":
            q, _w = mixture_posterior(
                n_s,
                q_ind,
                self.n_periods,
                self.config.mixture_pi0,
                self.config.mixture_c,
            )
            return min(max(q, 0.0), hi), n_s
        kappa = self.config.shrink_kappa
        q = (n_s + kappa * q_ind) / (self.n_periods + kappa)
        return min(max(q, 0.0), hi), n_s

    def joint_quarterly(self, a: str, b: str) -> tuple[float, int]:
        """Shrunk empirical joint occurrence probability (quarterly)."""
        return self.joint_quarterly_combo([a, b])

    def independence_joint_annual(self, a: str, b: str) -> float:
        """B1 baseline: annual co-occurrence under independence."""
        ma, mb = self.marginals[a], self.marginals[b]
        return _annual(ma.q_quarterly * mb.q_quarterly, self.config.horizon_quarters)

    def p_hat_analytic(self, risk_ids: list[str]) -> float:
        """Annual compound probability without MC — the cheap beam-ranking
        score used by the CBGE expander (loop-design.md step 5)."""
        q_s, _ = self.joint_quarterly_combo(risk_ids)
        return _annual(q_s, self.config.horizon_quarters)

    def _joint_posterior_draw(
        self, rng: random.Random, n_eff: int, q_ind: float
    ) -> float:
        """One draw from the joint-posterior given drawn marginals (A3).

        Dispatches on ``dependence_mode`` so the interval machinery always
        follows the point estimator: independence returns the drawn product
        (the ablation must not leak through the interval); shrunk_empirical
        uses the same pseudo-count Beta as the point estimate. The A1
        mixture challenger plugs in its model-averaged draw here.
        """
        cfg = self.config
        if cfg.dependence_mode == "independence":
            return q_ind
        if cfg.dependence_mode == "mixture":
            # model-averaged draw: pick the component by its posterior
            # weight given the drawn marginals, then draw from that
            # component's posterior — removes the alpha << 1 regime behind
            # the point-outside-interval rows (A1/A3)
            _mean, w = mixture_posterior(
                n_eff, q_ind, self.n_periods, cfg.mixture_pi0, cfg.mixture_c
            )
            if rng.random() < w:
                s = cfg.mixture_c / max(q_ind, 1e-12)
                return rng.betavariate(
                    max(cfg.mixture_c + n_eff, cfg.beta_floor),
                    max(s - cfg.mixture_c + self.n_periods - n_eff, cfg.beta_floor),
                )
            return rng.betavariate(
                1.0 + n_eff, 1.0 + self.n_periods - n_eff
            )
        post_a = max(n_eff + cfg.shrink_kappa * q_ind, cfg.beta_floor)
        post_b = max(
            (self.n_periods - n_eff) + cfg.shrink_kappa * (1.0 - q_ind),
            cfg.beta_floor,
        )
        return rng.betavariate(post_a, post_b)

    def _dependence_component(
        self,
        risk_ids: list[str],
        q_s: float,
        q_ind: float,
        n_s: int,
    ) -> dict:
        """Audit blob for the dependence layer. In mixture mode this carries
        the posterior coupling weight — the audited "dependence-evidence"
        scalar per pair (A1): how much the data itself shifted weight from
        the independence spike to the coupling slab."""
        cfg = self.config
        if cfg.dependence_mode == "mixture":
            _mean, w = mixture_posterior(
                n_s, q_ind, self.n_periods, cfg.mixture_pi0, cfg.mixture_c
            )
            return {
                "type": "mixture_spike_slab",
                "order": len(risk_ids),
                "params": {
                    "pi0": cfg.mixture_pi0,
                    "c": cfg.mixture_c,
                    "S": round(cfg.mixture_c / max(q_ind, 1e-12), 4),
                    "q_quarterly": round(q_s, 6),
                    "q_independence": round(q_ind, 6),
                },
                "posterior_coupling_weight": round(1.0 - w, 6),
                "data_support": n_s,
            }
        return {
            "type": (
                "independence"
                if cfg.dependence_mode == "independence"
                else "shrunk_empirical_joint"
            ),
            "order": len(risk_ids),
            "params": {
                "kappa": cfg.shrink_kappa,
                "q_quarterly": round(q_s, 6),
                "q_independence": round(q_ind, 6),
            },
            "data_support": n_s,
        }

    # -- scoring ------------------------------------------------------------

    def _pair_rng(self, state_hash: str) -> random.Random:
        return random.Random(f"{self.config.seed}:{state_hash}")

    def score_pair(self, a: str, b: str) -> ScoreRealization:
        return self.score_combo([a, b])

    def score_combo(self, risk_ids: list[str]) -> ScoreRealization:
        """Score a compound scenario of k >= 2 atoms (all_must_fire)."""
        if len(risk_ids) < 2:
            raise ValueError("compound scenarios need at least 2 atoms")
        cfg = self.config
        if cfg.judgment_mode != "off":
            raise NotImplementedError(
                f"judgment_mode={cfg.judgment_mode!r} is a config socket only: "
                "rungs above 'off' are gated behind the judgment ledger and "
                "not implemented (HYBRID_SCORING_PROPOSAL.md)"
            )
        risk_ids = sorted(risk_ids)
        ms = [self.marginals[r] for r in risk_ids]
        q_s, n_s = self.joint_quarterly_combo(risk_ids)
        p_annual = _annual(q_s, cfg.horizon_quarters)
        q_ind = 1.0
        for m in ms:
            q_ind *= m.q_quarterly

        state = scenario_state_hash(
            risk_ids,
            horizon=f"{cfg.horizon_quarters}q",
            compound_def="all_must_fire",
            graph_hash=self.graph_hash,
            feature_registry_hash=self.feature_registry_hash,
            algorithm_version=cfg.algorithm_version,
        )
        rng = self._pair_rng(state)

        # Posterior interval on the annual joint — two-level draws (A3):
        # (i) draw each marginal from its own Beta posterior (same EB /
        # Laplace pseudo-count prior as the point estimate), (ii) draw the
        # joint from the dependence posterior given the drawn independence
        # product, (iii) Fréchet-clip every draw against the drawn
        # marginals. This replaces the single-level Beta whose α ≪ 1
        # regime put the point estimate outside its own interval (T2).
        if cfg.eb_strength > 0:
            a0 = cfg.eb_strength * self.pooled_rate
            b0 = cfg.eb_strength * (1.0 - self.pooled_rate)
        else:
            a0 = b0 = cfg.marginal_smoothing
        marg_post = [
            (
                max(m.n_periods_fired + a0, cfg.beta_floor),
                max((self.n_periods - m.n_periods_fired) + b0, cfg.beta_floor),
            )
            for m in ms
        ]
        n_eff = 0 if cfg.dependence_mode == "independence" else n_s
        draws = []
        for _ in range(cfg.interval_draws):
            q_marg = [rng.betavariate(a, b) for a, b in marg_post]
            q_ind_d = 1.0
            for q_i in q_marg:
                q_ind_d *= q_i
            q_d = self._joint_posterior_draw(rng, n_eff, q_ind_d)
            q_d = min(max(q_d, 0.0), min(q_marg))  # Fréchet clip per draw
            draws.append(_annual(q_d, cfg.horizon_quarters))
        draws.sort()
        n_draws = len(draws)
        p_lo = draws[int(0.05 * (n_draws - 1))]
        p_hi = draws[int(0.95 * (n_draws - 1))]
        p_median = draws[int(0.5 * (n_draws - 1))]
        posterior_mean = sum(draws) / n_draws
        p_hat_above_hi = p_annual > p_hi

        # Monte Carlo: per draw simulate horizon_quarters of joint
        # occurrence; compound fires iff all atoms fire in the same quarter
        # (all_must_fire). Severity = sum of atom draws per hit quarter;
        # alpha_overlap = 1.0 (Phase 1, no BU dedup — see spec section 5).
        # Quarters without a joint hit contribute no compound loss.
        p11 = q_s
        hit_losses: list[float] = []
        loss_supported = all(m.n_severity >= cfg.min_severity_events for m in ms)
        n_occurrence_hits = 0
        for _ in range(cfg.mc_draws):
            total = 0.0
            fired = False
            for _q in range(cfg.horizon_quarters):
                u = rng.random()
                if u < p11:
                    fired = True
                    if loss_supported:
                        for m in ms:
                            total += rng.lognormvariate(m.sev_mu, m.sev_sigma)
            n_occurrence_hits += int(fired)
            if total > 0.0:
                hit_losses.append(total)
        hit_losses.sort()
        n_hit = len(hit_losses)
        p_hat_mc = n_occurrence_hits / cfg.mc_draws

        def _q(level: float) -> float | None:
            if not n_hit:
                return None
            idx = min(n_hit - 1, max(0, math.ceil(level * n_hit) - 1))
            return hit_losses[idx]

        loss_quantiles = {lev: _q(float(lev)) for lev in QUANTILE_LEVELS}
        loss_mean = (sum(hit_losses) / n_hit) if n_hit else None

        # Layer 5 (EVT): GPD overlay on the aggregate MC sample. Empirical
        # quantiles above stay authoritative; the overlay reports EVT tail
        # estimates alongside, or a downgraded note when unstable.
        from .evt import evt_overlay

        evt = (
            evt_overlay(
                hit_losses, threshold_quantile=cfg.evt_threshold_quantile
            )
            if cfg.evt_enabled and n_hit >= cfg.evt_min_hits
            else None
        )

        uncertainty_class = (
            "statistical"
            if (
                cfg.dependence_mode != "independence"
                and all(m.n_severity >= cfg.n_min for m in ms)
                and n_s >= cfg.pair_support_min
            )
            else "mixed"
        )
        evidence_support = 0.5 * min(
            1.0, min(m.n_events for m in ms) / 10.0
        ) + 0.5 * min(1.0, n_s / 5.0)

        scenario_id = f"scn-{state[:12]}"
        judgment_version = "det"
        realization_id = sha256_hex(
            f"{state}|{self.data_hash}|{cfg.config_hash()}|{judgment_version}|"
            f"{cfg.seed}|{cfg.algorithm_version}"
        )[:16]
        components = {
            "atoms": [m.atom_component() for m in ms],
            "dependence": self._dependence_component(risk_ids, q_s, q_ind, n_s),
            "modifiers": [],
            "judgment": [],
            "interval": {
                "p_lo": round(p_lo, 6),
                "p_hi": round(p_hi, 6),
                "p_median": round(p_median, 6),
                "posterior_mean": round(posterior_mean, 6),
                "draws": cfg.interval_draws,
                "two_level": True,
                "frechet_clipped": True,
                # honest-interval flag (A3/T2): rows where the point
                # estimate lands above its own q95 must be visible, never
                # silently shipped
                "p_hat_above_hi": bool(p_hat_above_hi),
            },
            "mc": {
                "M": cfg.mc_draws,
                "seed": str(cfg.seed),
                "p_hat_mc": round(p_hat_mc, 6),
                "hit_count": n_occurrence_hits,
                "loss_sample_count": n_hit,
                "alpha_overlap": 1.0,
                "loss_semantics": "dollars" if loss_supported else "activation_units",
                "severity_minimum": cfg.min_severity_events,
                "severity_note": (
                    "Conditional loss given joint activation; no severity dependence or overlap adjustment."
                    if loss_supported else
                    "Monetary outputs unavailable: every constituent needs sufficient measured training losses."
                ),
            },
            "evt": evt,
        }
        return ScoreRealization(
            realization_id=realization_id,
            scenario_id=scenario_id,
            state_hash=state,
            algorithm_version=cfg.algorithm_version,
            model_config_hash=cfg.config_hash(),
            rng_seed=str(cfg.seed),
            p_hat=p_annual,
            p_interval=(p_lo, p_hi),
            loss_quantiles=loss_quantiles,
            loss_mean=loss_mean,
            components=components,
            uncertainty_class=uncertainty_class,
            disagreement=0.0,
            novelty=1.0,  # caller (loop or batch) sets novelty vs archive
            evidence_support=round(evidence_support, 4),
            coherence=None,
            u_score=None,
            data_hash=self.data_hash,
            judgment_version=judgment_version,
        )

    def score_pairs(self, pairs: list[tuple[str, str]]) -> list[ScoreRealization]:
        return [self.score_pair(a, b) for a, b in pairs]
