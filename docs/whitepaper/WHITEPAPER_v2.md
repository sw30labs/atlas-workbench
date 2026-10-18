# Rating Compound Operational-Risk Scenarios

## A deterministic scoring algorithm with pre-registered falsification

**Contingency Atlas white paper — scoring engine (`algorithm_version` 0.1.0)**

| | |
|---|---|
| Status | Implemented and evaluated on the synthetic R0 regime; research-tier per the model inventory |
| Revision | **2** — amended after an adversarial full-repo review ([`WHITEPAPER_REVIEW.md`](WHITEPAPER_REVIEW.md)); change map in [`WHITEPAPER_v2_REVISION_LOG.md`](WHITEPAPER_v2_REVISION_LOG.md); v1 preserved unchanged as the reviewed artifact |
| Companion | [`HYBRID_SCORING_PROPOSAL.md`](HYBRID_SCORING_PROPOSAL.md) — the algorithm fixes and judgment-channel design this revision cites as scheduled work |
| Specification | [`docs/redesignv1/algorithm-spec.md`](../redesignv1/algorithm-spec.md) |
| Implementation | [`contingency_atlas/scoring/`](../../contingency_atlas/scoring/) |
| Evaluation | `generated/redesign/` — reproducible via Appendix B; **not under version control** (Appendix C) |
| Date | 2026-08-03 |

> Outputs are hypothetical research artifacts. Scenario scores express model
> assumptions and prioritization choices, not calibrated probabilities or
> forecasts. They are not financial, legal, regulatory, investment, security,
> or risk-management advice, and they are not capital parameters. See
> [DISCLAIMER.md](../../DISCLAIMER.md).

---

## Executive summary

*This section is for readers who are not statisticians. It says what the
system does, what the unavoidable jargon means, and what the results do and
do not show — at the same level of honesty as the technical sections, just
without the mathematics. A glossary follows it.*

Institutions prepare for risks one at a time; the events that actually hurt
are usually **combinations** — a control failure, a concentrated supplier,
and an external shock landing in the same quarter. This system finds and
scores those combinations, and it does so with a method deliberately built
from components a bank's model-validation team already knows how to
challenge.

The approach, in plain terms. Each single risk from the register is
treated as an indivisible building block — a **risk atom**. The
"decomposition per risk atom" means each block is split into two separately
estimated questions: *how often does this happen?* and *how bad is it when
it does?* The system then asks the data how often two blocks have actually
struck **together**, starting from a skeptic's assumption — no connection —
and moving away from it only as evidence accumulates. Because the
arithmetic of rare combinations has no clean formula, the final step is a
**Monte Carlo** simulation: rehearse the coming year twenty thousand times,
count how often the combination lands, and record what it costs when it
does. (Monte Carlo is nothing more mysterious than dice-rolling at
industrial scale — the same family of simulation used for modeling attack
paths in security engineering; for a practitioner-level treatment of
simulation-driven defense see the author's book, *AI Agents in
Cybersecurity*.) A final overlay borrows from **extreme-value theory** —
the statistics of worst cases — to give a second opinion on the most severe
outcomes, and that overlay must pass its own stability test or it is thrown
away.

What is actually new here is not the mathematics — every component is
deliberately standard. The innovation is **discipline**: the pass marks for
every test were written down *before* the tests were run; the failures are
published next to the passes; every published number can be reconstructed
from stored files; and no AI language model writes or influences any
number — models draft prose for humans, never scores.

What the results honestly say. On a simulated world where the true answer
was planted, the system recovers the planted structure and modestly
out-ranks a reference method that was effectively handed the answer key —
strong evidence the machinery works, not yet evidence about the real world.
On real public data there is not yet enough shared history for the
dependence layer to add anything: today its production output matches the
naive method, and this paper says so. One known flaw is disclosed rather
than hidden: the current estimator *under*-prices exactly the rarest
never-yet-seen combinations — the very thing the product exists to
surface — and the designed fix will be adopted only if it wins a
pre-registered test. Loss-size estimates are not yet validated at all.
Until that changes, every output is a **research quantity**: useful for
prioritization and challenge, not a probability to act on, and never a
capital or business input.

### Plain-language key to the jargon

| Term | What it means here |
|---|---|
| Risk atom | One single risk from the register, treated as an indivisible building block (e.g. "identity-provider outage"). |
| Compound scenario | Two or more atoms striking in the same window — the thing this paper rates. |
| Decomposition per risk atom | Splitting each risk into "how often?" (frequency) and "how bad?" (severity), estimated separately and then recombined. |
| Dependence / co-occurrence | The tendency of risks to strike together because they share plumbing (same cloud provider, same funding model, same regulator). Measured by counting quarters in which both actually fired. |
| Shrinkage prior | The skeptic's starting point: assume no connection until the data argues otherwise. Thin evidence moves the estimate a little; strong evidence moves it a lot. |
| Monte Carlo | Rehearsing the coming year many thousands of times in simulation and counting the outcomes, because rare-combination arithmetic has no clean closed formula. Named after the casino. |
| Tail / extreme-value theory (EVT) | The worst few percent of outcomes, and the branch of statistics built to estimate them. Used here as a second opinion that must pass a stability check or be discarded. |
| Pre-registration | The pass marks were written down before the tests were run, so results cannot quietly move the goalposts. Failures are published beside passes. |
| Holdout | Data kept locked away while the model is fitted and used only for the final exam. |
| Calibration | Whether stated probabilities match reality: when the system says "10%", does it happen about one time in ten? |
| Audit blob / realization | The stored file from which every published digit can be reconstructed, down to the random seed. |
| Research quantity | A number for prioritization and challenge — not a validated probability, and never a capital or business input. |

---

## Abstract

Banks are not surprised by single risks; they are surprised by *combinations* —
a control failure, a third-party concentration, and an external shock that
materialize together. Contingency Atlas rates such **compound scenarios** with
a deliberately conservative algorithm: standard frequency × severity
decomposition per risk atom, a shrunk-empirical dependence estimator, Monte
Carlo propagation under an explicit occurrence definition, and a peaks-over-
threshold EVT tail overlay with a built-in falsification rule. Every
evaluation-path score persists its full component decomposition so a validator
can reconstruct it — exactly, up to the hash-coverage gaps §5.3 itemizes; the
production path does not yet honor even that much, and §5.5 says so. The algorithm is deterministic end to end: no
large language model (LLM) participates in scoring. On the pre-registered
synthetic regime, the dependence-aware scorer beats the independence baseline
by 84% relative average precision — a bar §6.1 shows the synthetic generator
made structurally easy — and beats the strong shared-channel baseline, which
on this regime is an oracle holding the planted answer key, by 5.2% AP and
0.90-vs-0.80 precision-at-20, while losing to it on AUROC. A harness utility
recovers planted dependence structure at 77% precision against a 29% random
floor (48% among co-occurring pairs). The same harness documents what fails:
atom-frequency calibration loses to a pooled baseline on the decision regime,
the public-regime task fails on sparsity — so the dependence layer has yet to
influence a single production number — and the shipped estimator prices rare,
never-co-observed pairs *below* independence, a defect the ranking-only
evaluation cannot see (§7.2). This paper specifies the algorithm, its
auditability contract, the evidence for it, and — at equal rank — the evidence
against it.

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
observation path, and every number carrying its provenance. (§7.2 documents
where the shipped estimator currently fails its own first bullet — for rare
pairs with no observed co-firing it undershoots even the independence
product. The design stance below is why that finding is disclosed rather
than buried.)

The design stance is stated up front in the specification:

> **Novel where novelty buys coverage and auditability of search; boring
> where numbers must survive model validation.**

Candidate *generation* (channel-blocked graph expansion) may be novel. The
*rating* of a candidate — frequency, severity, dependence, tails, pooling —
uses methods a model-validation function already knows how to challenge.

### 1.1 Related methods — why each layer is boring

