# Rating Compound Operational-Risk Scenarios

## A deterministic hybrid scoring algorithm with pre-registered falsification

**Contingency Atlas white paper — scoring engine (`algorithm_version` 0.1.0)**

| | |
|---|---|
| Status | Implemented and evaluated on the synthetic R0 regime; research-tier per the model inventory |
| Specification | [`docs/redesignv1/algorithm-spec.md`](../redesignv1/algorithm-spec.md) |
| Implementation | [`contingency_atlas/scoring/`](../contingency_atlas/scoring/) |
| Evaluation | [`generated/redesign/`](../generated/redesign/) (reproducible via `atlas-eval demo`) |
| Date | 2026-08-03 |

> Outputs are hypothetical research artifacts. Scenario scores express model
> assumptions and prioritization choices, not calibrated probabilities or
> forecasts. They are not financial, legal, regulatory, investment, security,
> or risk-management advice, and they are not capital parameters. See
> [DISCLAIMER.md](../DISCLAIMER.md).

---

## Abstract

Banks are not surprised by single risks; they are surprised by *combinations* —
a control failure, a third-party concentration, and an external shock that
materialize together. Contingency Atlas rates such **compound scenarios** with
a deliberately conservative algorithm: standard frequency × severity
decomposition per risk atom, a shrunk-empirical dependence estimator, Monte
Carlo propagation under an explicit occurrence definition, and a peaks-over-
threshold EVT tail overlay with a built-in falsification rule. Every score
persists its full component decomposition so a validator can reconstruct it
exactly. The algorithm is deterministic end to end: no large language model
(LLM) participates in scoring. On the pre-registered synthetic evaluation
regime, the dependence-aware scorer beats the independence baseline by 84%
relative average precision (threshold: 15%) and recovers planted dependence
structure at 77% precision — while its own calibration harness documents where
the approach is still weak. This paper specifies the algorithm, its
auditability contract, and the evidence for and against it.

---

## 1. The problem: rating combinations, not risks

Operational-risk taxonomies and loss databases describe single event types.
Preparedness questions are about conjunctions: *what happens if risk A and
risk B materialize in the same quarter, and how bad is the loss if they do?*
Answering that naively fails in two opposite directions:

- **Independence multiplication** \(\prod_i f_i\) prices joint occurrence far
  too low. Risks that share a causal channel (a common cloud provider, a
  common funding model, a common regulatory trigger) fire together, and the
  error concentrates exactly in the tail that matters.
- **Unstructured judgment** ("these feel 70% correlated") prices joint
  occurrence unaccountably. It cannot be audited, calibrated, or falsified,
  and LLMs make it fluent rather than right.

The rating algorithm described here occupies the disciplined middle:
dependence estimated from data with an explicit shrinkage prior, judgment
admitted only through a protocolized side channel that never touches the
observation path, and every number carrying its provenance.

The design stance is stated up front in the specification:

> **Novel where novelty buys coverage and auditability of search; boring
> where numbers must survive model validation.**

Candidate *generation* (channel-blocked graph expansion) may be novel. The
*rating* of a candidate — frequency, severity, dependence, tails, pooling —
uses methods a model-validation function already knows how to challenge.

---

## 2. What is being rated

A **risk atom** \(r_i\) is a single emerging risk from the Atlas inventory,
mapped to an event-type cell and tagged with causal **channels** (identity,
cloud, payments, model, credit-chain, physical, legal, …).

A **compound scenario** \(S\) is a set of atoms \(R_S = \{r_1,\dots,r_k\}\),
\(k \ge 2\), a horizon \(H\) (default one year, in quarterly buckets), an
optional transmission narrative for humans, and a **score vector**
\(\mathbf{v}(S)\) with uncertainty. The output is a distribution, not a point:

- \(\hat p(S)\) — probability that the scenario's occurrence definition fires
  within \(H\), with a posterior interval;
- \(L \mid S\) — the conditional loss severity distribution, reported as
  quantiles \(q_{0.5}, q_{0.9}, q_{0.95}, q_{0.99}\) and mean.

**Occurrence is defined explicitly.** The default is **all-must-fire**: the
compound occurs in a quarter iff *every* atom in \(R_S\) fires in that
quarter. Softer \(k\)-of-\(n\) definitions are admitted only as a documented
sensitivity. Ambiguity here would make every downstream number meaningless,
so the definition is part of the scenario's structural identity (§5.4).

