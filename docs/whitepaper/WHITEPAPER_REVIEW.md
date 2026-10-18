# Review of "Rating Compound Operational-Risk Scenarios" (WHITEPAPER.md)

**Reviewer:** full-repo review — paper cross-checked against
`contingency_atlas/`, `docs/redesignv1/`, `docs/technical/`, the on-disk
`generated/redesign/` evidence pack (note: gitignored — see D10), and the
test suite. Every checkable number in the paper was verified against those
artifacts; the calibration harness and the full `atlas-eval demo` were
re-run to confirm the findings. Findings were then adversarially
re-verified by independent passes; all survived. Companion document:
[`HYBRID_SCORING_PROPOSAL.md`](HYBRID_SCORING_PROPOSAL.md)
(algorithm improvements + the LLM-hybrid design the review recommends).

**Verdict: major revision (as a paper); targeted fixes (as an
algorithm).** The paper is unusually honest for its genre — it quotes its
own FAILs, ships kill criteria, and the numbers are real: of the ~24
numeric claims checked, all match the artifacts except one (D1). But the
revision required is more than cosmetic, because both §6 headline results
are softer than presented: the 84% Task B lift is measured against a
baseline the synthetic generator forces to near-chance while the strong
baseline is a ground-truth oracle (E1), and Task C's second metric is a
post-hoc reinterpretation of an unpassable pre-registered criterion, with
no base-rate context (E6). Add two factually incorrect sentences (D1,
D6), a statistical pathology in the dependence estimator that the
evaluation design cannot see (T1), audit-blob rows where p̂ sits outside
its own interval (T2), an identity layer that cannot distinguish
realizations that differ (D12), a production path that does not honor the
§5 contract (D2/D8), and a pre-registration conflict on the GO decision
itself (E8). None of it is unfixable, and much of the underlying work is
genuinely good — but §5 and §6 need rewriting, not touch-ups, before a
model-validation audience sees this.

---

## 1. What the paper gets right (and should keep)

- **The verification holds.** Task B table, Task C, ablations A1/A3, R1
  failure numbers, synthetic-cell aggregation/egress counts, ext-features RR values,
  config defaults, EVT rules, formulas, hashes, CLI commands: all match
  code and artifacts (details in §2). (One caveat on A1's *meaning* — see
  E5: the ablated scorer is definitionally B1's formula, so the collapse
  verifies wiring, not value; the value evidence is the Task B gap itself.)
- **Appendix B reproduction was performed for this review and passes**:
  a fresh `atlas-eval demo` into a scratch directory reproduced every
  Task B and Task C metric exactly (AP lift 0.8434, precision 0.7667,
  top-decile 0.9091, recommendation GO). One wrinkle became D9 below.
- **Quoting the Task A FAIL (§6.3) and the R1 FAIL (§7) is the paper's
  best feature.** Same for pre-registered thresholds and the §9-style kill
  sentence culture inherited from `risks-and-objections.md`.
- **The auditability machinery in the evaluation path is real**:
  `pairs/score_realizations.jsonl` contains full component blobs; the
  egress audit chain verifies; the state-hash discipline is implemented as
  documented.
- **Language discipline** ("research quantity", "not a capital parameter",
  the DISCLAIMER block) is consistent throughout and consistent with
  `model-inventory.yaml`.

---

## 2. Factual discrepancies (paper vs. repo)

### D1. §6.3 — "NLL beats the naive baseline on both regimes" is false. [must-fix]

The paper: *"an R0′ count regime with a Poisson/NB family choice on which
NLL beats the naive baseline on both regimes."*

The artifact (`generated/redesign/phase3/calibration/calibration_report.json`)
records `nll_model 76.3384` vs `nll_naive 72.4738`, `nll_beats_naive: false`
on R0. A fresh deterministic re-run reproduces it: NLL beats naive **only on
R0′** (105.377 vs 135.805); on R0 the model still loses. The test suite agrees
(`tests/test_scoring_hardening.py` asserts the win for R0′ only). The same
overstatement appears in `docs/redesignv1/roadmap.md:161`. The paper is even
internally inconsistent: the same paragraph quotes the R0 NLL loss.

Fix: "…on which NLL beats the naive baseline on the count regime (R0′);
on R0 the pooled naive rate still wins, consistent with near-homogeneous
true rates." That is a *better* sentence for this paper — it is another
honest boundary, which is the paper's brand.

Two adjacent omissions in the same section: the artifact also records
`brier_beats_naive: false` (0.18634 vs 0.18542) — the sparse-regime
fallback comparison the calibration code itself defines also fails — and
the PIT histogram spikes at 0.233 in the top decile; §6.3's "states the
two causes plainly" analysis mentions neither. And no R0′ calibration
artifact exists on disk at all (see D10), so the one calibration claim
that *favors* the algorithm is the one with no stored evidence.

### D2. §4 / §5.1 — the production path does not persist what the paper says it persists. [must-fix]

The paper: *"persists the top 20 with their full score vectors"* (§4) and
*"Every rated scenario persists a `ScoreRealization` (JSON)"* (§5.1).