Each layer is the textbook method for its slot, deliberately. Layer 1–2 is
the loss-distribution approach — frequency × severity per cell — standard
since Frachot, Georges & Roncalli (2001) and the Basel AMA lineage. The
empirical-Bayes shrinkage of noisy per-cell rates toward a pool is classical
partial pooling (Efron & Morris 1975). Layer 5 is peaks-over-threshold EVT
as used in operational risk since Moscadelli (2004) and treated at length in
Embrechts, Klüppelberg & Mikosch (1997) and McNeil, Frey & Embrechts (2015),
fit by the method of moments of Hosking & Wallis (1987) — which is also why
§3.5 states the fit's validity limit (\(\xi < 1/2\)) rather than hiding it:
the op-risk literature (Moscadelli 2004; Nešlehová, Embrechts &
Chavez-Demoulin 2006) reports severity tails at or beyond that limit often
enough that an overlay on lognormal output must not be read as evidence
about true tails. Layer 3 is the deliberately lowest rung of a dependence
ladder whose higher rungs — copulas, common-shock and factor models — are
the standard aggregation devices (Embrechts, McNeil & Straumann 2002; Böcker
& Klüppelberg 2008); the spec (§4.2–4.3) prescribes when to climb. The
judgment protocol of §3.6 is Cooke's classical model territory —
calibration-weighted expert combination (Cooke 1991), quantile elicitation
practice (O'Hagan et al. 2006), and opinion pools (Genest & Zidek 1986). The
external-feature screen of §6.5 is Benjamini–Hochberg (1995) FDR control.
The evaluation posture — thresholds fixed before results — follows
pre-registration norms (Nosek et al. 2018), and the audience posture — every
number reconstructable from stored artifacts — is model-risk-management
doctrine (Federal Reserve SR 11-7 / OCC 2011-12). The mixture-prior
challenger scheduled in §7.2 is spike-and-slab model averaging (Mitchell &
Beauchamp 1988; George & McCulloch 1993). The one non-boring element is the
assembly and its falsification harness, which is the contribution claimed in
§8 — no component asks a validator to trust a new estimator.

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
- \(L \mid S\) — the conditional loss distribution, reported as quantiles
  \(q_{0.5}, q_{0.9}, q_{0.95}, q_{0.99}\) and mean. Precisely: the **annual
  aggregate** loss summed over the horizon's hit quarters, conditional on at
  least one hit quarter — not a per-occurrence severity; the two differ
  whenever more than one quarter fires. As of this revision the loss side
  has **no evaluation task** (§6.6): occurrence is evaluated, severity is
  reported unvalidated.

**Occurrence is defined explicitly.** The default is **all-must-fire**: the
compound occurs in a quarter iff *every* atom in \(R_S\) fires in that
quarter. A softer \(k\)-of-\(n\) definition is **specified but not
implemented** in v0.1.0 — only an unused `k` slot exists in the state hash,
so a validator today cannot run that sensitivity (v1 called it "admitted as
a documented sensitivity", which overstated it). Ambiguity here would make
every downstream number meaningless, so the definition is part of the
scenario's structural identity (§5.2).

**Language discipline.** \(\hat p(S)\) is a *research quantity with
calibration tracking*, not a capital parameter, until validated. "Urgency"
scores elsewhere in Atlas are prioritization devices, not probabilities, and
compound scores never map onto them (§4 documents one leak in the reverse
direction that is being closed).

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
[6] Judgment priors       protocolized, tagged, never observation — specified, empty
[7] Pooling               logarithmic opinion pool — specified, not implemented
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
true rates are near-homogeneous. (§6.1 discloses the circularity this
creates — the tuning regime and the decision regime are the same R0 — and
what softens it.) Annualization over the horizon:

\[
f_i(H) = 1 - (1 - q_i)^{H_q}, \qquad H_q = 4 \text{ quarters by default}.
\]

The spec additionally prescribes external peer rates entering as
variance-inflated priors flagged `ELD-proxy`, with
`frequency_source = elicited` otherwise. That mechanism is **specification,
not shipped code** — no such tags are produced anywhere in v0.1.0 (v1 used
the present tense here; this revision does not).

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
much is pool. (Two known blemishes, recorded in §7.4: the blend weight jumps
discontinuously at the \(n = 30\) threshold, and \(\sigma\) is blended
linearly where log-variance blending is the defensible choice under a
lognormal model.)

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

a pseudo-count prior (\(\kappa = 4\) quarters) centred on independence **in
mean**, clipped to the Fréchet upper bound. Observed co-firing pulls the
estimate up with explicitly bounded prior influence, and both the shrunk
estimate and the independence product are stored per score, so the
dependence *correction* is itself an audited number.

**Interpretation requires care, and v1 got it wrong.** With no observed
co-occurrence (\(N_S = 0\)), \(q_S = \kappa\, q_{\mathrm{ind}}/(T+\kappa)\):
the estimate equals the independence product only at \(T = 0\) and decays
**below** it as training data accumulates — the opposite of v1's "decays
smoothly to the independence product". At the defaults (\(T = 32,
\kappa = 4\)) every zero-support pair is priced at \(4/36 \approx 11\%\) of
its independence probability, regardless of whether zero co-fires is
evidence (common pairs, where independence expected several co-fires) or the
expected outcome under independence itself (rare pairs — probability 0.92 of
zero co-fires at \(q_i = 0.05\)). For rare pairs the markdown is therefore a
prior artifact, not evidence; §7.2 states the consequence — the estimator
undershoots independence exactly in the tail this product exists to price —
and the scheduled remedy.

Uncertainty on \(q_S\) is a Beta posterior under the same shrinkage prior
(pseudo-counts floored at \(10^{-6}\) to avoid degenerate cells), propagated
through 4,000 posterior draws and annualized to the reported 90% interval on
\(\hat p(S)\). **Known defect (found in review):** in 15 of the 105 shipped
audit blobs the reported point estimate — the posterior *mean* — lies above
the interval's upper end, because at \(\alpha = \kappa\, q_{\mathrm{ind}}
\ll 1\) the posterior is L-shaped (mean above the 95th percentile of its own
draws), `betavariate` underflows at tiny \(\alpha\), and the Fréchet clip
binds the point but is not applied to the draws. §7.2 gives the numbers and
the scheduled fixes.

### 3.4 Layer 4 — Monte Carlo propagation

For each scored combination (deterministic seed derived from the scenario's
state hash; \(M = 20{,}000\) draws):

1. Simulate \(H_q\) quarters; in each quarter the joint fires with
   probability \(q_S\) (all-must-fire).
2. On a hit quarter, draw each atom's severity from its lognormal marginal
   and aggregate. One severity draw per atom per hit quarter — historical
   event *counts* within a quarter are not used (a known simplification;
   §7.4). The overlap factor \(\alpha_{\mathrm{overlap}}\) is fixed at 1.0
   in Phase 1 (no P&L double-count correction yet; a bounded modifier is
   specified for later phases).
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

**Scope of the claim.** The overlay fits the MC *aggregate*, which is
generated from lognormal marginals. It is a variance-reduction device on
model output — that is exactly what ablation A3 measures (§6.4) — and it can
never detect heavier-than-lognormal true severity: the tail ceiling is set
at Layer 2, and MoM-GPD is in any case valid only for \(\xi < 1/2\), while
the op-risk literature routinely reports heavier severity tails (§1.1).
Data-side tail treatment (severity family selection at Layer 2, POT on
event-level excesses) is specified, not shipped (§7.4). One degradation
mode, visible in the emitted `xi_sensitivity` keys: a neighbouring threshold
whose refit would have fewer than 20 excesses is silently skipped — at
exactly 200 hits the 0.95 refit (~10 excesses) does not run and the
stability verdict rests on the 0.85 side alone. The check can therefore
degrade to one-sided, and the artifact records when it did.

**Artifact status (correction from v1):** all 105 shipped pair realizations
predate this layer and carry `evt: null` (56 of them meet the current
≥ 200-hit gate and would exercise the overlay if re-emitted), and freshly
generated evaluation reports still embed a stale "EVT tail layer is a stub"
limitation string from before the layer existed — as does the scorer module
docstring. String fixes and artifact re-emission are scheduled (§5.5); until
they land, a reader who runs Appendix B gets a report contradicting this
section, and this sentence is the warning.