**Language discipline.** \(\hat p(S)\) is a *research quantity with
calibration tracking*, not a capital parameter, until validated. "Urgency"
scores elsewhere in Atlas are prioritization devices, not probabilities, and
compound scores never map onto them.

---

## 3. The rating algorithm

The algorithm is a layer stack. Phase 1 implements layers 1–5
deterministically; layers 6–7 (judgment, pooling) exist as protocolized
channels with mandatory provenance fields and are empty on the deterministic
path. Layer 8 is the evaluation harness of §6.

```text
[1] Atom frequency        quarterly occurrence, EB-shrunk
[2] Atom severity         lognormal, shrunk to pool when thin
[3] Dependence            shrunk empirical joint, Frechet-clipped
[4] Joint occurrence+loss Monte Carlo, all-must-fire
[5] Tail overlay          GPD peaks-over-threshold, falsifiable
[6] Judgment priors       protocolized, tagged, never observation
[7] Pooling               logarithmic opinion pool, disagreement kept
[8] Diagnostics           calibration, ablations, audit package
```

### 3.1 Layer 1 — frequency

Each atom is reduced to a quarterly occurrence indicator: the atom *fired* in
quarter \(t\) if at least one mapped event (loss, or signal activation on the
public regime) exists. Raw per-atom rates are noisy at realistic sample
sizes, so the quarterly probability is **empirical-Bayes shrunk toward the
pooled rate** across all atoms:

\[
q_i = \frac{N_i + s_{\mathrm{EB}}\,\bar\pi}{T + s_{\mathrm{EB}}},
\qquad
\bar\pi = \frac{\sum_j N_j}{n_{\mathrm{atoms}}\,T},
\]

with shrinkage strength \(s_{\mathrm{EB}} = 4\) pseudo-quarters (setting it to
0 falls back to add-half Laplace smoothing). This default came out of the
calibration harness itself (§6.3): pooled rates beat noisy per-atom MLEs when
true rates are near-homogeneous. Annualization over the horizon:

\[
f_i(H) = 1 - (1 - q_i)^{H_q}, \qquad H_q = 4 \text{ quarters by default}.
\]

When no planted or registered public counts exist, public proxy rates may
enter only as variance-inflated priors flagged `public_proxy`; otherwise the atom is marked
`frequency_source = elicited` — no fake precision.

### 3.2 Layer 2 — severity

Severity is lognormal on positive loss amounts, with a three-tier source
hierarchy driven by the cell sample size \(n\) (threshold
\(n_{\min} = 30\)):

| Cell data | Fit | `source` tag |
|---|---|---|
| \(n \ge 30\) | cell MLE \((\mu_i, \sigma_i)\) | `cell_data` |
| \(2 \le n < 30\) | convex blend with pool, \(w = n/(n+n_{\min})\) | `shrunk_to_pool` |
| \(n < 2\) | pooled prior \((\mu_{\mathrm{pool}}, \sigma_{\mathrm{pool}})\) | `pooled_prior` |

Every marginal persists its family, parameters, sample size, and source tag,
so a validator can see exactly how much of a severity claim is data and how
much is pool.

### 3.3 Layer 3 — dependence (the actual product)

This is the layer that distinguishes the algorithm from independence
multiplication, and it is deliberately the *first rung* of the spec's
data-supported ladder (empirical co-occurrence; copulas and expert Bayesian
networks are higher rungs for richer data).

For a combination of atoms with quarterly marginals \(\{q_i\}\), observed
co-firing support \(N_S\) (quarters where all atoms fired) over \(T\)
training quarters, the joint quarterly probability is

\[
q_S = \frac{N_S + \kappa \prod_i q_i}{T + \kappa},
\qquad
q_S \leftarrow \min\!\big(q_S,\; \min_i q_i\big),
\]

a pseudo-count prior (\(\kappa = 4\) quarters) **centred on independence**,
clipped to the Fréchet upper bound. Interpretation: with no observed
co-occurrence the estimate decays smoothly to the independence product as
data accumulates; observed co-firing pulls it up with explicitly bounded
prior influence. Both the shrunk estimate and the independence product are
stored per score, so the dependence *correction* is itself an audited number.