`compound_join.py:232-299` persists a **trimmed row**: p̂, interval,
q50/q95 (DB) or all quantiles (artifact), mean, uncertainty class,
dependence support, evidence support. It does **not** persist the
components blob (per-atom fits, dependence block with κ and both joints,
MC block, EVT) or the seed anywhere; the config hash reaches only the
telemetry sidecar (`compound_join.py:223` →
`generated/telemetry/compound-join.jsonl`), not the compound row or
artifact. Full `ScoreRealization`s exist only on the evaluation-harness
path (`atlas-score pairs`). The §5 contract — "model validation must be
able to reconstruct any number from stored artifacts" — is currently
honored by the eval path and *not* by the monthly production join. See
also D8: the inputs are not snapshotted either, so recomputation after
the evidence store grows cannot reproduce last month's number.

Fix: either persist the full realization JSON per promoted compound (it is
~1.5 KB), or scope the §5.1 claim to the evaluation path explicitly.

### D3. §3.6 — present tense for machinery that does not exist in mainline. [should-fix]

*"Where independent judgment sources combine, a logarithmic opinion pool
**is used**…"* No opinion-pool, elicitation, or judgment-fusion code exists
in the tree; `judgment.py`, `expert_panel.py`, `panel_sim.py` were deleted
in the 2026-08-01 loop removal (roadmap decision log; `model-inventory.yaml`
says `judgment_channel: none`). The *fields* exist and are empty — that part
of §3.6 is accurate. The pooling and the seven-step elicitation protocol are
specification (`algorithm-spec.md` §7-8), not implementation.

Fix: "the spec prescribes a logarithmic opinion pool… these channels are
schema-present and empty; no elicitation machinery ships in v0.1.0."
Same tense problem one section earlier: §3.1's ELD-proxy /
`frequency_source = elicited` mechanism is spec, not code — no such tags
are produced anywhere in the tree.

### D4. §2 — k-of-n "admitted only as a documented sensitivity" is not runnable. [should-fix]

No k-of-n scoring logic exists anywhere; only an unused `k` slot in
`scenario_state_hash` (`models.py:288`). As written, a validator will ask
to see the sensitivity and there is nothing to run. Say "specified, not yet
implemented."

### D5. §3.5 vs. shipped artifacts — no stored realization exhibits an EVT overlay, and fresh reports still call EVT a stub. [should-fix]

All 105 shipped `score_realizations.jsonl` records have `evt: null` (the
artifact predates the EVT layer — 56 of those records have
`hit_count ≥ 200` and would carry overlays under current code). Worse,
`evaluate.py:275` still hardcodes
*"EVT tail layer is a stub (evt: null)"* (and a stale "R1 Task B deferred"
line) into the `limitations` of **every freshly generated eval report** —
so a reader who runs `atlas-eval demo` today gets a report contradicting
§3.5/§6.4 of the paper. `scorer.py:15` module docstring says the same stale
thing ("Layer 5 (EVT): stub only") directly above the code that implements
it. Three one-line fixes: refresh the limitation strings, refresh the
docstring, re-emit the pairs artifact so at least one stored blob shows a
real overlay.

### D6. §3.3 — the interpretation sentence misdescribes the estimator's own limiting behavior. [must-fix, and see T1]

*"with no observed co-occurrence the estimate decays smoothly **to the
independence product** as data accumulates."*

The opposite is true. With `N_S = 0`, `q_S = κ·q_ind/(T+κ)`: it **equals**
independence at `T = 0` and decays **below** independence toward zero as
`T` grows. At the paper's own defaults (T = 32, κ = 4) every
zero-co-occurrence pair is priced at `4/36 ≈ 11%` of its independence
probability. Whether that behavior is even desirable is finding T1; the
sentence as written is simply not what the formula does.

### D7. Minor wording/nuance items. [nits]

- Appendix C.2's "noise features FDR-rejected": one of the two planted
  noise features (`rate-hike-quarter`) was blocked by the min-support gate
  (4 < 8 quarters) and never reached FDR; only `sanctions-regime-active`
  was FDR-rejected (q = 0.788). Net claim (no noise accepted) holds; the
  mechanism stated is half right. (Related nuance: the synthetic-cell egress
  audit log holds 46 cumulative rows from two rehearsals — the second
  logged the paper's 6/6 and 20/20 exactly, the first logged 0 allows /
  20 denies — so a validator counting raw lines gets 40 denials and 46
  rows and should be told why.)
- §5.3 "the same inputs and config reproduce the same bytes on any
  machine": `math.log`/`exp`/`lognormvariate` route through platform libm;
  cross-platform bit-identity is very likely but unproven and untested. Add
  a cross-platform reproduction test to CI, or say "same bytes for a given
  platform; cross-platform reproduction verified for the reference
  artifacts."
- §5.2 lists the state-hash inputs incompletely — code also hashes `k`,
  `transmission_id`, `scope`, `assumption_hash` (`models.py:284-317`).
  Superset, not contradiction; worth one clause.
- "Standard-library only" holds for the scorer; note `ext_features.py`
  imports PyYAML (registry serialization, outside the score path).
- §2 defines `L | S` as "the conditional loss severity distribution"; the
  code computes the **annual aggregate** loss summed over hit quarters,
  conditional on ≥ 1 hit quarter (`scorer.py:304-315`). Per-occurrence vs.
  annual-aggregate should be stated; they differ whenever more than one
  quarter fires.