### 3.6 Layers 6–7 — judgment and pooling (specified, not implemented)

The spec (algorithm-spec §7–8) prescribes a mandatory protocol for LLM or
expert priors: quantile elicitation (p10/p50/p90, never a point), a stated
reference class, requested disconfirming evidence, a second independent
elicitation, and conversion to a parametric prior by an explicit method —
everything tagged `judgment`, never mixed into `observation` without a
review flag. Where independent judgment sources combine, the spec prescribes
a logarithmic opinion pool with the range across sources retained as a
first-class `disagreement` signal.

**None of this machinery ships in v0.1.0.** The audit-blob *fields* exist
and are empty on every realization; no elicitation, pooling, or
judgment-fusion code exists in the tree (the model inventory records
`judgment_channel: none`). v1 described the pool in the present tense; that
was specification wearing implementation's tense. The companion proposal
specifies how these layers activate and under what gates.

### 3.7 Uncertainty classification

Every score is labeled so consumers know what they are holding:

- `statistical` — every atom has \(n \ge n_{\min}\) severity data **and** the
  pair has \(\ge 3\) co-firing quarters in training, with dependence enabled;
- `judgment_dominant` — any critical atom elicited, or dependence expert-only
  (**unreachable in v0.1.0**: no elicitation exists, so the deterministic
  path emits only `statistical` or `mixed`);
- `mixed` — otherwise.

An `evidence_support` summary (half severity data depth, half co-occurrence
support, each squashed to \([0,1]\)) accompanies the class. Rows labeled
`judgment_dominant` would be workshop material only — noting that the
workshop-material framing itself rests on Task D (expert resolution), which
has not run (§6.6).

---

## 4. Candidate generation: who gets rated

Rating all \(\binom{n}{2}\) pairs is affordable at demo scale and wrong as a
product habit. Candidates are **channel-blocked**: the channel graph connects
atoms sharing at least one channel tag, and only connected pairs are scored.
This aligns generation with the dependence model — compounds are expected to
organize along shared causal channels — and it is what the evaluation tests
(§6.4). The full Cartesian product remains available as the A2 ablation.

The graph API also admits pairs with empirical co-occurrence above a
threshold — but **the production join never exercises that option**: the
graph is built without event data and the empirical-edge flag defaults to
off. Consequence, stated plainly: two risks that demonstrably co-fire in the
data but share no inventory tag are structurally unscoreable in production
today, and production coverage rests entirely on keyword-derived channel
tags. For a system whose thesis is "dependence shows up in the data," the
data cannot yet nominate candidates. The companion proposal addresses the
coverage gap through gated channel-graph *enrichment* (§P1); activating the
data-driven empirical edge itself is not yet scheduled anywhere — this
sentence is the open item's record.

In production the algorithm runs as a **monthly compound join** at the end of
the six-phase Atlas pipeline: every \(\ge 28\) days it builds the quarterly
signal-activation series from the approved public evidence store, fits the
scorer on all but the last two quarters, scores channel-blocked pairs, and
persists the top 20. **What it persists is a trimmed row** — \(\hat p\) with
interval, two loss quantiles in the database (all four in the JSON
artifact), mean, uncertainty class, dependence and evidence support — not
the full §5.1 realization; §5.5 itemizes the gap. When the public series is
too sparse, the run is recorded as `insufficient_data` with the reason and
**fabricates nothing**. Separately, a graduation step can promote a compound
that survives its holdout into the scenario library: an LLM drafts the prose,
but the stored score fields come from the deterministic realization — the
model writes words, never numbers. (Appendix D maps where LLMs *do* touch
the wider system, because a validator will find those touchpoints and should
find them documented here first.)

**Production-integrity blemishes on this path, found in review and corrected
by proposal §A7:** on the public regime the severity inputs were
unit-polluted — missing loss amounts default to 1.0, and activation-only
"losses" are unit-free constants — yet graduation formats the resulting
quantiles as dollars ("q95 ≈ $0.0M"); loss quantiles on this regime have no
loss semantics and should be suppressed by a regime-aware output contract.
Graduation also stamps fixed metadata (`signal_maturity="established"`,
`momentum="accelerating"`, `evidence_strength="moderate"`) unconditionally —
"no fake precision" says derive or omit. And graduated compounds re-enter
the legacy urgency scorer, whose parser matches none of their deterministic
likelihood strings and silently assigns fallback constants (0.5 likelihood,
0.45 financial) — the letter of "compound scores never map onto urgency" is
kept while the urgency a graduated compound receives is *arbitrary*, which
was worse. The shipped correction persists `origin=compound_graduation` and
excludes that origin from phase-4 scoring; the cosmetic ID format is not used
as provenance.

---

## 5. The auditability contract

Model validation must be able to reconstruct any number from stored
artifacts. Three mechanisms implement the contract on the evaluation path;
§5.5 states where the contract is **not yet honored**, because an
auditability section that hides its own gaps would be self-refuting.

### 5.1 The score vector

Every scenario rated on the **evaluation path** (`atlas-score pairs`,
`atlas-eval`) persists a `ScoreRealization` (JSON, ~1.5 KB) containing, per
`algorithm-spec.md` §9: \(\hat p\) with interval, loss quantiles and mean,
per-atom fits (rate, severity family/parameters, \(n\), source), the
dependence block (type, \(\kappa\), shrunk and independence joints, data
support), modifiers, judgment entries, the MC block (draws, seed, hit count,
\(\alpha_{\mathrm{overlap}}\)), the EVT overlay or its explicit absence, the
uncertainty class, disagreement, evidence support, and the hashes below.
The production join persists a trimmed subset (§4); v1 claimed the full
vector for "every rated scenario", which was true only of the evaluation
path.

### 5.2 Structural state hash

A scenario's identity is a SHA-256 over canonical JSON of **structural
inputs only**: sorted risk ids, horizon, occurrence definition (including
the unused `k` slot), transmission id, scope, an assumptions hash,
channel-graph hash, feature-registry hash, and the algorithm major version.
Scores, seeds, timestamps, and prose are deliberately excluded — two runs of
the same state with different MC seeds are the same *state*, different
*realizations*. The state hash also derives the per-scenario RNG seed,
making the Monte Carlo exactly reproducible per scenario rather than per run
order.

Two identity defects, found in review and scheduled (they change hashes, so
the fix lands with a version bump): the horizon enters the hash coarsened to
whole years (`horizon_quarters // 4`), so 4–7-quarter horizons share one
structural identity and a 2-quarter horizon hashes as `"0y"` — the identity
layer cannot distinguish horizons the paper calls structural inputs. And
risk ids are slugified titles with order-dependent dedup suffixes, so a
title edit or register reorder silently re-identifies scenarios and strands
prior rows (§7.3 for the deeper identity problem this points at).

### 5.3 Versioning and determinism

`algorithm_version` is semver; a breaking change is one where score
components change meaning. Every realization stores the version, a
config hash, and the RNG seed.

