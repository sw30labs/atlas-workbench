# Making the Scorer Hybrid: Deterministic Upgrades + a Disciplined LLM Judgment Channel

**Companion to** [`WHITEPAPER_REVIEW.md`](WHITEPAPER_REVIEW.md). Status:
proposal for discussion — nothing here is implemented. This document was
drafted, then adversarially reviewed against the spec pack and the code,
and revised; the revision notes are preserved in §F so the failure modes
of the first draft stay on the record (house style).

Everything below is designed to survive the house rules: ADR-001 (no LLM
produces or adjusts a score figure — with its own recorded exception path,
see §0), the §7 elicitation protocol and §8 pooling spec in
`algorithm-spec.md`, review-first ingestion (ADR-006), advisory-only
review assistance (ADR-013), the egress gateway (public-only to frontier
models), and the pre-registration culture of `evaluation-plan.md`.

---

## 0. The question, answered up front

> *"The algo today is pure match / deterministic. Is it possible to improve
> it by making it Hybrid (LLM)? E.g. match historical patterns from model
> background knowledge against current risks/compounds and increase the score
> on a match?"*

**Yes — and the architecture already contains the socket for it.** The
whitepaper's §3 defines layers 6-7 (judgment priors, pooling) as
protocolized channels that are *empty* on the deterministic path;
`model-inventory.yaml` says `judgment_channel: none`; Task E (judgment
calibration) and Task F (A/B model configs) are reserved evaluation
slots; the modifier path (`associational_feature` → human/policy-accepted
bounded modifiers, δ ∈ [0.5, 2.0]) is specified and gated. The question
is not *whether* to make it hybrid — the design is hybrid-by-declaration
with the judgment half switched off — but **how to switch it on without
destroying the property that makes the engine validatable**.

**Reconciling with ADR-001 explicitly, since a validator will ask.**
ADR-001's decision is "no LLM produces or adjusts any frequency,
severity, dependence, or tail figure," and it records the sanctioned
exception path itself: judgment may exist only as a *tagged, separate
channel* under the spec §7.2 protocol, and its alternatives-considered
section keeps "LLM proposes modifiers, humans accept" as a possible
Phase-4 extension behind a policy-accept gate. Everything in Part B lives inside that exception:
quantile *elicitation* into the tagged judgment channel (which spec §7.2
mandates in exactly that form) is the one sanctioned way judgment touches
numbers, and it reaches a production score only through the bounded,
human-gated, dual-realized machinery below. Activating the channel is a
semver-major event and a `model-inventory.yaml` change
(`judgment_channel: none` → `protocolized`), not a quiet feature.

The naive version of the idea — "LLM sees a pattern match, score goes up"
— would be rejected by this repo's own governance, four times over:

1. An LLM writing a number into the score path (ADR-001; spec §12
   "LLM scoring of loss: Forbidden as primary").
2. Unfalsifiable judgment ("these feel 70% correlated" — the exact
   anti-pattern spec §4.1 names).
3. Post-hoc pattern matching against history — "a machine for just-so
   stories," the reason the §10/§6.5 FDR gauntlet exists.
4. Retrospective validation of LLM historical knowledge is contaminated:
   the model has read the outcome.

The disciplined version keeps the user's insight — **the outside view
(historical analogues) is one information source for risks with little
registered history, and the deterministic engine is weakest
exactly there** (review T1/T7: cold atoms get pool-shrunk rates, zero
co-occurrence support, and pooled severities) — and routes it through
machinery where every number stays reconstructable:

> **The LLM writes structure; deterministic code converts structure to
> bounded numbers; humans gate the conversion; both the with-judgment and
> without-judgment scores are persisted; a prospective ledger decides
> whether judgment earns weight.**

This extends the scorer's own audit pattern one level up: it already
stores `q_independence` next to the shrunk `q_S` so "the dependence
correction is itself an audited number." Here: store the deterministic
realization next to the hybrid realization so *the judgment correction is
itself an audited number* — and wire their difference into the schema's
existing `disagreement` field, which spec §8 defines as exactly this
(range across sources, never averaged away).

### The influence ladder (graduated rights)