- Abstract: "the dependence-aware scorer… recovers planted dependence
  structure at 77% precision" — Task C recovery is done by co-occurrence
  *lift* ranking (`recover_empirical_edges`), a harness utility, not by the
  scorer's shrunk-joint layer. Attribute it to the harness.

### D8. Realization identity is missing the data coordinate. [should-fix — spec-level]

`realization_id = sha256(state | seed | algorithm_version)` and
`compound_id = f"scn-{state_hash[:12]}"` is the `compound_scenarios`
primary key with `INSERT OR REPLACE`. In the eval harness the dataset is
frozen, so this is fine. In production the evidence store **grows between
monthly runs**: the same structural state re-scored next month produces
different numbers under the *same* realization_id and silently overwrites
the previous row (and `compound_scenarios.json`). No per-run score history
survives, and no snapshot/hash of the fitted input series is stored. Both
`data-model.md` ("scores are derived artifacts of *(state,
algorithm_version, seed)*") and the paper omit the fourth coordinate:
**the data snapshot**. Add a `data_hash` (hash of the signal-activation
series + train-window bounds) to the realization identity and keep
realization history append-only. Without it, §5's reconstruction promise
cannot be honored for any run except the latest.

### D9. The shipped GO artifact carries a config hash today's code cannot regenerate. [should-fix]