**Hash-coverage gaps (correction from v1, which claimed "all scorer
hyperparameters"):** `interval_draws` is not in the config hash, the
\(10^{-6}\) interval pseudo-count floor is a hardcoded constant outside it,
and `realization_id` = SHA-256(state | seed | version) carries **no config
coordinate at all** — so two realizations differing only in
`interval_draws` (different \(p\)-interval and, through the shared RNG
stream, different loss quantiles) carry identical config hashes *and*
identical realization ids. Widening the hash surface (`interval_draws` in)
and adding a *data* coordinate to identity are scheduled (proposal §A7);
adding a config coordinate to `realization_id` is not yet scheduled
anywhere — it remains this paper's outstanding ask. Appendix A marks which
parameters are hash-covered today.

**Determinism, scoped precisely:** the scorer is standard-library only in
the score path (the external-feature registry imports PyYAML outside it), so
the same inputs and config reproduce the same bytes **on a given platform**.
`log`/`exp`/`lognormvariate` route through platform libm; cross-platform
bit-identity is likely but unverified — v1 claimed "any machine" without a
test. A cross-platform reproduction job is the scheduled fix; until it runs,
the claim is scoped to per-platform reproduction, which is what this
revision re-verified (Appendix B).

### 5.4 Confidentiality boundary

The synthetic scoring-cell demo strips planted free text, suppresses sparse
scoring-cell/segment groups, and writes two tiers: aggregate structural features
(the only tier the egress gateway's `scoring_features` allow-list admits)
and fictional event rows that are local-only by construction. No egress
schema exists for those rows, so the gateway denies the sampled events.
Frontier models never see local-detail text; the local-to-external boundary
is enforced by a hash-chained audit log with red-team tests. (Bookkeeping note
for validators counting raw lines: the shipped audit log accumulates two
rehearsals — 46 rows, 6 allows / 40 denies in total; the second rehearsal is
the 6/6-admitted, 20/20-denied run quoted here, and the hash chain verifies
across both.)

### 5.5 Where the contract is not yet honored

Stated as a list because a validator will build one anyway:

1. **Production persistence** (§4): the monthly join stores trimmed rows —
   no components blob, no seed, no config hash on the row (the config hash
   reaches only the telemetry sidecar). Full `ScoreRealization`s exist only
   on the evaluation path. Fix: persist the full realization JSON per
   promoted compound (~1.5 KB each). Scheduled, proposal §A7.
2. **The data coordinate is missing from realization identity.** Identity
   is (state, seed, algorithm version) — the paper's own data model calls
   scores derived artifacts of the *inputs*, yet no hash of the fitted
   signal-activation series or train-window bounds is stored. In the frozen
   evaluation regime this is harmless; in production the evidence store
   grows monthly, so the same structural state re-scored next month yields
   different numbers under the *same* realization id and `INSERT OR
   REPLACE` silently overwrites last month's row. No per-run score history
   survives, and the §5 reconstruction promise cannot be honored for any
   run except the latest. Fix: add a `data_hash` coordinate and keep
   realization history append-only. Scheduled, proposal §A7.
3. **Hash-coverage gaps** (§5.3): `interval_draws`, the interval floor, no
   config coordinate in `realization_id`, horizon coarse-hashing, slug
   churn (§5.2).
4. **A false attestation waiting for the first triple:** graduation's
   holdout-survival check applies only to pairs; a \(k \ge 3\) compound
   would bypass the check while its eligibility record hardcodes
   `"holdout_survival": true`. Unreachable today (the join emits pairs
   only) — which is exactly when such things should be fixed.
5. **Stale evidence surfaces:** the shipped Phase-1 report carries a config
   hash today's code cannot regenerate (§6.1 explains why that turned out
   to be a robustness fact), all shipped pair blobs predate the EVT layer,
   and freshly generated reports embed stale limitation strings (§3.5).
   Re-emission is scheduled together with the string fixes.
6. **Cross-platform determinism unverified** (§5.3).

---

## 6. Evaluation: pre-registered, then believed — with one conflict disclosed

The algorithm ships with its own kill criteria. The evaluation plan
([`evaluation-plan.md`](../redesignv1/evaluation-plan.md)) fixes the data
regimes, holdouts, baselines, thresholds, and falsification rules *before*
results are produced. Phase 1's decision regime is **R0**: a synthetic world
of 15 atoms, 3 planted channels, and 40 quarters, with train quarters
[0, 31] and a locked test window [32, 39]. Planted structure is ground
truth.

### 6.0 A pre-registration conflict, stated before the results

Two pre-registered documents disagree about what Phase 1 must pass, and v1
did not surface it. The evaluation plan's phase mapping (§9) requires Phase
1 to pass "R1 Task B vs B0/B1" — the *public-regime* ranking task. R1 Task B
**failed** (78 pairs, 4 holdout positives, lift 0.0 — §7.1). The roadmap's
§1.3 criterion reads "R1/R0 Task B: … lift ≥ 15% relative vs independence
baseline **or clear documented failure**" — a disjunction that any outcome
satisfies. The GO was declared under the roadmap criterion, with R0 as the
decision regime, after results were known. Documenting the R1 failure
honestly (which v1 did) is not the same as acknowledging that the two
pre-registered gates disagree and the lenient one was chosen ex post; this
paragraph is that acknowledgment. Under the stricter pre-registered reading,
Phase 1 did not pass.

The operational corollary deserves its own sentence: on the only real
regime, no pair co-occurs in the training window, so the deterministic
scorer's output is *identical* to the independence baseline (B3 ≡ B1,
verified to four decimals in the R1 artifact) — **the dependence layer this
paper calls "the actual product" has yet to influence a single production
number.**

### 6.1 Task B — pair co-occurrence ranking (the core product claim)

Can the scorer rank the pairs that will actually co-fire in the holdout above
those that will not? 105 pairs, 39 holdout positives, pre-registered
baselines:

| Baseline | AP | AUROC | P@20 |
|---|---|---|---|
| B0 random | 0.391 | 0.525 | 0.35 |
| B1 independence (product of marginals) | 0.398 | 0.521 | 0.25 |
| B2 shared-channel indicator¹ | 0.697 | 0.777 | 0.80 |
| **B3 deterministic batch scorer** | **0.734** | **0.761** | **0.90** |

¹ B2 carries an independence tie-break at weight \(10^{-6}\) so its ranking
is total — it is already minimally composite.

Pre-registered pass: AP lift over B1 ≥ 15% relative. Observed lift: **84.3%**
→ **PASS**. Three things the pass does *not* mean, stated here so a
validator does not have to discover them:

1. **The B1 bar was structurally near-unloseable on R0.** The generator's
   marginal firing is shock-dominated with near-homogeneous atom parameters
   (base quarterly rates 0.004–0.012 against channel-shock additions of
   0.55–0.75), so the product of marginals carries almost no ranking signal
   — B1 scores AP 0.398 against a random 0.391 — while the same planted
   channel shocks *create* the holdout labels. The 84% figure is therefore
   a function of the generator's parameters at least as much as of the
   scorer. It verifies the machinery; it is not a finding about the world.
2. **B2 is an oracle on this regime, not a deployable baseline.** Its
   channel tags are the planted ground truth — the answer key. The honest
   data-only margin the table supports is **B3 over B2: +5.2% AP (0.7335
   vs 0.6971) and P@20 0.90 vs 0.80, while B3 loses on AUROC (0.761 vs
   0.777)**. The metric disagreement is informative, not embarrassing: AP
   and P@20 concentrate on the top of the ranking — the operative loss
   function for a product that surfaces a top-20 — while AUROC integrates
   over the full ranking, where the shrinkage layer gives back some
   discrimination. Both facts are reported; preferring the top-weighted
   metrics is a product-shape argument, made explicitly rather than by
   omission.
3. **No metric carries a confidence interval.** Single frozen split, 105
   pairs, 39 positives; the B3−B2 AP gap is 0.036. Bootstrap CIs over
   pairs, multi-seed regeneration of R0, and a κ ∈ {1, 4, 16} sensitivity
   are all cheap, deterministic, and scheduled (proposal §A6); until they
   run, the gap should be read as "consistently positive on the top-weighted
   metrics, magnitude unresolved."

Two provenance disclosures. *Hyperparameter circularity:* `eb_strength = 4`
was selected using the Task A calibration harness **on R0**, and the GO is
measured on the same single-seed R0; κ = 4 is a bare code default with no
recorded selection procedure. One mitigating fact, found during this
revision's reproduction: the shipped Phase-1 report predates the config
fields added by hardening (including `eb_strength`), and re-running today
reproduces **every reported metric exactly** under the enlarged config —
i.e., the Task B result is invariant to the eb_strength introduction on
this dataset, so the circularity, while real in principle, was immaterial
here. *The missing baseline:* the table cannot isolate the shrinkage
layer's marginal value over "channel knowledge plus any tie-break"; a
composite baseline (graded channel weight × empirical count) is specified
and scheduled (proposal §A6).