| Rung | What judgment may touch | Bound | Prerequisite |
|---|---|---|---|
| **L1 — attention** | Ranking/triage of workshop attention; display fields (`coherence`, analogue dossiers, refutation memos) | no contact with p̂ | none — start here |
| **L2 — priors** | Prior *centers/mass* inside the existing shrinkage estimators. Analogue-driven mass capped per P2's published table (coupling prior-weight shift Δπ ≤ +0.15; matched-pair pseudo-observations); elicited centers clamped to `[q_ref/δ_max, q_ref·δ_max]` with δ_max pre-registered (mirroring the spec's modifier bounds) | explicit numeric caps, dual-realization | G-shadow pass (Part C); ledger live |
| **L3 — pooled p̂** | Logarithmic opinion pool of deterministic posterior + judgment prior, weights from measured ledger calibration | weights sum to 1; disagreement retained | ≥ 4 quarters prospective ledger; G-cal pass; policy accept; semver-major |

Every rung is ablatable to the rung below via
`judgment_mode: off | rank_only | priors | pooled` in the scorer config
(hash-tracked). The deterministic realization remains the authoritative
row in `compound_scenarios` at every rung below L3; hybrid realizations
are stored alongside with `judgment_version` inside their realization
identity (without it, dual realizations under one state hash would
collide — see review D12's identity fixes, which are prerequisites).

---

## Part A — Deterministic upgrades first (no LLM; do these regardless)

These fix the review's technical findings and make any hybrid honest: a
judgment channel fused onto a biased deterministic core would launder the
bias. **Ordering note:** A7 (persistence/history) ships *before* any
estimator change, so the change itself is recorded rather than silently
overwriting rows (review D8/D12); estimator changes are semver-major per
spec §14 ("score components change meaning").

### A1. Fix the dependence prior with a mixture, not a swap (review T1)

The current prior collapses zero-support rare pairs to ~11% of
independence (T1). The obvious repair — prior strength in pseudo-co-events,
`q_S = (N_S + c)/(T + c/q_ind)` — holds the center at zero support but
**over-corrects for observed support**: its effective sample size is
c/q_ind ≈ 400 pseudo-quarters for a rare pair, so N_S = 2 (an outcome
~170× likelier under coupling than under independence) would be crushed
to ≈ 0.007 where the current estimator gives ≈ 0.05. No single Beta prior can treat zeros as
uninformative *and* positives as decisive — a conjugate-family shape
limit, found by this proposal's own adversarial pass (§F).

The estimator that does both is a **two-component mixture** (standard
Bayes, closed form, stdlib): independence spike
`Beta(S·q_ind, S·(1−q_ind))` with S = c/q_ind, diffuse coupling slab
`Beta(1,1)`, prior weight π₀, combined by Beta-Binomial marginal
likelihoods. Behavior at T = 32, c = 1, π₀ = 0.9 (verified numerically):

| stratum | N_S | empirical | current (κ=4, clipped) | naive c-fix | mixture |
|---|---|---|---|---|---|
| rare (q_ind = 0.0025) | 0 | 0.000 | 0.0003 | 0.0023 | **0.0024 ≈ q_ind** |
| rare | 1 | 0.031 | 0.028 | 0.0046 | 0.0072 |
| rare | 2 | 0.063 | 0.050 | 0.0069 | **0.0397** |
| rare | 3 | 0.094 | 0.050 | 0.0093 | 0.050 (Fréchet) |
| common (q_ind = 0.09) | 0 | 0.000 | 0.0100 | 0.0232 | 0.0233 (markdown kept) |
| common | 3 | 0.094 | 0.0933 | 0.0928 | 0.0935 |

The mixture holds the skeptic's null where zeros prove nothing, keeps the
markdown where zeros are informative, and responds to observed coupling.
Its posterior coupling weight (1 − w_indep) is a new audited
"dependence-evidence" scalar per pair, worth persisting in the dependence
block. π₀ and c are config-hashed with pre-registered sensitivity rows
(π₀ ∈ {0.8, 0.9, 0.95}, c ∈ {0.5, 1, 2}).

**Discipline:** ships as **challenger**, not primary. Promotion requires
the extended Task B-cal (A6) — including the *supported-rare* stratum
that would have caught the naive fix's regression — to favor it. The
interval machinery follows the same mixture (model-averaged draws),
which also removes the α ≪ 1 regime behind the 15/105
point-outside-interval rows (review T2).

### A2. Fit the common-factor model the spec already prescribes (higher rung; subsumes A1's role when data allows)

R0 is *generated* from a channel-shock common-factor model
(`synthetic.py`; spec §4.3 names it the sparse-data default) — but the
scorer never fits that structure. Closing the generator-estimator gap
gives graded channel-conditional dependence, k-way joints by conditional
independence given shocks, and a structural fix for rare-pair collapse.

Honesty about scope, added after adversarial review: this is ~33
parameters (π̂_c per channel; base rate and shock response per atom) from
32 quarters of binary series — identifiable only because channel
membership pools atoms (5 atoms × shared shock quarters), and the
generator's two-parameter shock response (`q·mult + add`) will be
approximated by a one-parameter lift, so a generator-estimator gap
remains by design. The EM/moment procedure must be fully deterministic
(fixed init, fixed iteration count, both config-hashed). Expert edges
from P1 do not fit a channel-membership factor model; they enter as a
pairwise residual term (spec §4.4's REC: common-factor + capped residual
dependence), not as phantom channels. Scope: R0 and the synthetic-cell demo
challenger now; on R1 today even the pairwise
estimator is data-starved (review E8), which no estimator choice fixes.

### A3. Honest intervals (review T2)

Two-level posterior draws (marginals then joint), Fréchet-clip every
draw, report posterior median alongside mean (the mean of an L-shaped
posterior can exceed its own q95 — 15/105 shipped rows do), flag any
`p_hat > p_hi` row, and floor Beta parameters in the config surface
rather than as a buried constant.

### A4. Time-awareness (review T3, T7)

- Windowed co-occurrence sensitivity (`within_w_quarters`, w ∈ {1, 2})
  persisted alongside all-must-fire — same "documented sensitivity"
  pattern the paper promises for k-of-n (which does not exist yet;
  review D4 — implement it or stop citing it).
- Exponential time-decay on the occurrence series (half-life
  config-hashed): an emerging-risk product should weight recency.
  Challenger discipline, same as A1.

### A5. Count-aware Monte Carlo (review T4)

Per hit quarter draw a **zero-truncated** Poisson event count per atom —
P(k) = Pois(k|λ̂_i)/(1 − e^(−λ̂_i)) for k ≥ 1, sampled by seeded
rejection (draw until ≥ 1), *not* `max(1, Poisson)` which piles P(0)
onto 1 and biases the mean — then sum severities. Uses
`IncidentFeature.count` and the R0′/NB machinery that already exist.

### A6. Evaluation hardening (review E1-E8)

- Bootstrap CIs on AP/AUROC/P@20; multi-seed R0 (10 generative seeds →
  distribution of lifts); κ/c/π₀ sensitivity rows.
- **Task B-cal (new):** compound-level p̂ coverage and level bias on R0
  against generative truth, stratified by E[N_S | independence] **and by
  (rare × supported)** — the stratum where the naive T1 fix fails. This
  is the instrument that makes level pathologies visible at all.
- **Task L (new):** loss-side evaluation against `r0_truth.json`'s
  per-atom severity parameters (review E7: L|S currently has no
  evaluation task anywhere).
- Composite baseline (graded channel weight × co-occurrence count);
  report B2's oracle status on R0 (review E1); fix Task C reporting
  (base rates, metric-as-implemented, tie-break; review E6).
- Fix stale limitation strings; re-emit artifacts (review D5/D9/D10).

### A7. Production auditability — ships first (review D2, D8, D12, T8)

Full `ScoreRealization` persisted per promoted compound; `data_hash`
(fitted signal series + train bounds) added to realization identity;
`interval_draws` into the config hash; stable `pair_id`
(hash of sorted risk_ids) column so compounds survive graph-version and
slug churn; append-only realization history (current-view on top of it);
horizon hashed in quarters not `//4` years; regime-aware loss
suppression (R1 activations carry no loss semantics — stop formatting
them as dollars); graduation metadata derived or omitted;
`origin=compound_graduation` excluded from legacy urgency scoring without
parsing IDs; k≥3 graduation holdout check
implemented before any k≥3 compound exists.

---

## Part B — The judgment channel: eight integrations, one audit shape

Each item specifies: **what the LLM writes / what code converts / human
gate / where stored / ablation / kill rule.** Where a field is "none,"
it says so explicitly rather than omitting it.

**Engineering reality (added after adversarial review; gates this whole
Part).** Today the tree has exactly one LLM client (`OMLXClient`, local),
no frontier client, no egress schemas for judgment payloads, and a
review queue that handles two item kinds (evidence, risks). Before any
P-item below runs: (i) a frontier client wired through the same
telemetry choke point (the "100% of LLM calls have an event" invariant —
telemetry-spec §8, enforced at ADR-007's choke point — must hold for it
by construction); (ii) new egress allow-list schemas
(`analogue_elicitation`, `elicitation_packet`, `channel_audit`) with
red-team deny tests extending `test_scoring_egress.py` (evaluation-plan
§7.4); (iii) review-queue item types for channel proposals, analogues,
and elicitation packets — tables, decision functions as the only write
path (ADR-013 pattern), provenance stamps, UI cards; (iv) a quarterly
resolution job that maps ledger claims to resolved outcomes. This is
real work: budget it (Part E) rather than discovering it.

### P1. Channel-graph enrichment — highest leverage, no score contact

**Problem:** production candidate generation is `shared_channel` edges
only — `compound_join.py:202` builds the graph without events, so the
empirical-edge option is dead (review D11), and a missing tag means a
pair is *never scored*. Tag quality is the binding constraint on the
system (B2 carries the signal), and tags come from regex keyword rules.

- **LLM writes** (public payload: titles/descriptions): per-atom tag audit —
  missing channels from the existing taxonomy, proposed new channels,
  proposed atom-atom expert edges; every proposal carries a named
  transmission mechanism and a public citation resolved by the
  deep-research loop (URL-clamped; unresolvable citation ⇒ dies before
  review).
- **Code converts:** approved items → `RiskAtom.channels` entries /
  `GraphEdge(reason="expert", source=<proposal id>)`. Two wiring facts
  the first draft missed: `EDGE_REASONS` reserves `expert`
  (`models.py:117`) **but** the production pair generator filters to
  `("shared_channel", "empirical")` (`candidates.py:31-35`) — expert
  edges must be added to that tuple or they are silently dropped; and
  graph changes bump `graph_hash`, which churns every compound's
  state-hash identity — so **graph updates are batched into a quarterly
  freeze window**, and cross-version continuity rides on A7's stable
  `pair_id`.
- **Human gate:** channel/edge proposals are a new review-queue item
  type. Because approved tags mutate the governed risk register — a
  mutation class ADR-013 deliberately restricted to adopt-refs — this
  needs **a new ADR** ratifying the second mutation path and its gate
  (proposed: single reviewer with written reason for tags/edges;
  citations keep the two-attestation evidence flow). Not a parenthetical
  decision.
- **Stored:** `channel_proposals` table (proposal, citation, mechanism,
  decision, reviewer, graph version applied in); per-version graph diff.
- **Ablation:** dual-run per join — base vs enriched graph; report
  coverage delta and would-have-caught. Graduation freezes for the first
  enriched quarter (an L1 item still changes the candidate set, hence
  the top-20, hence graduation eligibility — say it and gate it).
- **Kill rule (disjunctive, power-aware):** stop if reviewers reject
  > 50% of proposals over a quarter, **or** if after 4 quarters the
  enriched-only cohort has zero holdout co-fires *while its expected
  count under the enriched-graph scores was ≥ 1* (an unpowered zero
  kills nothing).

### P2. Analogical priors — the user's idea, made falsifiable

An analogue is not a vibe; it is a **structured, citable, individually
reviewable claim**:

```json
{
  "analogue_id": "ANA-…",
  "for": {"pair_id": "…", "risk_ids": ["…","…"], "horizon": "1y",
           "compound_def": "all_must_fire",
           "elicited_under": {"graph_version": 7, "state_hash": "…"}},
  "event": "Knight Capital deployment cascade",
  "period": "2012-08",
  "mechanism_map": {"channel": "model", "atom_1_role": "…", "atom_2_role": "…"},
  "co_fired": true,
  "outcome_class": "q95-band-3",
  "citations": ["https://…"],
  "disanalogies": ["…"],
  "as_of_check": "citation dates precede cutoff: pass (hygiene, not proof)"
}
```

Keyed on **pair_id/risk_ids, not state_hash** (first draft's error: a
P1 graph bump would have orphaned every approved analogue); the
elicitation-time state is recorded as metadata.

- **LLM writes** (frontier via egress gateway; public payload): K candidate
  analogues per compound with mandatory `disanalogies` (spec §7.2's
  disconfirming-evidence slot) and deep-research-resolved citations.
- **Code converts:** approved analogues → bounded prior mass in the A1
  mixture: the coupling component's prior weight rises from π₁ to
  π₁ + Δ(n_approved), Δ capped (e.g. +0.15 at ≥ 3 mechanism-mapped
  co-fired analogues), and/or pseudo-observations added **in matched
  pairs** (α += w_a·r, β += w_a·(1−r) with r a fixed per-class rate from
  a published table — not success-only mass, which asserts a 100%
  co-fire rate; §F). Weights per analogue class are versioned constants.
  The LLM never emits a number; approved counts drive a published
  formula.
- **Human gate:** each analogue is a review-queue item (the register's
  evidence bar applies: mechanism-mapped, cited, no over-claiming).
- **Stored:** append-only `analogues` store + ledger rows (P7); dual
  realizations with the judgment delta wired into `disagreement`.
- **Ablation:** `judgment_mode: off` must reproduce deterministic bytes.
- **Kill rules:** placebo (below) fails ⇒ elicitor frozen out of L2;
  G-shadow fails ⇒ analogues stay L1 display; two G-shadow fails ⇒
  retire the mechanism, keep the dossiers as workshop material.

**Rung:** L1 immediately (the analogue dossier is workshop gold even if
it never touches a prior); L2 only through Part C's gates.

### P3. Frequency & severity elicitation for cold-start atoms (Layer 6 as specified)

- **LLM writes:** the full spec §7.2 packet — p10/p50/p90 for quarterly
  occurrence and severity, named reference class, disconfirming
  evidence. The **second elicitation comes from a different model or a
  human** (§7.2 item 5 — the first draft's "same model, adversarial
  framing" does not satisfy independence; it may be added as a labeled
  third, non-independent source only).
- **Code converts:** quantiles → parametric prior by an explicit stored
  method; variance inflated ×(pre-registered factor); tagged `judgment`,
  `ELD-proxy` where the reference class is external-loss-shaped. **Bound
  (added after review):** the elicited center is clamped to
  `[q_ref/δ_max, q_ref·δ_max]` around the reference-class-free default
  (pool rate), δ_max pre-registered — mirroring the spec's own modifier
  bounds — so L2 stays literally bounded, atom-level shifts included.
- **Human gate:** packet review; auto-accept forbidden (ADR-013
  precedent).
- **Stored:** `judgment` entries in the realization; ledger rows.
- **Ablation:** elicited vs pool prior per cold atom.
- **Kill rule:** Task E made decisive — if elicited priors' realized
  calibration is worse than the pool prior's on the resolving quarters
  (minimum-n per Part C), revert cold atoms to pool.

### P4. Coherence lens — display and panel instrument only

Slimmed from the first draft after review (§F): coherence **gates
nothing** — not graduation prose, not promotion (the "G6-style gate"
is dropped; a model scalar deciding publication is the anti-pattern
Part D row 3 rejects and review T8 flags in the existing judge).

- **LLM writes:** per top-K compound, a structured mechanism verdict
  (shared precondition? causal path with named intermediate? timing
  within the occurrence window? which observable would confirm?), every
  answer citing lines from the supplied dossier (refs clamped,
  signpost-style).
- **Code converts:** fixed rubric table → `coherence ∈ [0,1]` into the
  schema's existing empty field. Display only.
- **Validity instrument, fixed:** batches mix scored compounds with
  **dossier-matched controls** (random non-edge pairs given equally
  thick dossiers — same evidence-row count and structure), because with
  thin controls a rater separates classes by counting evidence rows, a
  deterministic feature. Raters that cannot separate on matched dossiers
  get weight 0 and the lens reports itself unvalidated. Note the
  roadmap's own alternative reading of the simulated-panel FAIL (all 15
  R0 atoms are plausible tech-ops risks — discrimination may be weak *by
  construction* there), which is why validity claims are confined to
  real-atom regimes.
