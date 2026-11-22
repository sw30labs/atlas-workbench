# Design — the deterministic scoring engine

Package: `contingency_atlas/scoring/`. Algorithm version: `0.2.0`
(`scoring/__init__.py::ALGORITHM_VERSION`; breaking change = score
components change meaning). 0.1.0 → 0.2.0: two-level posterior
intervals, realization identity (`data_hash`, `judgment_version`),
horizon hashed in quarters, mixture-challenger socket.

## 1. What is being scored

A **compound scenario** `S` is a set of `k ≥ 2` emerging-risk atoms
`R_S = {r_1, …, r_k}` (in production, `k = 2`) with a one-year horizon.
Output is a **score vector with uncertainty**, not a point estimate:

- `p̂(S)` — probability that **all atoms fire in at least one shared
  quarter** within the horizon (compound definition `all_must_fire`),
- `L | S` — loss distribution conditional on materialization:
  quantiles q0.5, q0.9, q0.95, q0.99 and mean,
- `uncertainty_class` — `statistical | mixed | judgment_dominant`,
- full **components** blob (atoms, dependence, MC parameters, EVT) for
  audit reproduction.

## 2. Layer stack

```text
[1] marginal frequency per atom      q_i  (EB-shrunk Bernoulli rate)
[2] marginal severity per atom       lognormal(mu_i, sigma_i), pooled shrinkage
[3] dependence per pair              shrunk empirical joint q_ij
[4] joint occurrence + loss          Monte Carlo over quarterly buckets
[5] tail overlay                     GPD (EVT) with threshold falsification
[6] judgment channel                 (none in production; see ADR-001)
[7] diagnostics                      calibration, ablations, state hash
```

## 3. Layer 1 — marginal frequency (with empirical-Bayes shrinkage)

For atom `i` with `N_i` firing quarters in a train window of `T`
quarters, and pooled rate `μ = ΣN_i / (n_atoms · T)`:

```
q_i = (N_i + α·μ) / (T + α),     α = eb_strength (default 4)
```

`eb_strength = 0` falls back to add-half Laplace smoothing. Rationale:
pooled rates beat noisy per-atom MLEs at short windows when true rates
are homogeneous (found by the Task A harness; see evaluation-analysis).
Annualization: `p_i = 1 − (1 − q_i)^4`.

## 4. Layer 2 — marginal severity

Lognormal fit to positive loss amounts in the atom's cell:

- `n ≥ n_min` (30): per-cell MLE on log amounts (`cell_data`);
- `2 ≤ n < n_min`: shrink `mu`/`sigma` toward the pooled fit with weight
  `w = n/(n + n_min)` (`shrunk_to_pool`);
- `n < 2`: pooled fit verbatim (`pooled_prior`).

Every fit stores family, parameters, n, and source.

## 5. Layer 3 — dependence (the product)

For pair `(i, j)` with `N_ij` shared firing quarters:

```
q_ij = (N_ij + κ·q_i·q_j) / (T + κ),   κ = shrink_kappa (default 4)
```

a pseudo-count prior centered on independence, clipped to the Fréchet
upper bound `min(q_i, q_j)`. Ablation A1 (`dependence_mode =
"independence"`) replaces this with `q_i·q_j` and removes empirical
support from the posterior — the pre-registered way to prove the layer
pays rent (it does: AP 0.734 → 0.398 on R0).

Posterior interval (two-level draws, A3): each of `interval_draws`
seeded draws first samples the marginals from their Beta posteriors
(EB prior `eb_strength`·pooled rate; add-half Laplace
`marginal_smoothing` when `eb_strength = 0`), then draws the joint from
the dependence posterior `Beta(N_ij + κ·q*, (T − N_ij) + κ(1 − q*))`
evaluated at the *drawn* independence product `q* = q_i·q_j` (floored
at `beta_floor` = 1e-6 — boundary fix for cells firing every period),
clips the draw to the Fréchet bound of the drawn marginals, and
annualizes; 5%/95% quantiles. The posterior median is reported beside
the mean in `components.interval` (`p_lo`, `p_hi`, `p_median`,
`posterior_mean`, `draws`, `two_level`, `frechet_clipped`,
`p_hat_above_hi`); the `p_hat_above_hi` flag — point estimate above its
own interval — is counted in evaluation reports (16/105 rows flagged on
R0 with the primary — visible, not hidden).

### A1 mixture challenger (not promoted)

`dependence_mode: "mixture"` (the primary stays `"shrunk_empirical"`)
puts a spike-and-slab posterior on the joint: spike
`Beta(S·q_ind, S·(1 − q_ind))` with `S = c/q_ind`, slab `Beta(1, 1)`,
prior weight `pi0 = 0.9`, `c = 1.0` (both in `config_hash`;
pre-registered sensitivity grid `π₀ ∈ {0.8, 0.9, 0.95} × c ∈ {0.5, 1,
2}` in the ablations sensitivity section). Per draw the two components
are combined by their Beta-Binomial marginal likelihoods (model
averaging), and the posterior coupling weight is audited per pair in
the dependence block. Task B-cal on R0 (`evaluation-analysis.md` §2a):
zero-support coverage 0.00 → 0.47, interval flags 16 → 0, but
supported-rare regresses 0.62 → 0.46 — both modes fail the
pre-registered band, so the challenger is **not promoted**.

## 6. Layer 4 — Monte Carlo

Per pair, `M = 20,000` draws (configurable), each simulating 4 quarters:

```text
per quarter: u ~ U(0,1); hit if u < q_ij
on hit: L += Lognormal(mu_i, sigma_i) + Lognormal(mu_j, sigma_j)   (alpha_overlap = 1.0)
scenario hits if any quarter hits; record Σ L
```