Uncertainty on \(q_S\) is a Beta posterior under the same shrinkage prior
(pseudo-counts floored to avoid degenerate cells), propagated through 4,000
posterior draws and annualized to the reported 90% interval on \(\hat p(S)\).

### 3.4 Layer 4 — Monte Carlo propagation

For each scored combination (deterministic seed derived from the scenario's
state hash; \(M = 20{,}000\) draws):

1. Simulate \(H_q\) quarters; in each quarter the joint fires with
   probability \(q_S\) (all-must-fire).
2. On a hit quarter, draw each atom's severity from its lognormal marginal
   and aggregate. The overlap factor \(\alpha_{\mathrm{overlap}}\) is fixed
   at 1.0 in Phase 1 (no P&L double-count correction yet; a bounded
   modifier is specified for later phases).
3. Collect the positive totals: \(\hat p_{\mathrm{MC}}\) is the hit fraction
   (cross-check on the analytic \(\hat p\)), and the empirical quantiles of
   the hit losses are the reported \(L \mid S\) distribution.

### 3.5 Layer 5 — tail overlay (EVT), with self-falsification

When the MC sample yields enough positive hits (\(\ge 200\)), a generalized
Pareto distribution is fit by method of moments (Hosking & Wallis) to
excesses over the 90th-percentile threshold \(u\), requiring
\(n_u \ge 20\) excesses:

\[
Q_{\mathrm{EVT}}(p) = u + \frac{\beta}{\xi}
\left( \Big(\tfrac{1-p}{n_u/n}\Big)^{-\xi} - 1 \right).
\]

The empirical quantiles remain authoritative; the overlay *reports alongside*.
The spec's falsification rule is implemented, not aspirational: the shape
\(\xi\) is refit at neighbouring thresholds (0.85, 0.95), and if it flips
sign or swings by more than 50%, the overlay returns `stable=false`, emits no
EVT quantiles, and the tail claim is downgraded to qualitative. With too few
excesses the overlay is absent and the tail is labeled "parametric
extrapolation, low confidence."

### 3.6 Layers 6–7 — judgment and pooling (protocolized, off by default)

LLM or expert priors are permitted only under a mandatory protocol: quantile
elicitation (p10/p50/p90, never a point), a stated reference class, requested
disconfirming evidence, a second independent elicitation, and conversion to a
parametric prior by an explicit method. Everything derived is tagged
`judgment` and is never mixed into `observation` without a review flag.
Where independent judgment sources combine, a logarithmic opinion pool is
used and the **range across sources is retained as a first-class
`disagreement` signal** — averaging must not hide dissent. On the
deterministic path these channels are empty arrays in the audit blob.

### 3.7 Uncertainty classification

Every score is labeled so consumers know what they are holding:

- `statistical` — every atom has \(n \ge n_{\min}\) severity data **and** the
  pair has \(\ge 3\) co-firing quarters in training, with dependence enabled;
- `judgment_dominant` — any critical atom elicited, or dependence expert-only;
- `mixed` — otherwise.

An `evidence_support` summary (half severity data depth, half co-occurrence
support, each squashed to \([0,1]\)) accompanies the class. Rows labeled
`judgment_dominant` are workshop material only.

---

## 4. Candidate generation: who gets rated

Rating all \(\binom{n}{2}\) pairs is affordable at demo scale and wrong as a
product habit. Candidates are **channel-blocked**: the channel graph connects
atoms sharing at least one channel tag (plus, optionally, pairs with
empirical co-occurrence above a threshold), and only connected pairs are
scored. This aligns generation with the dependence model — compounds are
expected to organize along shared causal channels — and it is what the
evaluation tests (§6.4). The full Cartesian product remains available as the
A2 ablation.

In production the algorithm runs as a **monthly compound join** at the end of
the six-phase Atlas pipeline: every \(\ge 28\) days it builds the quarterly
signal-activation series from the approved public evidence store, fits the
scorer on all but the last two quarters, scores channel-blocked pairs, and
persists the top 20 with their full score vectors. When the public series is
too sparse, the run is recorded as `insufficient_data` with the reason and
**fabricates nothing**. Separately, a graduation step can promote a compound
that survives its holdout into the scenario library: an LLM drafts the prose,
but the stored score fields come from the deterministic realization — the
model writes words, never numbers.

---

## 5. The auditability contract

Model validation must be able to reconstruct any number from stored
artifacts. Three mechanisms guarantee it.