- **Human gate:** none needed (display-only); panel use is G-panel.
- **Stored:** rubric answers + score with the realization; ledger rows
  (process-audited, not outcome-calibrated — coherence has no resolvable
  outcome; §F).
- **Ablation:** hide the column; nothing downstream may change (test).
- **Kill rule:** "two attempts" defined: two pre-registered
  configurations (rater model × dossier form), one documented change
  between them; if neither separates scored-vs-matched-control at
  AUROC ≥ 0.6 on a real-atom regime, retire the lens and record it.

### P5. Severity reference classes for thin cells (ELD-proxy, words → reviewed data)

- **LLM writes / research loop resolves:** public loss analogues
  (enforcement actions, disclosed outage costs) for a cell's reference
  class, as pending evidence rows with amounts in the source's words.
- **Code converts:** approved rows → variance-inflated ELD-proxy
  severity prior (spec §3.1's flag), blend weight fixed and tagged;
  never mixed into `observation`. Also the honest fix for R1's `$1.0`
  placeholders (review T8): real public amounts, provenance-attested.
- **Human gate:** the standard two-attestation evidence flow, unchanged.
- **Stored:** ordinary evidence rows + severity-source tag in the atom
  component.
- **Ablation:** proxy-prior on/off per cell.
- **Kill rule:** if proxy-informed severities score worse than the pool
  prior on holdout log-likelihood (Task L machinery, A6) over ≥ 10
  cells, stop retrieving and revert.

### P6. Adversarial roles — the LLM as the paper's own red team

- **Adversarial null, default-on:** the existing two-model
  `--with-llm-null` pass (`ext_features.py`) becomes the default for
  feature acceptance (fail-closed already).
- **Feature-family proposer:** LLM proposes *pre-registration
  candidates* for external-feature families (spec §10.2 step 1). Safe by
  construction: pre-registration is the gate; the FDR gauntlet is
  unchanged; the LLM just widens what gets tested.
- **Refutation memos:** for each would-graduate compound, the best case
  *against* co-materialization, shown beside the claim in review
  (dialectical review, extending ADR-013's agreement-ratio philosophy).
- **Definition stress:** propose, per compound, the **lagged-window**
  variant (A4 machinery — k-of-n is out of scope until it exists;
  review D4) most likely to change the decision; deterministic re-score;
  swing recorded as a structural component of `disagreement`.
- **Human gate:** none — none of these write state; memos and proposals
  are inputs to existing human gates.
- **Stored:** memos with the compound; proposals in the feature
  registry's candidate tier; stress results in the realization.
- **Ablation:** feature acceptance with vs without the LLM null pass
  (already a CLI flag).
- **Kill rule:** quarterly spot-audit of memos by the reviewer; if
  > 30% are rated boilerplate ("could be written without reading the
  compound"), stop generating them.

### P7. The judgment ledger — calibration as a first-class artifact

Append-only, hash-chained (pattern copied from `egress.py:177-217`,
which exists; the chain-verification helper generalizes):
`generated/judgment/ledger.jsonl`. Every elicitation, analogue, and
verdict: elicitor identity (model id + version + prompt hash),
structured claim, conversion output, human decision, resolution when
quarters resolve (via the Part B resolution job).

- **Calibration is computed only for resolvable claim types** (elicited
  occurrence quantiles; analogue-informed co-fire predictions).
  Coherence and refutations are process-audited (P4/P6), not
  Brier-scored — the first draft promised calibration for claims with no
  resolvable outcome (§F).
- **Weights:** Cooke-style performance weights for the L3 pool derive
  from the ledger. This **amends spec §8's "default equal weights"** —
  deliberately, and labeled as an amendment: equal weights are forbidden
  here *until* the ledger has data, because the ledger is the point.
- **Model churn** (objection 3.4): a model version change freezes the
  old elicitor's row and starts a new one; no weight inheritance.
- **Contamination:** the citation-date as-of check is **hygiene, not
  control** (current pages can be edited; archived snapshots required
  where available) — and no retrospective check defeats
  weights-contamination, so backtest rows are labeled
  `contaminated_possible` and **only prospective rows count** for gates.
- **Kill rule (feasibility):** if after 4 quarters the ledger resolves
  fewer than ~30 claims per quarter, the prospective program is
  under-powered by construction — declare it, stay at L1 permanently,
  and keep the dossier/red-team value (which needs no calibration).

### P8. Semantic atom-identity hygiene (restored; review T9 promised it)

Identity error becomes dependence signal: duplicate atoms under
different names convert shared evidence into fake co-occurrence — the
exact quantity the product sells. Today identity is exact/slugified
string matching plus a token-Jaccard scenario dedup with hardcoded demo
phrases.

- **Machinery writes:** local embedding model (aggregate-safe, no egress) ranks
  candidate duplicate pairs in the risk register; the LLM writes a
  structured merge proposal (same-risk? same-mechanism-different-scope?
  distinct?) with quoted evidence.
- **Code converts:** approved merges → register merge under the existing
  lock, provenance-stamped; co-occurrence series rebuilt; affected
  compounds re-scored (A7's history keeps the before/after).
- **Human gate:** merge proposals in the review queue; the register is
  never machine-mutated (ADR-013 pattern; same new-ADR umbrella as P1).
- **Stored:** proposal + decision + register diff.
- **Ablation:** co-occurrence counts before/after merges, reported per
  join.
- **Kill rule:** if reviewer acceptance < 20% over a quarter, the
  embedding threshold is wrong — recalibrate or stop proposing.

### What runs where (plane discipline, unchanged)

| Role | Payload | Model class |
|---|---|---|
| Channel audit, analogues, elicitation | public structural (titles, channels, public citations) | frontier via egress gateway (new schemas + red-team) |
| Coherence, refutation, adversarial null | public/aggregate structural + approved evidence | local first; frontier only if public |
| Severity reference classes | public web via deep-research caps | local (existing loop) |
| Embeddings for P8 | aggregate register text | local only |
| Synthetic scoring-cell features | aggregate | local-only boundary exercise |

Telemetry: the OMLX choke point and `/costs` page were built for the day
LLM roles activate; the frontier client must join the same choke point
before its first call.

---

## Part C — The gates: how judgment earns (and loses) influence

The first draft gated L1→L2 on AP lift ("Task G-rank") and was rightly
demolished for it (§F): on synthetic regimes the atoms are fictional, so
real-world analogues are confabulation by construction; on R1 there are
4 holdout positives, so AP differences are noise; and a ranking test
cannot gate a level-changing intervention — the exact blindness (T1/E4)
this whole exercise exists to correct. The rebuilt gate structure:

**G-mech — machinery correctness on synthetics (runs now, no real
elicitation).** An **oracle harness** on R0: synthetic judgment sources
with *planted* calibration (a well-calibrated oracle reading
`r0_truth.json`, a storyteller emitting confident noise, a
systematically biased source) are fed through the full conversion →
ledger → weighting pipeline. Pass: the ledger's calibration weights
recover the planted ordering and the fusion machinery upweights the
oracle and freezes the storyteller. This validates the *pipeline* on
regimes where nothing real can be elicited — the legitimate use of
fictional atoms.

**Contamination canary (runs now, cheap).** Run the P2 elicitor on
fictional R0 atoms. Every confident analogue returned is a measured
confabulation; the canary rate is reported next to every real-atom
analogue batch. The first draft treated fictional atoms as a problem;
they are an instrument.

**G-shadow — the L1→L2 gate (prospective, powered, level-based).** L2
runs in **shadow** on the real register: dual realizations computed
monthly, only deterministic published. Gate on the P7 ledger once
pre-registered power exists — minimum **n ≥ 30 resolved claims** in the
cold-start stratum, and the judgment-informed p̂ must beat the
deterministic p̂ on proper scores (Brier/log) with a bootstrap CI
excluding zero. Placebo condition holds throughout: elicitations for
matched placebo pairs (non-edge, frequency-matched, dossier-matched)
must show no systematic mass. Analogue mass and (should it ever be
proposed for fusion) any other signal are gated **separately** — no
promotion of signal A on evidence about signal B.

**G-rank — demoted to a guard.** On R0-family synthetics with oracle
sources: fused ranking must not degrade Task B (non-inferiority, CI-
bounded). It gates nothing upward.

**G-panel — Task D, finally runnable.** The blind human panel (20
promoted vs 20 **dossier-matched** controls, per P4) with the original
pre-registered pass (≥ 0.75 coherence gap, paired p < 0.05). The
context-starved simulated panel stands as the pre-registered negative
control. If humans with dossiers cannot separate either, the
workshop-value claim fails per the original plan — hybrid or not.

**G-cal — the L2→L3 gate.** ≥ 4 quarters of prospective ledger; elicited
priors beat defaults on realized calibration in the cold-start stratum
at the registered n; placebo still clean; then L3 is a *policy*
decision (model inventory update, semver-major), not an automatic one.

**Cost gate (Task F revived; telemetry §3.3).** Cost per promoted
compound and per Brier-point from ledger + telemetry; ceiling pre-agreed
before the first frontier call.

**Timeline honesty.** G-mech and the canary run this month. G-shadow
needs ≥ 2-3 quarters of resolutions after the ledger goes live; L3 is
≥ a year out and may never arrive. That is the design working as
intended: the deterministic path keeps publishing throughout, and every
quarter of delay accumulates exactly the calibration record a validator
will eventually ask for.

---

## Part D — Anti-patterns (pre-rejected, so nobody relitigates them)

| Anti-pattern | Why it dies |
|---|---|
| LLM emits/adjusts any score figure **outside the tagged judgment channel** (p̂, a loss quantile, a correlation, a modifier) | ADR-001; spec §12. Inside the channel, only §7.2-protocol quantile elicitation, converted and bounded by code, dual-realized. |
| Silent fusion (one number, judgment inside) | destroys the dual-realization audit; "score washing" per `loop-design.md` |
| A model scalar deciding publication, promotion, or approval | ADR-013 rejected auto-approve; ingestion confidence scores were user-rejected; P4 gates nothing by design |
| Retrospective LLM backtests as validation | weights contamination; only prospective ledger rows count |
| Unstructured analogue prose ("this feels like 2008") | not reviewable, not countable, not falsifiable — structured object or nothing |
| Bulk-loading analogues/tags/merges without review | violates review-first (ADR-006/013 pattern; and this repo's operating rule: never bulk-load) |
| One combined "AI risk score" in the UI | recreates the urgency-style laundering the whitepaper walls off |
| Ranking evidence used to justify level changes (or vice versa) | the T1/E4 lesson; gates are matched to what the rung changes |

---

## Part E — Sequencing, effort, and versions

| Step | Contents | Effort | New risk |
|---|---|---|---|
| 1 | **A7** production auditability (persistence, identities, history, regime-aware losses) | ~1 wk | none — prerequisite for everything |
| 2 | A3 + A6 (intervals, Task B-cal + Task L, CIs, multi-seed, Task C reporting) | ~1 wk | none |
| 3 | A1 mixture as challenger (+ A4 windows, A5 counts) → promote via B-cal; **semver-major** on promotion | ~1 wk + gate | challenger-only until gated |
| 4 | **Infrastructure:** frontier client through the telemetry choke point; egress schemas + red-team tests; review item types; resolution job | ~2-3 wk | security-boundary work; red-team before first call |
| 5 | P8 semantic hygiene + P6 red-team roles + G-mech oracle harness + contamination canary | ~2 wk | none to scores |
| 6 | P1 channel audit (with its ADR) + P7 ledger + P2/P4 dossiers @ L1 | ~2-3 wk | display/generation only; graduation frozen first enriched quarter |
| 7 | P3/P5 elicitation packets @ shadow; G-shadow accumulates | ~2 wk + ≥ 2-3 quarters | shadow only |
| 8 | G-shadow pass → L2 (bounded, dual-realized, **semver-major**, inventory update) | gated | bounded |
| 9 | ≥ 4 quarters ledger → G-cal → L3 discussion (policy, not automatic) | gated | pooled, calibration-weighted |

Steps 1-6 are unconditionally worth doing — steps 1-3 fix defects the
review demonstrated, and steps 4-6 buy coverage, red-teaming, and
dossiers that pay off even if the judgment channel never earns L2.
Steps 7-9 are earned, not scheduled. The whitepaper's closing sentence
promises that "the falsification criteria for retiring it are already
written down"; this proposal keeps that promise for the hybrid — every
role above ships with the test that kills it, and the gates are matched
to what each rung actually changes.

---

## Part F — Revision record (what the adversarial pass caught in draft 1)

Kept deliberately, in the spirit of §6.3 of the paper under review:

1. Draft 1's A1 ("one-line reparameterization") held the prior center at
   zero support but crushed *observed* rare-pair co-firing (ESS
   c/q_ind ≈ 400 quarters); the claim "both parameterizations agree for
   pairs with real support" was false for rare pairs. Replaced with the
   mixture estimator, challenger-first, plus the supported-rare B-cal
   stratum that would have caught it.
2. Draft 1's L1→L2 gate was AP lift on regimes that cannot host the
   question (fictional atoms / 4 holdout positives) — and a rank test
   for a level intervention. Replaced with G-mech + powered prospective
   G-shadow; rank demoted to a guard.
3. Draft 1's anti-pattern table forbade quantile emission that its own
   P3 mandates; the "as primary" scope now stated in the table, and the
   ADR-001 reconciliation is performed in §0 instead of asserted.
4. P3's prior shift was unbounded (ladder said "bounded"); now clamped
   by a pre-registered δ_max, mirroring the spec's modifier bounds.
5. P2 analogues were keyed on state_hash and would have been orphaned by
   every P1 graph bump; re-keyed on pair_id/risk_ids with quarterly
   graph freezes; success-only pseudo-count mass replaced with
   matched-pair mass.
6. P1 assumed `expert` edges flow to production (they are filtered out
   in `candidates.py`) and put a governed-register mutation behind a
   parenthetical; both fixed (wiring change named; new ADR required).
7. P4's control panel was confounded by dossier thickness; controls are
   now dossier-matched, the lens gates nothing, and the undefined
   "G6-style gate" is gone.
8. Kill rules existed for four of seven items and several could not
   fire (conjunctive conditions, undefined "attempts", unpowered
   zeros); all eight items now carry fireable rules with power
   conditions.
9. Effort table ignored the frontier client, egress schemas, review
   item types, and the resolution job; now step 4, ~2-3 wk, before any
   frontier call.
10. P8 (semantic atom hygiene) had been promised by the companion review
    and omitted; restored. The contamination canary and the G-mech
    oracle harness were added — both convert draft-1 weaknesses
    (fictional atoms, unvalidated fusion plumbing) into instruments.