### 6.2 Task C — planted structure recovery, audited

With the planted channel graph hidden, ranking pairs by train-window
co-occurrence lift recovers it: precision@30 = **0.767** (23 of 30 planted
edges; pre-registered threshold 0.70) → **PASS**. Four disclosures that v1
owed the reader:

1. **Attribution.** Recovery is performed by a harness utility ranking
   observed co-occurrence lift — not by the scorer's shrunk-joint layer.
   v1's abstract attributed it to "the dependence-aware scorer"; this one
   does not.
2. **The second pre-registered metric was unpassable as written.** The plan
   requires "planted high-risk pairs rank in top decile of U" — but 30
   planted pairs cannot rank inside an 11-slot top decile
   (⌈105/10⌉ = 11), so recall against the planted set caps at 0.37, and
   "high-risk" is defined nowhere. The harness implements a different
   quantity — the planted fraction *of* the top decile — and that is what
   the reported **0.909** (10/11, threshold 0.70) measures. The number is
   real; the criterion is a post-hoc reinterpretation of an unpassable
   pre-registration, and this paper now says so rather than presenting it
   as the original.
3. **Base rates.** Random selection of 30 from 105 pairs yields expected
   precision 0.286; random selection among the 54 pairs with *any* train
   co-occurrence yields 0.481. Against those floors, 0.767 is a real
   effect of moderate size — not the dramatic figure it reads as bare.
4. **The lift normalization is inert here, and the cut is tie-dominated.**
   Ranking by raw co-occurrence *count* under the same lexicographic
   tie-break reproduces precision 0.767 exactly — and under other
   tie-break conventions count-ranking spans roughly 0.63–0.80, sometimes
   beating the lift ranking, because only 21 pairs have two or more
   co-occurrences: nine of the thirty slots go to count-1 pairs chosen
   from 33 candidates by tie-break alone. Either way the conclusion
   stands: on this regime Task C certifies that planted structure
   produces countable co-occurrence, not that the lift normalization adds
   value. The lift ranking's own top-30 cut likewise falls inside a
   five-way tie (ranks 30–34 share lift 4.0) resolved by count and then
   id lexicography over ids that embed the planted channel names; that
   resolution moves at most one planted pair across the cut, and the pass
   margin (23 vs 21) does not depend on it — but a pre-registered test
   should not owe any margin to tie-break lexicography, and a
   score-jittered re-run is scheduled.

One structural note spanning §6.1–6.2: Task B's B3, Task C's precision, and
the top-decile fraction are three views of the same underlying train
co-occurrence statistic. v1 presented them as independent confirmations;
they are one confirmation viewed three ways.

### 6.3 Task A — atom frequency calibration (an honest FAIL, now fully honest)

The same harness grades the marginals. On R0 the strict rule **FAILs**, in
all three of its views — v1 quoted two and omitted the third:

- NLL: the model (76.34) loses to the pooled naive rate (72.47);
- 90% interval coverage: 0.967, above the [0.80, 0.95] band;
- **Brier (the harness's own sparse-regime fallback comparison): the model
  also loses, 0.18634 vs 0.18542** — omitted in v1;
- and the PIT histogram over-masses its top decile (0.233 vs 0.10 nominal)
  — the coverage pathology in distributional form, also omitted in v1.

The report states the two causes plainly: discrete intervals over-cover at
low rates (a regime property — the band is unreachable), and no per-atom
model beats pooling when true rates are near-homogeneous. Two fixes
followed, both now in the algorithm: empirical-Bayes shrinkage of atom rates
toward the pool (§3.1), and an R0′ count regime with a Poisson/NB family
choice.

**Correction of v1's false sentence.** v1 claimed NLL then "beats the naive
baseline on both regimes." It does not, and the artifact behind v1 said so.
The corrected record, now backed by an on-disk artifact generated for this
revision (the R0′ calibration run had no stored artifact when v1 shipped —
its one favorable calibration claim was the one with no stored evidence):

- **R0 (occurrence regime): strict FAIL.** NLL 76.34 vs 72.47 — the pooled
  naive rate still wins, consistent with near-homogeneous true rates;
  pooling is simply the right model there.
- **R0′ (count regime): strict PASS, on all views.** NLL 105.38 vs 135.81;
  Brier 0.1708 vs 0.1766; coverage 0.917 inside the band; the
  moment-matched family choice selects negative binomial for all 15
  overdispersed cells; the PIT top-decile mass is 0.15. (This is *stronger*
  than v1's hedge "coverage still documented as over-covering" — on the
  generated artifact R0′ coverage is in band.)

The FAIL is quoted because the white paper's claim is not "the algorithm is
right" but "the algorithm's failures are measured and priced in" — a claim
that survives only if the failures are quoted completely.

### 6.4 Ablations — reframed to what they actually show

| Ablation | v1's framing | What it actually shows |
|---|---|---|
| A1 remove dependence | "cleanest possible confirmation the layer is the product" | **A wiring check.** With `dependence_mode="independence"` the scorer computes literally B1's formula, so the collapse (AP 0.734 → 0.398, = B1 to three decimals) is a code identity that cannot fail; its AP drop is Task B's B3−B1 gap counted a second time. Valuable as a smoke test; not design evidence. The value evidence is §6.1's B3-over-B2 margin, with its caveats. |
| A3 remove EVT overlay | "tail quantiles less stable across seeds; consistent" | **A weak stability claim, honestly bounded.** q99 seed-CV 0.045 → 0.034 with EVT, over 3 seeds × ≤ 5 pairs, under a rule that fails only if EVT makes stability > 5% (relative) *worse* — and the with-EVT arm substitutes the empirical q99 whenever the overlay reports unstable. A3 shows the overlay does not hurt and modestly helps stability. It says nothing about tail accuracy (no loss-side task exists — §6.6). |

A5 (external features) stays gated: the FDR-disciplined feature registry
exists, but accepted `associational_feature`s require human/policy
acceptance before they may act as modifiers, so the ablation activates with
the modifier path.

### 6.5 External-context discipline

Because time-aligning "what else was happening" with sparse losses is a
machine for just-so stories, external features face a gauntlet before they
may influence a rating: pre-registration of feature families, a fixed
quarterly calendar, train-only association tests with block-bootstrap CIs, a
one-shot holdout direction check, Benjamini–Hochberg FDR at \(q = 0.10\),
minimum support of 8 quarters, and an **adversarial null pass** that must
write the best *non-causal* explanation. Survivors are labeled
`associational_feature` — modifier candidates, never causal evidence.

On the planted demo series the gauntlet's stages each did their job — by
*different* mechanisms v1 conflated: the strong planted feature was accepted
(rate ratios 14.4 and 12.0, q < 0.1); one noise feature
(`sanctions-regime-active`) reached the FDR stage and was rejected; the
other (`rate-hike-quarter`) — and the weak planted feature — were blocked by
the minimum-support gate and never reached FDR at all. Net claim (no noise
accepted) holds; the mechanism is now stated correctly.

### 6.6 What the harness does not measure

The most important evaluation facts are the missing instruments, so they get
their own section.

- **No compound-level calibration task exists.** Tasks A/B/C calibrate atom
  marginals, rank pairs, and recover structure; **nothing anywhere tests
  the level of \(\hat p(S)\) or its interval coverage.** This blind spot is
  load-bearing: within a zero-support stratum the §7.2 prior defect is a
  constant multiplicative markdown, invisible to every ranking metric by
  construction — the estimator's level bias sits precisely where the
  evaluation has no instrument. On R0 the generative truth \(q_S\) is
  computable for every pair, so the instrument is cheap: interval coverage
  over the 105 pairs plus level bias stratified by expected co-occurrence
  under independence *and* by (rare × supported). Scheduled, proposal §A6;
  the mixture challenger of §7.2 promotes only if it wins on this task.
- **Half the output vector has no evaluation at all.** \(L \mid S\) is
  reported as four quantiles and a mean; no task anywhere scores loss
  quantiles against anything (A3 measures seed-*stability*, not accuracy).
  The truth pack stores every atom's generative severity parameters, so a
  severity-calibration task is cheap and scheduled (proposal §A6). Until it
  runs: **occurrence is evaluated; severity is reported unvalidated** — and
  on the production regime the persisted loss numbers are additionally
  unit-polluted placeholders (§4).
- **No metric uncertainty** (§6.1): single frozen split, no bootstrap, no
  multi-seed replication, no κ sensitivity. Scheduled, proposal §A6.
- **Task D (expert resolution) has never run.** The only judgment-adjacent
  evidence is a properly-labeled *simulated*-panel FAIL, which does not
  count in either direction; the "workshop material" framing of §3.7
  therefore rests on an untested instrument.

### 6.7 The GO decision, restated with its actual scope

The harness's combined recommendation on R0 is **GO**, and this revision
does not retract it — it scopes it. GO licenses: continued research-tier
operation behind the §5.4 boundary, the challenger and instrument work of
the companion proposal, and workshop use of scored realizations under the
§2 language discipline. GO does **not** assert: a pass under the evaluation
plan's stricter Phase-1 mapping (it fails there — §6.0); calibrated
\(\hat p\) levels (no instrument — §6.6, with a known level defect sitting
in the blind spot — §7.2); validated severity (§6.6); or any production
dependence signal (none exists — §6.0). (The Phase 2 multiverse search loop
passed its own pre-registered batch-vs-loop test and was removed from
mainline on 2026-08-01: at live-data scale batch scoring does everything
production needs. The rating algorithm is unaffected — batch is the
production path.)

---

## 7. Limitations, stated before the conclusion

### 7.1 Regime limits

- **R0 is synthetic.** Passing on planted structure proves the machinery
  recovers structure, not that the world has that structure — and §6.1
  shows the headline lift is partly a generator property. On the public
  regime (R1) Task B itself **FAILs** — 78 scored pairs, 4 holdout
  positives, lift 0.0 — and because no pair co-occurs in the R1 training
  window, **B3 ≡ B1 there: production scores currently carry zero
  empirical dependence support** (§6.0). That is exactly why R0 is the
  decision regime and why the production join gates on data sufficiency
  rather than scoring anyway. Absolute calibration is not established beyond
  the shipped synthetic and sparse public-demo regimes.
- **Compounds are rare by definition**, so holdout positives are few and
  metrics are noisy; the harness counters with proxy tasks and synthetic
  injection, not with bigger claims — and, since v1, with the explicit
  §6.6 list of instruments it still lacks.

### 7.2 Known defects in the shipped estimator (found in review)

- **The dependence prior cannot hold its center for rare pairs.** The
  prior's strength is denominated in quarters (κ = 4), not expected
  co-events, so every zero-support pair receives the same constant markdown
  \(\kappa/(T+\kappa) \approx 0.11\) on its independence product. For
  common pairs that markdown is evidence (at \(q_i = 0.30\), independence
  expected ≈ 2.9 co-fires in 32 quarters; observing zero is informative).
  For rare pairs it is a prior artifact (at \(q_i = 0.05\), independence
  predicts zero co-fires with probability 0.92; observing zero proves
  nothing) — the technical cause being that with \(\alpha = \kappa\,
  q_{\mathrm{ind}} \ll 1\) the Beta prior is L-shaped and "centred on
  independence" in mean only. Consequence, plainly: **rare, severe,
  never-yet-co-observed combinations — the product's stated reason to
  exist — are priced roughly nine times below the independence baseline
  that §1 already calls too low.** Task B cannot see it (constant factors
  within a stratum preserve rankings) and no level-calibration task exists
  (§6.6): the defect sits exactly in the evaluation's blind spot, which is
  why both fixes ship together — the spike-and-slab mixture challenger
  that treats zeros as uninformative for rare pairs while keeping the
  markdown where zeros are informative (proposal §A1; the spec's §4.3
  common-factor model subsumes it when data allows, §A2), promoted only if
  it wins on the new §6.6 calibration task.
- **In 15 of the 105 shipped audit blobs, \(\hat p\) lies outside its own
  90% interval** — worst row \(3.1\times10^{-4}\) against
  \([0, 4.7\times10^{-11}]\), nearly seven orders of magnitude apart.
  Mechanisms in §3.3. A further defect in the same construction: the
  interval conditions on the marginal point estimates \(q_i\) as if known,
  so it understates total uncertainty most for thin-history atoms. Fixes
  scheduled (proposal §A3): clip each draw, report the posterior median
  alongside the mean, flag violating rows, two-level sampling so marginal
  uncertainty propagates; the §A1 reparameterization removes the
  \(\alpha \ll 1\) regime at the root. Until then a validator
  opening the audit blobs meets a point outside its interval in 14% of
  rows — the blob is honest about it, and now so is the paper.
- **Atom calibration remains the weak layer** on the occurrence regime
  (§6.3): compound *ranking* is the product claim; absolute \(\hat p(S)\)
  values are research quantities with calibration tracking, not validated
  probabilities.

### 7.3 Structural blind spots

- **Only same-quarter co-occurrence is visible.** The occurrence
  definition, the estimator, and the labels all use the same
  single-quarter window, so a lagged cascade — control failure in Q1
  causing third-party failure in Q2 — contributes nothing to the support,
  the labels, or the score. Realistic transmission narratives (§2 stores
  them) are mostly lagged. Scheduled sensitivity: windowed co-firing
  (w ∈ {1, 2} quarters), documented like the k-of-n sensitivity; a
  Hawkes-style cross-excitation model is the higher rung (proposal §A4).
- **Emerging risks are where this engine is weakest — by construction.**
  Three choices compound: EB shrinkage pulls a short-history atom toward
  the pooled *historical* rate (for a genuinely novel risk, the wrong
  reference class); a new atom has no co-occurrence support, so §7.2's
  below-independence markdown applies to its every pair; and its severity
  source is `pooled_prior`. Net: the newer the risk, the more its score is
  an artifact of priors chosen for statistical convenience rather than
  relevance — and this product's charter is *emerging* risks. The
  deterministic engine has no mechanism to price a novel compound above
  its absent history. This is the honest limit of the data path, not a
  bug; v1's omission of it was the bug. It is the single strongest
  argument for activating the judgment channel (layers 6–7) the
  architecture already reserves (proposal Part B, esp. §P3).
- **Atom identity is string-fragile, and identity error masquerades as
  dependence.** Risk identity is exact/slugified string matching end to
  end; two records naming the same underlying risk differently become two
  atoms, and any shared evidence then registers as co-occurrence —
  manufacturing the exact quantity this product sells. Semantic
  identity hygiene with human-gated merge is proposal work (§P8).
- **The dependence estimator is pairwise-empirical.** With sparse data it
  is a shrunk co-occurrence rate, not a causal claim; copulas and expert
  DAGs are specified higher rungs and are not yet fit. For \(k \ge 3\) the
  estimator uses only the k-way count and the full independence product —
  the better-supported pairwise joints are computed but unused (§7.4).

### 7.4 Aggregation simplifications (roadmap tier)

Severity dependence between atoms is not modeled (independent severities,
\(\alpha_{\mathrm{overlap}} = 1.0\)); event multiplicity within a quarter is
discarded where it matters most — the loss (one severity draw per atom per
hit quarter; a compound-Poisson quarter model would use counts the pipeline
already stores); the severity blend has a discontinuity at \(n = 30\) and
blends \(\sigma\) linearly; the EVT overlay inherits the lognormal ceiling
(§3.5) pending data-side family selection; and k-way scoring ignores
pairwise information (a sharper Fréchet clip from pairwise joints is the
cheap fix). Tracking status, stated precisely: the count-aware MC and
k-way items are in the proposal (§A5, §A2); the severity-blend items and
data-side family selection are prescribed at spec level (algorithm-spec
§3.2) but scheduled nowhere yet — this list is their record.

### 7.5 Contract gaps

§5.5 is part of the limitations and is not repeated here.

---

## 8. Conclusion

The contribution of this work is not a new estimator — every component is a
method model validation already trusts (§1.1). It is the **assembly
discipline**: an explicit occurrence definition, shrinkage priors centered
on the skeptic's null, a tail overlay that can fail its own stability test,
an audit blob from which every digit is reconstructable, and a
pre-registered harness that has already recorded FAILs — including against
itself — and priced them into the design.

This revision is that discipline applied to the paper's own headlines. The
honest results are smaller than v1's and stated without protection: on a
synthetic regime whose generator makes the naive baseline near-chance, the
data path modestly out-ranks an oracle baseline on the top-weighted metrics
(+5.2% AP, 0.90 vs 0.80 P@20) and loses to it on AUROC; structure recovery
demonstrates that planted co-occurrence is countable, against a 48%
informed floor; atom calibration passes on the count regime and fails on
the occurrence regime; severity and score *levels* are so far unvalidated
(§6.6); the shipped estimator undershoots independence for rare unobserved
pairs (§7.2); and on the only real regime the dependence layer has yet to
touch a production number (§6.0). v1's concluding claim — "dependence-aware
because independence is demonstrably wrong in the tail" — is retired until
the level-calibration instrument exists and an estimator that holds the
skeptic's null for rare pairs ships; both are specified in the companion
proposal, with promotion gates this harness controls.

What survives unchanged is the posture: deterministic because scores must
be reproducible, humble because the falsification criteria for retiring the
algorithm are already written down — and honest in the only way that
compounds: including where it hurts.

---

## Appendix A — Default configuration

| Parameter | Default | In config hash | Meaning |
|---|---|---|---|
| `horizon_quarters` | 4 | yes | rating horizon (1 year) — note §5.2: hashed coarsened to whole years |
| `mc_draws` | 20,000 | yes | Monte Carlo draws per scenario |
| `shrink_kappa` | 4.0 | yes | dependence pseudo-count (quarters), prior centred on independence in mean (§3.3, §7.2) |
| `n_min` | 30 | yes | min events for a parametric severity claim |
| `pair_support_min` | 3 | yes | min co-firing quarters for `statistical` class |
| `marginal_smoothing` | 0.5 | yes | add-half smoothing on occurrence (the `eb_strength = 0` fallback; missing from v1's table) |
| `eb_strength` | 4.0 | yes | empirical-Bayes shrinkage of atom rates (pseudo-quarters) |
| `interval_draws` | 4,000 | **no** (§5.3) | Beta posterior draws for the \(\hat p\) interval |
| `evt_enabled` | true | yes | Layer 5 on/off |
| `evt_min_hits` | 200 | yes | min positive MC hits before the EVT overlay runs |
| `evt_threshold_quantile` | 0.90 | yes | POT threshold; sensitivity refits at 0.85 / 0.95 (can degrade to one-sided — §3.5) |
| `seed` | 20260801 | no (stored per realization) | base RNG seed (per-scenario seeds derive from the state hash) |
| `dependence_mode` | `shrunk_empirical` | yes | `independence` activates ablation A1 |
| interval pseudo-count floor | \(10^{-6}\) | **no** — hardcoded (§5.3) | Beta degeneracy guard |

## Appendix B — Reproduction

All evaluation figures in this paper were re-derived from fresh runs for
this revision; the commands and their expected outcomes:

```bash
# R0 generate + Task B + Task C (writes eval_report.{json,md})
atlas-eval demo
#   expected: AP lift 0.8434 (PASS), Task C precision 0.7667 /
#   top-decile fraction 0.9091 (PASS), recommendation GO

# Task A calibration on R0 — exits 1: the strict FAIL is the expected result
atlas-eval calibrate --risks generated/redesign/phase1/r0/r0_risks.json \
                     --events generated/redesign/phase1/r0/r0_events.jsonl
#   expected: NLL 76.3384 vs naive 72.4738 (loses), Brier 0.1863 vs 0.1854
#   (loses), coverage 0.9667 vs [0.80, 0.95] (over-covers)

# R0' count regime: regenerate the dataset, then calibrate — exits 0 (PASS)
python -c "
import json; from pathlib import Path
from contingency_atlas.scoring.synthetic import R0Config, generate_r0
out = Path('generated/redesign/phase3/calibration-r0prime'); out.mkdir(parents=True, exist_ok=True)
ds = generate_r0(R0Config(count_model='poisson_counts'))
(out/'r0prime_risks.json').write_text(json.dumps([a.to_dict() for a in ds.atoms], indent=2, sort_keys=True))
with (out/'r0prime_events.jsonl').open('w') as f:
    [f.write(json.dumps(e.to_dict(), sort_keys=True)+'\n') for e in ds.events]
(out/'r0prime_truth.json').write_text(json.dumps(ds.truth, indent=2, sort_keys=True, default=str))
"
atlas-eval calibrate --risks generated/redesign/phase3/calibration-r0prime/r0prime_risks.json \
                     --events generated/redesign/phase3/calibration-r0prime/r0prime_events.jsonl \
                     --out generated/redesign/phase3/calibration-r0prime
#   expected: NLL 105.3765 vs naive 135.805 (wins), Brier 0.1708 vs 0.1766
#   (wins), coverage 0.9167 in band, NB family for 15/15 atoms

atlas-eval ablations --risks ... --events ... --truth ...   # pre-registered ablation matrix
python -m contingency_atlas.compound_join --force           # production monthly join
```

Two caveats a reproducing validator must know (both §5.5):

1. **The config hash will not match the shipped artifact.** A fresh run
   reproduces every reported metric exactly but emits
   `model_config_hash 6219ecd3…` where the shipped Phase-1 report says
   `0e76a450…`: the shipped report predates the hardening additions to the
   config surface (`eb_strength`, `dependence_mode`, EVT fields) —
   removing exactly those keys from today's surface reproduces the shipped
   hash. The metrics' invariance across that config change is a robustness
   fact (§6.1); the hash mismatch is evidence-pack staleness, and
   re-emission is scheduled.
2. **Freshly generated reports embed stale limitation strings** ("EVT tail
   layer is a stub"; "R1 Task B deferred") that contradict §3.5 and §6.0
   of this paper; the strings predate the features. Fix scheduled with the
   re-emission.

Determinism scope: identical bytes verified on the reference platform
(macOS/CPython); cross-platform bit-identity is untested (§5.3).

## Appendix C — Evidence map: working papers, artifacts, code

Every claim in this paper resolves to one of three kinds of evidence: the
pre-registered working papers that fixed the rules and the code that
produced the outcomes — both in the repository tree, though at revision
time the `docs/redesign` → `docs/redesignv1` spec-pack rename and this
whitepaper directory are **uncommitted**, so a checkout of HEAD predates
both — and the generated evidence artifacts, which are never versioned:
`.gitignore` excludes `generated/` entirely, so **a fresh clone contains no
evidence pack** until the Appendix B commands regenerate it
(deterministically — verified again for this revision; the R0 figures
reproduce exactly). v1's claim that all three
kinds were "in this repository" was wrong in the way that matters to a
validator's checkout. The standing options are to commit a frozen evidence
pack with hashes or to treat this appendix plus Appendix B as the contract:
*reproducible* is not *archived*, and the reader deserves the distinction
stated. The R0′ calibration artifact cited in §6.3 — absent entirely when
v1 shipped — now exists at the path below.

### C.1 Working papers (specification pack — `docs/redesignv1/`)

- `algorithm-spec.md` — normative layer stack, elicitation and fusion protocols, falsification
- `evaluation-plan.md` — pre-registered tasks A–F, baselines, thresholds, abandon criteria (§6.0 documents its Phase-1 conflict with the roadmap)
- `data-model.md` — schemas, structural state hash, lineage rules (§5.5 item 2 documents the missing data coordinate)
- `architecture.md` — components, descriptive data classes, tech choices
- `loop-design.md` — candidate generation (channel-blocked expansion); the multiverse loop itself was removed from mainline on 2026-08-01
- `telemetry-spec.md` — cost/token instrumentation contract
- `risks-and-objections.md` — the honest case against the system
- `roadmap.md` — phase plan and decision log (§1.3 is the lenient GO criterion of §6.0; the decision log's "both regimes" NLL entry is corrected by §6.3 and a dated correction row)
- `model-inventory.yaml` — research-tier registration, restrictions, evidence links
- `pitch.md`, `visualization-spec.md` — context documents

### C.2 Primary evaluation evidence (`generated/redesign/` — regenerable, not versioned)

Phase 1 — R0 go/no-go (`phase1/`):

- `r0/eval_report.md` / `.json` — Task B and Task C results behind §6.1–6.2 (recommendation GO; carries the stale config hash and limitation strings per Appendix B)
- `r0/r0_risks.json`, `r0/r0_events.jsonl`, `r0/r0_truth.json` — the synthetic regime and its planted ground truth (including the generative severity parameters §6.6's scheduled loss task will score against)
- `pairs/score_realizations.jsonl` — one full audit blob (§5.1) per scored pair; **predates the EVT layer** (`evt: null` in all 105 rows) and contains the 15 interval-violation rows of §7.2 — kept as-is until the §7.2 fixes land, because the blobs are the evidence for the finding
- `pairs/run_report.json`, `pairs/telemetry.jsonl` — cost rollup ($0 on the deterministic path) and per-pair telemetry

Phase 3 — harness and boundary runs (`phase3/`):

- `calibration/calibration_report.md` / `.json` — Task A on R0 behind §6.3 (strict FAIL on all three views, with cause analysis)
- `calibration-r0prime/` — **generated for this revision**: the R0′ dataset (`r0prime_risks.json`, `r0prime_events.jsonl`, `r0prime_truth.json`) and its calibration report (strict PASS; §6.3)
- `ablations/ablations.md` / `.json` — ablation matrix behind §6.4 (A1 and A3 runnable; A2/A4/A6 historical rows)
- `r1/r1_eval.md` / `.json` — public-regime Task B: FAIL on sparse data (78 pairs, 4 holdout positives, lift 0.0; B3 ≡ B1 — §6.0); records the regime boundary cited in §7.1
- `synthetic-cell-demo/` — fictional scoring-cell exercise: `eval_report.md` / `.json` (Task B and C pass), `aggregation_report.json` (65/68 rows accepted, sparse groups suppressed), `egress_audit.jsonl` (aggregate features admitted, sampled local-detail event rows denied), `scoring_features.jsonl`, `synthetic_scoring_events.jsonl`, and `synthetic_events.csv`
- `ext-features/ext_report.md` / `.json`, `ext_registry.yaml` — the §6.5 screen on a planted series: strong planted feature accepted (RR 14.4 and 12.0, q < 0.1); one noise feature FDR-rejected, the other — and the weak planted feature — blocked by the min-support gate before testing
- `workshop/` — example human-facing scenario packs produced from scored realizations

### C.3 Implementation and tests

- `contingency_atlas/scoring/scorer.py` — Layers 1–4 (frequency, severity, dependence, Monte Carlo; module docstring carries the stale EVT line noted in §3.5)
- `contingency_atlas/scoring/evt.py` — Layer 5 (GPD overlay with threshold-sensitivity falsification)
- `contingency_atlas/scoring/calibration.py`, `evaluate.py`, `ablations.py` — the harness that produced C.2
- `contingency_atlas/scoring/ext_features.py` — the §6.5 FDR screen (imports PyYAML — outside the score path, §5.3)
- `contingency_atlas/scoring/egress.py` — local-to-external gateway with hash-chained audit log
- `contingency_atlas/compound_join.py`, `compound_graduate.py` — production monthly join and graduation (§4; trimmed persistence per §5.5)
- `tests/test_scoring_*.py` — unit and integration coverage, including egress red-team tests, the runnable ablations, and the hardening tests that assert the R0′-only NLL win §6.3 now states

## Appendix D — Boundary map: where LLMs touch the wider system

The compound scorer (layers 1–5) is LLM-free — verified, and the claim is
scoped to the scorer. A validator auditing the surrounding system will meet
model output in five places, listed here so the discovery costs nothing:

| # | Touchpoint | Nature | Guard |
|---|---|---|---|
| 1 | Compound scoring (layers 1–5) | none — deterministic | this paper |
| 2 | Channel tags feeding candidate generation (§4) | keyword-derived, deterministic; but they are the *only* production candidate source while empirical edges stay off | §4; coverage via §P1 enrichment; empirical-edge activation itself unscheduled |
| 3 | Graduation gate | an LLM judge's clamped numeric score decides which compounds enter the scenario library — a model-authored number gating publication, though never a score field | threshold + provenance tag; boundary documented here |
| 4 | Graduated prose | LLM-written narrative fields may contain numbers (the rationale prompt cites dependence support); kept out of score fields by **instruction, not mechanism** | review flag only; no mechanical check scheduled |
| 5 | Legacy urgency scores (outside compound scoring) | parsed largely from LLM-authored text (likelihood words, dollar strings, foresight vocabulary); graduated compounds currently receive arbitrary fallback constants there (§4) | exclusion/deterministic mapping scheduled §A7 |

Layers 6–7 (judgment/pooling) would be a sixth touchpoint; they are empty
in v0.1.0 (§3.6) and their activation gates are the subject of the
companion proposal.

---

### References (§1.1)

Benjamini, Y. & Hochberg, Y. (1995). Controlling the false discovery rate. *JRSS-B* 57(1).
Böcker, K. & Klüppelberg, C. (2008). Modelling and measuring multivariate operational risk with Lévy copulas. *Journal of Operational Risk* 3(2).
Cooke, R. M. (1991). *Experts in Uncertainty*. Oxford University Press.
Cravino, N. *AI Agents in Cybersecurity*. Manuscript in preparation (cited in the executive summary as companion practitioner reading on simulation-driven defense and AI-agent guardrails).
Efron, B. & Morris, C. (1975). Data analysis using Stein's estimator and its generalizations. *JASA* 70(350).
Embrechts, P., Klüppelberg, C. & Mikosch, T. (1997). *Modelling Extremal Events*. Springer.
Embrechts, P., McNeil, A. & Straumann, D. (2002). Correlation and dependence in risk management: properties and pitfalls. In *Risk Management: Value at Risk and Beyond*. Cambridge University Press.
Federal Reserve / OCC (2011). *Supervisory Guidance on Model Risk Management* (SR 11-7 / OCC 2011-12).
Frachot, A., Georges, P. & Roncalli, T. (2001). Loss Distribution Approach for operational risk. Working paper, Crédit Lyonnais.
Genest, C. & Zidek, J. V. (1986). Combining probability distributions: a critique and an annotated bibliography. *Statistical Science* 1(1).
George, E. I. & McCulloch, R. E. (1993). Variable selection via Gibbs sampling. *JASA* 88(423).
Hosking, J. R. M. & Wallis, J. R. (1987). Parameter and quantile estimation for the generalized Pareto distribution. *Technometrics* 29(3).
McNeil, A., Frey, R. & Embrechts, P. (2015). *Quantitative Risk Management* (rev. ed.). Princeton University Press.
Mitchell, T. J. & Beauchamp, J. J. (1988). Bayesian variable selection in linear regression. *JASA* 83(404).
Moscadelli, M. (2004). The modelling of operational risk: experience with the analysis of the data collected by the Basel Committee. Banca d'Italia, Temi di discussione 517.
Nešlehová, J., Embrechts, P. & Chavez-Demoulin, V. (2006). Infinite-mean models and the LDA for operational risk. *Journal of Operational Risk* 1(1).
Nosek, B. A., Ebersole, C. R., DeHaven, A. C. & Mellor, D. T. (2018). The preregistration revolution. *PNAS* 115(11).
O'Hagan, A. et al. (2006). *Uncertain Judgements: Eliciting Experts' Probabilities*. Wiley.