### 5.1 The score vector

Every rated scenario persists a `ScoreRealization` (JSON) containing, per
`algorithm-spec.md` §9: \(\hat p\) with interval, loss quantiles and mean,
per-atom fits (rate, severity family/parameters, \(n\), source), the
dependence block (type, \(\kappa\), shrunk and independence joints, data
support), modifiers, judgment entries, the MC block (draws, seed, hit count,
\(\alpha_{\mathrm{overlap}}\)), the EVT overlay or its explicit absence, the
uncertainty class, disagreement, evidence support, and the hashes below.

### 5.2 Structural state hash

A scenario's identity is a SHA-256 over canonical JSON of **structural
inputs only**: sorted risk ids, horizon, occurrence definition, channel-graph
hash, feature-registry hash, and the algorithm major version. Scores, seeds,
timestamps, and prose are deliberately excluded — two runs of the same state
with different MC seeds are the same *state*, different *realizations*. The
state hash also derives the per-scenario RNG seed, making the Monte Carlo
exactly reproducible per scenario rather than per run order.

### 5.3 Versioning and determinism

`algorithm_version` is semver; a breaking change is one where score
components change meaning. Every realization stores the version, the
config-hash (all scorer hyperparameters), and the RNG seed. The scorer is
standard-library only — no third-party numerics — so the same inputs and
config reproduce the same bytes on any machine.

### 5.4 Confidentiality boundary