Outputs: empirical `L | S` quantiles, loss mean, `p_hat_mc` (hit rate —
cross-checked against the analytic `p̂` within 0.10), hit count.
`alpha_overlap = 1.0` in Phase 1 (no segment overlap adjustment; documented
in components).

RNG is `random.Random(f"{seed}:{state_hash}")` — every realization is
reproducible from `(seed, state)` alone. The interval draws consume the
same stream before the MC draws, in fixed order.

## 7. Layer 5 — EVT tail overlay

When `hit_count ≥ 200`, fit a GPD by method of moments on excesses over
the `q0.9` threshold of the aggregate MC sample (spec: use GPD only if
`n_u ≥ 20` excesses). **Threshold falsification:** refit at q0.85/q0.95;
if ξ flips sign or moves > 50%, the overlay is downgraded (`stable:
false`, qualitative-only note) — no EVT quantiles are reported. When
stable, EVT q95/q99 are reported *alongside* the empirical quantiles
(which stay authoritative).

## 8. State hash and realizations

```
state_hash = sha256(canonical_json({
  risk_ids: sorted, horizon: f"{horizon_quarters}q", compound_def, k,
  transmission_id, scope, assumption_hash, graph_hash,
  feature_registry_hash, algorithm_major
}))
```

Horizon is hashed in quarters (was floor-years — different quarter
counts could collide). **Scores never enter the hash** — they are
derived artifacts of `(state, data, judgment_version,
algorithm_version, seed)`. Two runs of the same state with different
seeds are the same state, different realizations:

```
realization_id = sha256(state_hash | data_hash | judgment_version |
                        rng_seed | algorithm_version)[:16]
```

- `pair_id_for(risk_ids)` = `"pair-" +
  sha256(canonical_json(sorted ids))[:12]` — stable compound identity
  that survives graph/slug churn; backfilled onto pre-existing rows by
  an additive migration.
- `data_hash` (new `ScoreRealization` field) — hash of the fitted
  series + train bounds, so the same state refit on different data is a
  different realization.
- `judgment_version` — `"det"` today. `ScorerConfig.judgment_mode` is
  an `"off"` socket; any other value raises `NotImplementedError` (the
  judgment channel stays unbuilt, ADR-001).

`ScorerConfig` also gained `beta_floor` (1e-6); `config_hash` now
covers `interval_draws`, `beta_floor`, `judgment_mode`, `mixture_pi0`,
and `mixture_c`. Canonical JSON: sorted keys, compact separators, no
floats in hashed assumptions.

## 9. Uncertainty classes

- `statistical`: all atoms `n ≥ n_min` **and** `N_ij ≥ 3` (dependence
  from data);
- `mixed`: otherwise;
- `judgment_dominant`: any critical atom elicited (unused in production
  today — nothing is elicited).

## 10. Candidate generation (production)

Channel-blocked pairs only: atoms share ≥1 channel from the
deterministic keyword tagger (`identity, cloud, payments, model,
credit-chain, physical, energy, legal, third-party, geopolitical,
data`). The channel graph edge set is hashed into every state hash. The
weekly full-pair batch (`candidates.all_pairs`) remains the safety-net
comparator used by the evaluation harness.

## 11. Baselines used in evaluation

| ID | Meaning |
|----|---------|
| B0 | random (seeded) |
| B1 | independence product `1 − (1 − q_iq_j)^4` |
| B2 | shared-channel indicator (+ ε tie-break) |
| B3 | full batch det scorer over all pairs |
| B5 | composite: #shared channels × N_S (independence tie-break) |

B2 and B5 read channel tags — planted truth on R0, so both are
partially oracle (flagged as such in eval reports). Task B pass rule
(pre-registered): AP lift of B3 over B1 ≥ 15% relative on the holdout
window.

## 12. ScoreRealization (audit blob)

```json
{
  "state_hash": "…", "algorithm_version": "0.2.0", "model_config_hash": "…",
  "data_hash": "…", "judgment_version": "det",
  "rng_seed": "…", "p_hat": 0.0, "p_interval": [0.0, 0.0],
  "loss_quantiles": {"0.5": 0, "0.9": 0, "0.95": 0, "0.99": 0},
  "loss_mean": 0,
  "components": {
    "atoms": [{"risk_id": "…", "lambda": 0.0, "sev_family": "lognormal",
               "sev_params": {"mu": 0, "sigma": 0}, "n": 0, "source": "…"}],
    "dependence": {"type": "shrunk_empirical_joint", "order": 2,
                   "params": {"kappa": 4, "q_quarterly": 0, "q_independence": 0},
                   "data_support": 0},
    "interval": {"p_lo": 0.0, "p_hi": 0.0, "p_median": 0.0,
                 "posterior_mean": 0.0, "draws": 0, "two_level": true,
                 "frechet_clipped": true, "p_hat_above_hi": false},
    "modifiers": [], "judgment": [],
    "mc": {"M": 20000, "seed": "…", "p_hat_mc": 0.0, "hit_count": 0,
           "alpha_overlap": 1.0},
    "evt": null
  },
  "uncertainty_class": "mixed", "disagreement": 0.0,
  "novelty": 1.0, "evidence_support": 0.0
}
```

`dependence.type` is `"mixture"` when the challenger is active, with
the posterior coupling weight audited alongside. On activation regimes
(`real_loss_fraction == 0` in r1 diagnostics) dollar losses are
suppressed: the loss fields are null and the artifact carries
`loss_units: "activation"` instead. A validator can reconstruct any
score from this blob plus the train data. That is the acceptance
criterion for the audit story.

Related stores outside this document's scope: the append-only
`score_realizations` history, the proposal queues (merge / channel /
analogue), refutation memos, and the judgment ledger are inventoried in
`erd.md`; none of them ever rewrites a score.