Reproducing `atlas-eval demo` yields metrics identical to
`generated/redesign/phase1/r0/eval_report.json` in every reported figure —
but a different `model_config_hash` (`6219ecd3…` fresh vs `0e76a450…`
shipped). The shipped Phase-1 report predates the hardening additions
to the config surface (`eb_strength`, `dependence_mode`, and the EVT
fields entered `ScorerConfig.config_hash()` later — removing exactly
those keys from today's surface reproduces the shipped hash); the
reported rank metrics happen to
be invariant to those changes on this dataset. So the paper's §5.3 story
("every realization stores the config-hash") is true, but the shipped
evidence pack is hash-stale relative to the shipped code: a validator
re-running Appendix B gets the right numbers with a non-matching hash and
must reconstruct this explanation alone. Fix: re-emit the R0 artifacts
(and the `pairs/` blobs, per D5) from current code, and note in §6.1 that
the GO metrics are reproduction-invariant across the hardening change —
a robustness fact the paper currently leaves on the table. (This also
softens E3 for Task B specifically: the reproduction shows the
eb_strength choice does not move the Task B result on R0 — the
circularity concern stands in principle, with evidence it was immaterial
here.)

### D10. The "evidence map" is not in the repository. [must-fix]

Appendix C opens: "Every claim in this paper resolves to one of three
kinds of evidence, **all in this repository**." But `.gitignore` excludes
`/generated/` entirely — none of the C.2 evidence artifacts are under
version control. They exist as local, reproducible outputs (and Appendix
B does regenerate the R0 ones exactly, per D9), but a fresh clone
contains **no evidence pack at all**, and one cited artifact (the R0′
calibration run behind §6.3's only favorable calibration claim) does not
exist even locally. For a paper whose §5 is titled "The auditability
contract," this is the first thing a validator's checkout will surface.
Fix: commit a frozen evidence pack (with hashes) or amend Appendix C to
"reproducible via Appendix B" — and generate the R0′ artifact either way.

### D11. §4's empirical-edge clause is dead code in production. [should-fix]

§4: candidates are channel-blocked "(plus, optionally, pairs with
empirical co-occurrence above a threshold)". The production join never
exercises the option: `compound_join.py:202` builds the graph without
passing events, and `include_empirical` defaults to False
(`channels.py:57`), so `empirical` edges never exist on the production
path. Consequence: two risks that demonstrably co-fire in the data but
share no inventory tag are **permanently unscoreable**, and production
coverage rests entirely on the keyword-derived channel tags. For a system
whose thesis is "dependence shows up in the data," the data is not
allowed to nominate candidates. (Cosmetic but telling: the tau-threshold
docstring at `channels.py:63-67` still contains a mid-sentence "… no:"
self-correction.)

### D12. Identity-layer defects that exist today, not only under growth. [must-fix]

Beyond D8's missing data coordinate:

- **`interval_draws` is absent from `config_hash`** (`scorer.py:68-84`)
  and `realization_id` contains no config coordinate at all
  (`sha256(state|seed|version)`, `scorer.py:355`). Changing
  `interval_draws` changes `p_interval` *and* (through the shared RNG
  stream position) the loss quantiles — two realizations with different
  numbers carry **identical** `model_config_hash` and identical
  `realization_id`. §5.3's "the config-hash (all scorer hyperparameters)"
  is false as written.
- **The horizon is hashed as `f"{horizon_quarters // 4}y"`**
  (`scorer.py:272`): horizons 4-7 quarters share one structural identity
  (verified: identical state hash for 4 and 6), and a 2-quarter horizon
  hashes as `"0y"`. The identity layer cannot distinguish horizons the
  paper calls structural inputs.
- **risk_ids are slugified titles with order-dependent dedup suffixes**
  (`inventory.py`): a title edit or register reorder changes risk_id →
  state_hash → compound_id, stranding prior rows as zombie `candidate`
  entries under `INSERT OR REPLACE`.
- **k ≥ 3 graduation bypass:** `compound_graduate.py:132-137` applies the
  holdout-survival check only to pairs (`len == 2`); a k ≥ 3 compound
  would skip the check entirely while its eligibility record hardcodes
  `"holdout_survival": True`. Unreachable today (the join emits pairs
  only) — but it is a false attestation waiting for the first triple.

---

## 3. Technical findings on the algorithm

### T1. The dependence prior cannot hold its center for rare pairs — the estimator is systematically *below* independence exactly in the tail the paper claims to price conservatively. [major]

The prior `Beta(κ·q_ind, κ·(1−q_ind))` is "centred on independence" in
mean only. Its strength is measured in **quarters** (κ = 4), not in
expected co-events. For pairs where independence itself predicts ≈ zero
co-occurrences in the training window, observing zero is uninformative —
yet the posterior mean applies the same constant markdown to all
zero-support pairs:

| q_i (each) | q_ind | E[N_S \| indep], T=32 | P(N_S=0 \| indep) | q_S(N_S=0) | q_S/q_ind |
|---|---|---|---|---|---|
| 0.30 | 0.090 | 2.88 | 0.05 | 0.0100 | 0.111 |
| 0.10 | 0.010 | 0.32 | 0.72 | 0.0011 | 0.111 |
| 0.05 | 0.0025 | 0.08 | **0.92** | 0.00028 | **0.111** |

For the q_i = 0.30 row the 9× markdown is defensible: zero co-fires in 32
quarters genuinely is evidence of below-independence coupling. For the
q_i = 0.05 row it is a prior artifact: zero co-fires is the *expected*
outcome under independence (92% probability), yet the pair is priced at
11% of the independence probability — the very probability the paper's §1
calls "far too low" already. The technical cause: with α = κ·q_ind ≪ 1 the
Beta prior is L-shaped with most mass near 0; "centred on independence"
holds for the mean, not for the shape, so 32 uninformative observations
drag the posterior to the prior's mass, not its mean.

**Why the paper's evaluation cannot see this.** Within the N_S = 0 stratum
the markdown is a *constant factor* (κ/(T+κ)), and annualization is
monotone — so the **ranking** among zero-support pairs is unchanged, and
Task B (AP/AUROC/P@20) is blind to it. The distortion lives entirely in
the **level** of p̂ — and there is no compound-level calibration task
(Task A covers atom marginals only; see E4). The two blind spots are
mutually reinforcing: the estimator's level bias sits precisely where the
evaluation design has no instrument.

**Consequence.** The paper's concluding claim — "dependence-aware because
independence is demonstrably wrong in the tail" — inverts for rare-pair
compounds: for rare×rare pairs with no observed co-firing, this estimator
reports p̂ ≈ 9× *below* the independence baseline. Rare, severe,
never-yet-co-observed combinations are the product's stated reason to
exist (§1).

**Fix (deterministic, standard Bayes, preserves the design language).**
The naive repair — prior strength in pseudo-co-events,
`Beta(c, c·(1−q_ind)/q_ind)`, i.e. `q_S = (N_S + c)/(T + c/q_ind)` —
holds the center for zero-support rare pairs (≈ 0.93·q_ind at c = 1) but
over-corrects in the opposite direction: its effective sample size is
c/q_ind ≈ 400 pseudo-quarters for a rare pair, so **observed** co-firing
gets crushed (a rare pair with N_S = 2 — an event ~170× likelier under
coupling than under independence — would score ≈ 0.007 vs. the ≈ 0.05
the current estimator gives). No single Beta prior can treat zeros as uninformative *and*
positives as decisive; that is a shape limitation of the conjugate
family. The estimator that does both is a **two-component mixture**
(spike-and-slab, closed form, stdlib): an independence spike
`Beta(S·q_ind, S·(1−q_ind))` with S = c/q_ind, plus a diffuse coupling
component `Beta(1,1)`, prior weight π₀ ≈ 0.9, posterior-model-averaged
via Beta-Binomial marginal likelihoods. Verified numerically at T = 32:

| stratum | N_S | empirical | current (κ=4, clipped) | mixture |
|---|---|---|---|---|
| rare (q_ind=0.0025) | 0 | 0 | 0.0003 | **0.0024 ≈ q_ind** |
| rare | 2 | 0.063 | 0.050 | 0.0397 |
| common (q_ind=0.09) | 0 | 0 | 0.0100 | 0.0233 (markdown kept) |
| common | 3 | 0.094 | 0.0933 | 0.0935 |

The mixture holds the skeptic's null where zeros prove nothing, keeps the
markdown where zeros are informative, responds to observed coupling, and
its posterior coupling weight (1 − w_indep) is an auditable
"dependence-evidence" scalar per pair. Ship it as a **challenger** first,
promoted only after a compound-level calibration task (E4) that includes
a supported-rare-pair stratum. Alternatively (larger, subsumes this): fit
the common-factor channel-shock model the spec already prescribes as the
sparse-data default (`algorithm-spec.md` §4.3) — the model R0 is
*generated* from but the scorer never *fits*; see proposal §A2.

### T2. In 15 of the 105 shipped realizations, p̂ lies outside its own 90% interval. [major]

This is not hypothetical: 15/105 records in the shipped
`score_realizations.jsonl` report a point estimate above the interval's
upper end — e.g. `scn-04bc49946200` has p̂ = 3.06e-4 against an interval
of [0, 4.7e-11], seven orders of magnitude apart. A validator opening the
audit blob meets a point outside its interval in 14% of rows. Three
mechanisms in `scorer.py:280-297`:

1. **L-shaped posterior:** with α = κ·q_ind ≪ 1 the Beta posterior is so
   right-skewed that the posterior *mean* (the reported p̂) exceeds the
   95th percentile of the draws. Clipping alone cannot fix this — either
   report the posterior median alongside the mean, or flag
   `p_hat > p_hi` rows explicitly. (The T1 reparameterization removes the
   α ≪ 1 regime and fixes this at the root.)
2. **Numerical underflow:** `betavariate` at tiny α returns values that
   annualize to ~0, producing near-degenerate intervals.
3. **Fréchet asymmetry:** the point estimate is clipped to `min_i q_i`
   (`scorer.py:234-235`); the 4,000 draws are not (`scorer.py:292-296`),
   so the interval can also exceed a bound the point respects, and when
   the clip binds the interval is centred on the unclipped mean. Fix:
   clip each draw.

Separately, the posterior conditions on the point estimates q_i (q_ind
treated as known): the interval reflects joint-count uncertainty only,
understating total uncertainty most for thin-history atoms. Fix:
two-level draws — sample each q_i from its own Beta posterior, form q_ind
per draw, then draw q_S.

### T3. Same-quarter co-occurrence is the only dependence the model can see. [moderate]

`all_must_fire` within a single quarter is a clean occurrence definition,
but the *estimator* and the *labels* both use it too — so a lagged cascade
(control failure in Q1 → third-party failure in Q2) contributes nothing to
N_S, nothing to the holdout positives, and nothing to the score. Realistic
transmission narratives (§2 stores them!) are mostly lagged. Cheap
sensitivity: windowed co-firing (both atoms fire within w ∈ {1, 2}
quarters) as a parallel estimate, documented like the k-of-n sensitivity.
Higher rung: Hawkes-style cross-excitation, which also fits the spec's
open continuous-time question (§15.3).

### T4. Event multiplicity is discarded where it matters most — the loss. [minor]

Layer 1 reduces a quarter to a binary indicator (documented), but the MC
then draws **one** severity per atom per hit quarter (`scorer.py:310-312`)
— a quarter that historically produced 7 events contributes the same
per-quarter loss as a single-event quarter. The R0′/NB machinery exists
for calibration but the scorer never uses counts. Standard LDA practice
(compound Poisson per quarter: N ~ Poisson(λ̂_i), sum of N severity draws)
would use data the pipeline already has (`IncidentFeature.count`).

### T5. The severity layer has a discontinuity and a ceiling. [minor]

- At n = 29, w = 29/59 ≈ 0.49 (half pool); at n = 30, w = 1 (full MLE) —
  a discontinuous jump at the threshold. Smooth alternative: use
  w = n/(n+c) for all n, drop the three-tier branch (keep the tags).
- Sigma is blended linearly; blending log-variances or precisions is the
  defensible choice under a lognormal model.
- The EVT overlay fits the MC *aggregate* — a sample generated from
  lognormal marginals. It can stabilize the empirical q99 across seeds
  (A3's result, and its honest value) but it can never detect
  heavier-than-lognormal severity: the tail ceiling is set by the
  lognormal assumption at Layer 2, and MoM-GPD is anyway only valid for
  ξ < ½. Op-risk severity is routinely heavier. The missing piece is
  **data-side** tail treatment: family selection at Layer 2 (the spec
  §3.2 already prescribes candidates + holdout selection) and/or POT on
  event-level excesses. The paper should state that the current overlay is
  a variance-reduction device on model output, not evidence about true
  tails.
- `evt.py:91`: a sensitivity threshold with < 20 excesses is silently
  skipped — with exactly 200 hits, the 0.95 refit has ~10 excesses and the
  stability verdict rests on 0.85 alone, while §3.5 says "refit at
  neighbouring thresholds (0.85, 0.95)". Emit which thresholds actually
  refit (the artifact records `xi_sensitivity` keys, so this is visible —
  but the paper should say the check can degrade to one-sided).

### T6. k-way scoring ignores pairwise information. [minor]

`joint_quarterly_combo` for k ≥ 3 uses only the k-way count and the full
independence product; the (much better-supported) pairwise shrunk joints
are computed but unused. Cheap improvements: clip by
`min over pairs q̂_ij` (a sharper Fréchet bound than `min_i q_i`), or
chain conditionals from pairs. The common-factor model (proposal §A2)
subsumes this properly.

### T7. Emerging risks are exactly where this engine is weakest — by construction. [moderate, strategic]

Three design choices compound: (a) EB shrinkage pulls a short-history
atom's rate toward the *pooled historical* average — for a genuinely novel
risk this is shrinkage toward the wrong reference class; (b) new atoms
have no co-occurrence support, so T1's below-independence collapse applies
to their every pair; (c) severity for new cells is `pooled_prior`. Net:
**the newer the risk, the more the score is an artifact of priors chosen
for their statistical convenience rather than their relevance** — and this
product's charter is *emerging* risks. The deterministic engine has no
mechanism to price a novel compound above its (absent) history. This is
not a bug — it is the honest limit of the data path — but the paper never
names it, and it is the single strongest argument for activating the
judgment channel (layers 6-7) that the architecture already reserves. The
companion proposal is built around this gap.

### T8. Production-path integrity blemishes (outside the scorer, inside the paper's §4 story). [minor but visible]

- **R1 unit pollution:** `r1.py:61` maps missing loss amounts to `1.0`, so
  the severity fit mixes $1 placeholders with real USD amounts in one
  lognormal — and on activation-only data the "losses" are unit-free
  constants. `compound_join` persists loss quantiles from those fits, and
  graduation then prints them as dollars:
  `financial_impact_band = "q95 ≈ $0.0M (research)"`
  (`compound_graduate.py:270-272`). On the R1 regime, loss quantiles have
  no loss semantics and should be suppressed (regime-aware output
  contract), not formatted as currency.
- **Fabricated-looking metadata on graduation:** every graduated compound
  is stamped `signal_maturity="established"`, `momentum="accelerating"`,
  `evidence_strength="moderate"` unconditionally
  (`compound_graduate.py:261-263`) — house rule "no fake precision" says
  derive or omit.
- **Graduated compounds get a legacy urgency score anyway:** they enter
  `scenarios` as `active`, phase 4 scores them; their likelihood string
  (`"p̂ 0.412 [0.310–0.530] (research)"`) matches no `LIKELIHOOD_MAP` key
  so they silently receive the fallback constants (0.5 likelihood, 0.45
  financial — verified by executing the phase-4 parsers on the exact
  formats). The paper's §2 promise that "compound scores never map onto"
  urgency is honored in the letter — the urgency a graduated compound
  gets instead is *arbitrary*, which is worse. Exclude scenarios whose
  persisted origin is `compound_graduation` from phase-4 scoring, or map the
  bands deterministically; do not infer provenance from the ID format.
- **Words-not-numbers boundary, ecosystem view:** the compound scorer
  itself is LLM-free as claimed. But (i) the LLM judge's clamped score
  gates which compounds *enter the library* (a number deciding
  publication), (ii) the LLM prose fields of graduated scenarios may
  contain numbers (the prompt even instructs detailed_rationale to cite
  the dependence support), enforced by instruction, not mechanism, and
  (iii) elsewhere in the same system the legacy urgency score is largely
  derived from LLM-authored text (likelihood words, dollar strings,
  verbosity-rewarding foresight terms — `phase4_prioritize.py`). None of
  this contradicts the paper's scoped claim; all of it belongs in a
  "boundary map" appendix, because a validator will discover it and the
  discovery will cost credibility that the paper's honesty otherwise buys.

### T9. Atom identity is string-fragile, and identity errors masquerade as dependence. [minor, forward-looking]

Risk identity across the system is exact/slugified string matching
(`phase2` exact issue equality; `r1.py` title-set filtering;
`inventory.slugify`). Two records describing the same underlying risk
under different names become two atoms; any shared evidence then registers
as co-occurrence — *identity error becomes dependence signal*, the exact
quantity the product sells. The phase-6 scenario dedup (token Jaccard +
three hardcoded demo phrases) does not cover the risk register. Worth a
line in §7; the fix (semantic dedup with human-gated merge) is in the
companion proposal.

---

## 4. Evaluation-design findings

### E1. The headline lift is against a baseline the generator forces to chance, and the strong baseline is an oracle. [must-fix]

B1 (independence) scores AP 0.398 vs B0 random 0.391 — near-chance. This
is not an empirical discovery about independence; it is **built into the
generator**: R0's marginal firing is shock-dominated with near-homogeneous
atom parameters (`synthetic.py:62-66`), so products of marginals rank
noise, while the same channel shocks that flatten B1 *create* the
co-firing labels. The pre-registered "≥ 15% lift over B1" bar was
structurally near-unloseable on R0, and the 84% figure is a function of
`shock_pi`/`q_base_range`, not a finding about the world. Meanwhile B2's
channel tags on R0 **are the planted ground truth** — an oracle, not a
deployable baseline — so "channel knowledge carries most of the signal"
reads, on this regime, as "the answer key carries most of the signal."
The honest data-only margin the table supports is B3 vs B2: **+5.2% AP**
(0.734 vs 0.697), **P@20 0.90 vs 0.80**, **AUROC 0.761 vs 0.777 — B3
loses**. Recommendation: report the B2-relative lift in the abstract
alongside the B1 lift; state B2's oracle status on R0 explicitly; explain
the AUROC inversion (AP/P@20 weight the top of the ranking — the right
metric for a top-20 product, so say so); and add a composite baseline
(graded channel weight × empirical count) so the shrinkage layer's
marginal value over "channels + any tie-break" is isolated. On real
regimes (R1 today) note plainly that B3 ≡ B1 because no pair co-occurs in
train — the dependence layer is currently inert in production (see E8).

### E2. No uncertainty on the evaluation metrics themselves. [moderate]

105 pairs, 39 holdout positives, a single frozen split. The B3−B2 AP gap
(0.037) carries no CI; bootstrap over pairs (or over holdout quarters)
would cost minutes. Multi-seed R0 replication (regenerate the world with
10 seeds, report the AP-lift distribution) is fully deterministic and
free, and would also answer E3.

### E3. Mild hyperparameter circularity on the decision regime. [moderate]

`eb_strength = 4` was chosen from the Task A calibration harness **on R0**
(§3.1 says so), and the GO decision is measured on the same R0 (single
generative seed). κ = 4 is a bare code default — it appears nowhere in
the spec and no selection procedure is stated. Nothing dishonest — but
the decision regime doubles as the tuning regime. Multi-seed replication
(E2) plus reporting Task B under κ ∈ {1, 4, 16} would close it.

### E4. There is no compound-level calibration task — and R0 has the ground truth to run one. [moderate; enables T1's invisibility]

Task A calibrates atom marginals. Nothing calibrates p̂(S) or its interval
at the pair level, yet on R0 the generative truth q_S is *computable* for
every pair (common-factor closed form or long-simulation). Add Task B-cal:
coverage of the 90% p̂ interval over the 105 pairs, and level bias
stratified by `E[N_S | independence]` **and by (rare × supported)** — the
first stratification makes T1 visible; the second would have caught the
naive fix's regression (see T1's fix paragraph). The spec's
proxy-calibration section (§11.2) already points here; this is the
concrete instrument.

### E5. Evaluation-integrity nits.

- Fresh eval reports carry stale hardcoded limitations (see D5).
- Task D (expert resolution) remains open; the simulated-panel FAIL is
  properly labeled and does not count — good — but the paper's "workshop
  material" framing ultimately rests on a test that has not run.
- The B2 baseline includes an independence tie-break (1e-6 weight,
  `candidates.py:58`) — worth one footnote, since it makes B2 slightly
  composite already.
- **A1 is a wiring check presented as design evidence.** With
  `dependence_mode="independence"` the scorer computes literally B1's
  formula (`scorer.py:230-231` vs `candidates.py:51`), so "collapses …
  onto the independence baseline to three decimal places" is a code identity
  that cannot fail; the AP drop it reports is Task B's B3−B1 gap counted
  a second time. Fine as a smoke test — §6.4 should not present it as the
  "cleanest possible confirmation that the dependence layer is the
  product."
- A3's consistency rule cannot fail unless EVT makes q99 stability > 5%
  *worse* (`ablations.py:175-177`), on 3 seeds × ≤ 5 pairs, and its
  with-EVT arm silently substitutes the empirical q99 when the overlay is
  unstable (`ablations.py:157-160`). "Consistent: yes" is therefore a
  weak claim; say what the tolerance is.

### E6. Task C does not survive an audit at the level the paper invites. [must-fix]

Four problems, in increasing order of discomfort:

1. **The pre-registered second metric is unpassable as written.**
   `evaluation-plan.md` §3.3 requires "planted high-risk pairs rank in
   top decile of U" at ≥ 0.70 — but there are 30 planted pairs and the
   top decile holds ⌈105/10⌉ = 11 slots; recall against 30 caps at 0.37.
   The harness (`evaluate.py:224-226`) implements a *different* metric —
   planted fraction **of** the top decile — and "high-risk" is defined
   nowhere. The reported 0.909 answers a reinterpreted question; the
   paper presents it as the pre-registered one.
2. **No base rates.** Random selection of 30 from 105 gives 0.286
   precision; random selection among the 54 pairs that co-occur at all in
   train gives 0.481. Against those floors, 0.767 is real but much less
   dramatic than it reads bare.
3. **The lift machinery adds nothing on the decision regime.** Ranking
   pairs by raw co-occurrence *count* reproduces precision 0.767 exactly;
   `recover_empirical_edges`' lift normalization (`channels.py:115-144`)
   is inert here. The paper's "co-occurrence lift must recover it" implies
   the normalization matters; on R0 it does not.
4. **Tie-break leakage at the pass margin.** Ties at the top-30 cut are
   broken alphabetically on risk_ids that *embed the planted channel
   name* (`r0-cloud-05`), grouping same-channel pairs; the pass margin is
   23/30 against a 21/30 threshold. Probably survivable — but a
   pre-registered test should not owe its margin to id lexicography.

Also worth stating: Task B's B3, Task C's precision, and Task C's
top-decile metric are three views of the same train co-occurrence
statistic; §6 presents them as independent confirmations.

### E7. L|S — half the promised output vector — has no evaluation task at all. [must-fix]

§2 defines the output as p̂ *and* the conditional loss distribution with
four quantiles. Tasks A/B/C evaluate frequency, occurrence ranking, and
structure recovery; nothing anywhere scores the loss quantiles against
anything. The only loss-adjacent evidence is A3 (seed *stability*, not
accuracy — see E5). This is doubly unfortunate because the machinery to
close it exists: `r0_truth.json` stores every atom's true `sev_mu`/
`sev_sigma`, so true compound loss quantiles are computable and a
severity-calibration task is a few dozen lines. And on the production
regime the loss numbers that *are* persisted are placeholder arithmetic
(T8's $1.0 default). Until a loss-side task exists, the paper should
demote L|S from "the output is a distribution" to "occurrence is
evaluated; severity is reported unvalidated."

### E8. The GO decision sits on a pre-registration conflict the paper does not surface. [must-fix]

`evaluation-plan.md` §9 (phase mapping) requires Phase 1 to pass "R1
Task B vs B0/B1." R1 Task B **failed** (78 pairs, 4 holdout positives,
lift 0.0). The paper's §6 instead adopts the roadmap §1.3 criterion —
"R1/R0 Task B … **or clear documented failure**" — a disjunction that any
outcome satisfies, and declares R0 the decision regime. Documenting the
R1 failure honestly (which the paper does) is not the same as
acknowledging that the two pre-registered gates disagree and the lenient
one was chosen after results were known. One paragraph in §6 naming this
would cost little; a validator finding it unnamed costs more. The same
paragraph should state the operational corollary (also E1): on the only
real regime, B3 ≡ B1 today — every production score currently has zero
empirical dependence support, so the layer the paper calls "the actual
product" has yet to influence a single production number.

---

## 5. Paper-craft

- **No related work.** For a paper whose thesis is "boring where numbers
  must survive model validation," the absence of the literature that makes
  each component boring is a gap: LDA frequency×severity (Frachot et al.),
  POT/EVT in op-risk (and the known ξ ≥ ½ heavy-tail findings that
  motivate T5), copula aggregation, empirical-Bayes shrinkage, Cooke's
  classical model for exactly the §3.6 pooling-with-calibration-weights
  protocol, and pre-registration norms. One page, high leverage with the
  validation audience.
- §3.7's class list implies three reachable classes; the deterministic
  path can only emit `statistical`/`mixed` (`judgment_dominant` requires
  elicitation that doesn't exist) — one clause.
- Appendix A: `marginal_smoothing = 0.5` (the eb_strength=0 fallback) is
  in the config and its hash but missing from the table.
- The Beta-interval floor (1e-6 pseudo-counts, boundary-bug fix per
  roadmap) is a hardcoded constant (`scorer.py:290-291`), not part of the
  config hash — worth a footnote, and arguably worth promoting into the
  config surface alongside the D12 fix.

---

## 6. Summary of requested revisions

| # | Item | Severity |
|---|---|---|
| D1 | Fix "both regimes" NLL claim (R0′ only); disclose Brier fail + PIT spike | must |
| D6 | Fix §3.3 interpretation sentence (decays below independence, not to it) | must |
| D2 | Persist full realizations in production join, or scope §5.1 | must |
| D10 | Evidence pack is gitignored; R0′ artifact missing — fix Appendix C's claim or commit a frozen pack | must |
| D12 | interval_draws into config hash; config into realization identity; horizon hashing; slug churn; k≥3 graduation bypass | must |
| T1 | Rare-pair prior collapse: disclose in §7; fix via mixture prior (challenger-first) or common-factor model | must (disclose) / should (fix) |
| T2 | 15/105 shipped rows have p̂ outside its interval: report median or flag; clip draws; propagate marginal uncertainty | must |
| E1 | Generator-forced B1, oracle B2: re-anchor headline; state both; explain AUROC inversion | must |
| E6 | Task C: state the metric reinterpretation, base rates (0.286/0.481), count-equivalence, tie-break margin | must |
| E7 | Add a loss-side evaluation task (truth exists in r0_truth.json) or demote L|S claims | must |
| E8 | Name the eval-plan §9 vs roadmap §1.3 pre-registration conflict; state B3 ≡ B1 on R1 today | must |
| D8 | Add data-snapshot hash to realization identity; keep score history | should |
| E2/E3 | Bootstrap CIs; multi-seed R0; κ sensitivity | should |
| E4 | Add compound-level calibration/coverage task on R0 (incl. supported-rare stratum) | should |
| D3/D4/D5/D9/D11 | Spec-vs-code tense fixes; stale stub strings; re-emit artifacts; dead empirical-edge clause | should |
| T7 | Name the emerging-risk cold-start limit in §7; it motivates layers 6-7 | should |
| T8 | R1 loss-unit suppression; graduation metadata; origin-aware urgency exclusion | should |
| T3/T4/T5/T6 | Lagged windows; count-aware MC; smooth severity blend; data-side tails; k-way from pairs | roadmap |
| E5 | Reframe A1 as wiring check; state A3's tolerance | should |
| §5 | Add related work | nice |

Two closing observations. First: none of the must-fix items above
required access the authors lack — every one is visible from the repo and
its own artifacts, which is exactly why they should be fixed before an
external validator finds them. The paper's honesty culture is its real
asset; these findings are the compound interest on cashing that asset
consistently, including where it hurts (§6's headlines).

Second, the strategic findings (T1, T7, E4, E7, E8) point one direction:
the deterministic engine is excellent at pricing *documented* dependence
and structurally silent about *undocumented* dependence — and on the only
real regime it has yet to see any documented dependence at all. That is
the product's actual subject. The architecture already reserved the
socket for the missing capability (layers 6-7, Task E/F, the modifier
gate). The companion proposal specifies how to fill it without giving up
what this paper got right.