The synthetic scoring-cell demo strips planted free text, suppresses sparse
scoring-cell/segment groups, and writes two tiers: aggregate structural features
(the only tier the egress gateway's `scoring_features` allow-list admits)
and fictional event rows that are local-only by construction. No egress
schema exists for those rows, so the gateway denies the sampled events.
Frontier models never see local-detail text; the local-to-external boundary
is enforced by a hash-chained audit log with red-team tests.

---

## 6. Evaluation: pre-registered, then believed

The algorithm ships with its own kill criteria. The evaluation plan
([`evaluation-plan.md`](redesign/evaluation-plan.md)) fixes the data regimes,
holdouts, baselines, thresholds, and falsification rules *before* results are
produced. Phase 1's decision regime is **R0**: a synthetic world of 15 atoms,
3 planted channels, and 40 quarters, with train quarters [0, 31] and a locked
test window [32, 39]. Planted structure is ground truth.

### 6.1 Task B — pair co-occurrence ranking (the core product claim)

Can the scorer rank the pairs that will actually co-fire in the holdout above
those that will not? 105 pairs, 39 holdout positives, pre-registered
baselines:

| Baseline | AP | AUROC | P@20 |
|---|---|---|---|
| B0 random | 0.391 | 0.525 | 0.35 |
| B1 independence (product of marginals) | 0.398 | 0.521 | 0.25 |
| B2 shared-channel indicator | 0.697 | 0.777 | 0.80 |
| **B3 deterministic batch scorer** | **0.734** | **0.761** | **0.90** |

Pre-registered pass: AP lift over B1 ≥ 15% relative. Observed lift: **84.3%**
→ **PASS**. Note what the table says about the algorithm's anatomy: channel
knowledge (B2) carries most of the signal, and the shrunk-empirical
dependence layer adds the rest — while naive independence is barely better
than random.

### 6.2 Task C — planted structure recovery

With the planted channel graph hidden from the graph builder, co-occurrence
lift must recover it: precision@30 = **0.767** (threshold 0.70) and the
planted fraction of the top-decile urgency ranking = **0.909** (threshold
0.70) → **PASS**.

Combined recommendation of the harness: **GO**. (The Phase 2 multiverse
search loop later passed its own pre-registered batch-vs-loop test but was
removed from mainline on 2026-08-01: at live-data scale batch scoring does
everything production needs. The rating algorithm is unaffected — batch is
the production path.)

### 6.3 Task A — atom frequency calibration (an honest FAIL)

The same harness grades the marginals: on R0 the strict rule **FAILs** — the
model's negative log-likelihood (76.34) loses to a pooled naive rate (72.47),
and 90% interval coverage (0.967) sits above the (0.80, 0.95) band. The
report is part of the artifact and states the two causes plainly: discrete
intervals over-cover at low rates (a regime property, unreachable band), and
no per-atom model can beat pooling when true rates are near-homogeneous. Two
fixes followed, both now in the algorithm: empirical-Bayes shrinkage of atom
rates toward the pool (§3.1), and an R0′ count regime with a Poisson/NB
family choice on which NLL beats the naive baseline on both regimes —
coverage still documented as over-covering at low rates. The FAIL is quoted
here because the white paper's claim is not "the algorithm is right" but "the
algorithm's failures are measured and priced in."

### 6.4 Ablations — does each layer earn its keep?

| Ablation | Expectation if the design is right | Observed | Consistent |
|---|---|---|---|
| A1 remove dependence | Task B AP drops | AP 0.734 → **0.398** (= B1 exactly) | yes |
| A3 remove EVT overlay | tail quantiles less stable across seeds | q99 CV 0.045 → **0.034** with EVT | yes |

A1 is the cleanest possible confirmation that the dependence layer *is* the
product: ablating it collapses the scorer onto the independence baseline to
three decimal places. A5 (external features) stays gated: the FDR-disciplined
feature registry exists, but accepted `associational_feature`s require
human/policy acceptance before they may act as modifiers, so the ablation
activates with the modifier path.

### 6.5 External-context discipline

Because time-aligning "what else was happening" with sparse losses is a
machine for just-so stories, external features face a gauntlet before they
may influence a rating: pre-registration of feature families, a fixed
quarterly calendar, train-only association tests with block-bootstrap CIs, a
one-shot holdout direction check, Benjamini–Hochberg FDR at \(q = 0.10\),
minimum support of 8 quarters, and an **adversarial null pass** that must
write the best *non-causal* explanation. Survivors are labeled
`associational_feature` — modifier candidates, never causal evidence.

---

## 7. Limitations, stated before the conclusion

- **R0 is synthetic.** Passing on planted structure proves the machinery
  recovers structure, not that the world has that structure. On the public
  regime (R1) the Task B harness itself **FAILs** — 78 scored pairs but only
  4 holdout positives, too sparse to rank — which is exactly why R0 is the
  decision regime and why the production join gates on data sufficiency
  rather than scoring anyway. Absolute calibration is not established beyond
  the shipped synthetic and sparse public-demo regimes.
- **Atom calibration is the weak layer** (§6.3). Compound ranking is the
  product claim; absolute \(\hat p(S)\) values are research quantities with
  calibration tracking, not validated probabilities.
- **Severity dependence between atoms is not modeled** (independent
  severities, \(\alpha_{\mathrm{overlap}} = 1.0\)). Compound losses can be
  mis-aggregated when atoms share a P&L event.
- **The dependence estimator is pairwise-empirical.** With sparse data it is
  a shrunk co-occurrence rate, not a causal claim; copulas and expert DAGs
  are specified higher rungs for richer data and are not yet fit.
- **Compounds are rare by definition**, so holdout positives are few and
  metrics are noisy; the harness counters with proxy tasks and synthetic
  injection, not with bigger claims.

---

## 8. Conclusion

The contribution of this work is not a new estimator — every component is a
method model validation already trusts. It is the **assembly discipline**: an
explicit occurrence definition, shrinkage priors centered on the skeptic's
null (independence), a tail overlay that can fail its own stability test, an
audit blob from which every digit is reconstructable, and a pre-registered
harness that has already recorded FAILs — including against itself — and
priced them into the design.
The algorithm rates compound scenarios the way a conservative institution
should: dependence-aware because independence is demonstrably wrong in the
tail, deterministic because scores must be reproducible, and humble because
the falsification criteria for retiring it are already written down.

---

## Appendix A — Default configuration

| Parameter | Default | Meaning |
|---|---|---|
| `horizon_quarters` | 4 | rating horizon (1 year) |
| `mc_draws` | 20,000 | Monte Carlo draws per scenario |
| `shrink_kappa` | 4.0 | dependence pseudo-count (quarters), prior centred on independence |
| `n_min` | 30 | min events for a parametric severity claim |
| `pair_support_min` | 3 | min co-firing quarters for `statistical` class |
| `eb_strength` | 4.0 | empirical-Bayes shrinkage of atom rates (pseudo-quarters) |
| `interval_draws` | 4,000 | Beta posterior draws for the \(\hat p\) interval |
| `evt_min_hits` | 200 | min positive MC hits before the EVT overlay runs |
| `evt_threshold_quantile` | 0.90 | POT threshold; sensitivity refits at 0.85 / 0.95 |
| `seed` | 20260801 | base RNG seed (per-scenario seeds derive from the state hash) |
| `dependence_mode` | `shrunk_empirical` | `independence` activates ablation A1 |

## Appendix B — Reproduction

```bash
atlas-eval demo            # generate R0, score all pairs, run Task B + C
                           # on the held-out last 8 quarters; writes
                           # generated/redesign/phase1/r0/eval_report.{json,md}
atlas-eval calibrate ...   # Task A calibration harness
atlas-eval ablations ...   # runnable pre-registered ablation matrix
python -m contingency_atlas.compound_join --force   # production monthly join
```

## Appendix C — Evidence map: working papers, artifacts, code

Every claim in this paper resolves to one of three kinds of evidence, all in
this repository: the pre-registered working papers that fixed the rules, the
generated artifacts that recorded the outcomes, and the code that produced
both.

### C.1 Working papers (specification pack — `docs/redesignv1/`)

- `algorithm-spec.md` — normative layer stack, elicitation and fusion protocols, falsification
- `evaluation-plan.md` — pre-registered tasks A–F, baselines, thresholds, abandon criteria
- `data-model.md` — schemas, structural state hash, lineage rules
- `architecture.md` — components, descriptive data classes, tech choices
- `loop-design.md` — candidate generation (channel-blocked expansion); the multiverse loop itself was removed from mainline on 2026-08-01
- `telemetry-spec.md` — cost/token instrumentation contract
- `risks-and-objections.md` — the honest case against the system
- `roadmap.md` — phase plan and decision log
- `model-inventory.yaml` — research-tier registration, restrictions, evidence links
- `pitch.md`, `visualization-spec.md` — context documents

### C.2 Primary evaluation evidence (`generated/redesign/`)

Phase 1 — R0 go/no-go (`phase1/`):

- `r0/eval_report.md` / `.json` — Task B and Task C results behind §6.1–6.2 (recommendation GO)
- `r0/r0_risks.json`, `r0/r0_events.jsonl`, `r0/r0_truth.json` — the synthetic regime and its planted ground truth
- `pairs/score_realizations.jsonl` — one full audit blob (§5.1) per scored pair
- `pairs/run_report.json`, `pairs/telemetry.jsonl` — cost rollup ($0 on the deterministic path) and per-pair telemetry

Phase 3 — harness and boundary runs (`phase3/`):

- `calibration/calibration_report.md` / `.json` — Task A result behind §6.3 (strict FAIL, with cause analysis)
- `ablations/ablations.md` / `.json` — ablation matrix behind §6.4 (A1 and A3 runnable; A2/A4/A6 historical rows)
- `r1/r1_eval.md` / `.json` — public-regime Task B: FAIL on sparse data (78 pairs, 4 holdout positives); records the regime boundary cited in §7
- `synthetic-cell-demo/` — fictional scoring-cell exercise: `eval_report.md` / `.json` (Task B and C pass), `aggregation_report.json` (65/68 rows accepted, sparse groups suppressed), `egress_audit.jsonl` (aggregate features admitted, sampled local-detail event rows denied), `scoring_features.jsonl`, `synthetic_scoring_events.jsonl`, and `synthetic_events.csv`
- `ext-features/ext_report.md` / `.json`, `ext_registry.yaml` — the §6.5 screen on a planted series: the strong planted feature accepted (RR 14.4 and 12.0, q < 0.1), noise features FDR-rejected, per-family registry status
- `workshop/` — example human-facing scenario packs produced from scored realizations

### C.3 Implementation and tests

- `contingency_atlas/scoring/scorer.py` — Layers 1–4 (frequency, severity, dependence, Monte Carlo)
- `contingency_atlas/scoring/evt.py` — Layer 5 (GPD overlay with threshold-sensitivity falsification)
- `contingency_atlas/scoring/calibration.py`, `evaluate.py`, `ablations.py` — the harness that produced C.2
- `contingency_atlas/scoring/ext_features.py` — the §6.5 FDR screen
- `contingency_atlas/scoring/egress.py` — local-to-external gateway with hash-chained audit log
- `contingency_atlas/compound_join.py`, `compound_graduate.py` — production monthly join and graduation (§4)
- `tests/test_scoring_*.py` — unit and integration coverage, including egress red-team tests and the runnable ablations
