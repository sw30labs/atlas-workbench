# ADR-018: Hub-fire is first-hop AND-gates; Perfect Storms stay workshop; compounds are never nodes

- **Status:** proposed
- **Date:** 2026-08-30
- **Does not reverse:** ADR-001, ADR-003, ADR-004, ADR-005
- **Narrows:** `docs/research/llm-compound-join.md` ToDo #6 (Perfect Storms)
- **Defers:** proposal §A2 common-factor k-way estimator; ToDo #2 trigger chains

## Context

The operator question is a daisy-chain of named, underpriced couplings: A+B
generate C, D+E generate F, C+F generate H — average-looking atoms
co-materialize and the impact compounds.

What the system actually has:

- Production join is **channel-blocked unordered pairs** (`compound_join.py`,
  `channel_blocked_pairs`). Live `compound_scenarios` rows are all k=2.
- `DetScorer.score_combo` accepts k≥2, but `joint_quarterly_combo` for k≥3
  uses only the k-way count and the independence product, Fréchet-clipped to
  `min q_i`. Pairwise joints are computed and unused (whitepaper T6).
- Proposal §A2 (channel-shock common-factor k-way) is **not implemented**.
- ADR-004: compounds are a separate object class. They are not catalog atoms
  and not library scenarios until graduation.
- Graduation holdout is k-agnostic (`all_must_fire` subset of a holdout
  quarter). There is **no `k==2` guard**. The first k≠2 row in
  `compound_scenarios` is graduation-eligible under today's code.
- Dark Matter Join and P1 shadow freeze by construction: no writes to
  `compound_scenarios` / `score_realizations`.
- ToDo #6 already named the kill: never pairwise-max masquerading as a triad
  score; k-way deferred until A2.
- The Briefing **AND cascade** (one hop, hub fire) is the honest diagram of
  the pair graph we have: atoms on the ring, each scored pair an AND-gate,
  pulse = hub fire. It is not a second-hop net.

Two readings of “C+F generate H” are being smuggled as one idea:

1. **Shared-atom wedge** — pairs (A,H) and (B,H) already exist. When H fires,
   both AND-gates light. That is first-hop hub fire. It is in the data.
2. **Compound-of-compound** — C and F become joinable nodes. That reverses
   ADR-004, has no signal series, and any algebra on `p̂_C` and `p̂_F`
   (product / min / max) is not a joint.

## Decision

**Compounds are never nodes.** A pair or triad is not written to the risk
catalog, is not a `RiskAtom`, and is not a legal member of another compound's
`risk_ids`. Graduation still emits a library scenario; that row does not
re-enter the join. Daisy-chains that share an atom are shown as hub fire.
Disjoint pair-unions (literal C+F→H with no shared atom) are **out of scope**
— that is k=4, a later ADR, still without compound nodes.

**No published k-way `p̂` in this ADR.** `score_combo(k≥3)` is T6, not A2.
Pairwise-max, hop-product, and Fréchet-bound-as-`p̂` are forbidden conversions.
A tree-of-pairs prior (chain conditionals through the hub) may be explored as
a **workshop diagnostic** with an honesty flag; it is not a production score
and must not land in `compound_scenarios` / `score_realizations`.

**Perfect Storms (k=3) are workshop / L1 only**, as ToDo #6 specified:

- Nominated triads (operator or LLM) display the **pairwise** engine scores
  that already exist, plus the mandatory label **no k-way joint was scored**.
- Writes: display artifact and/or pending proposals. Graduation freeze holds
  by construction — same contract as Dark Matter Join.
- Generation, if any, is channel-graph **wedges** (hub + two neighbors),
  never `C(n,3)`. Cap scored diagnostics. Do not mix k=2 and k=3 in one top-N.

**`eligible_compounds` must require `len(risk_ids)==2`** before any k≠2 row
can exist. The A7 k=3 tests become negative tests (triple in table ⇒ not
eligible). Re-open k≥3 eligibility only under a later ADR that names the
estimator hash and a holdout definition that is not “all k in one of two
quarters.”

**A2 remains the specified production k-way estimator** and is not this
release. Sequence: A2 challenger on R0 → Task B-cal with a k=3 stratum
against generative truth → only then a production k-way `p̂` as a separate
TOP-N. Even then, A2's own text says R1 is pair-starved.

**Briefing AND-cascade is the hub-fire skin for this ADR:**

| May show | Must not show |
|---|---|
| Catalog atoms as ring neurons | A pair drawn as a vertex that joins another pair |
| Stored Phase-7 pairs as AND-gates (engine `p̂`, interval, N_S) | A numeric `p̂` on a triad or on “H” |
| Hub fire: when atom H lands, every **pair** containing H lights | Ranking “top storms” on Briefing KPIs |
| Caption: one hop, not a triad score, not a temporal fuse | Same-quarter AND drawn as fuse→detonator (ToDo #2 is not this) |

Timeshift on that canvas is cited evidence on member signals, not
reconstructed urgency.

Keep the vocabulary of a *named, underpriced, coupled* shock. Do not relabel
a k=3 compound as an unforeseen event; the atoms are already on the register.

## Consequences

- ADR-004 holds. The catalog stays atomic. Combinatorial cost stays
  `TOP_N` pairs, not `C(n,3)` and not `C(compounds, 2)`.
- The interesting object (the joint that has not happened yet) remains
  visible as **hub degree** on the cascade, without pretending we scored it.
- The graduation landmine closes: k≠2 cannot silently enter the library
  with a T6 number.
- T6 is disclosed, not papered over. Operators who want a triad `p̂` wait
  for A2 + B-cal, or look at three labeled pairwise scores.
- Rollback of any later workshop strip: delete the artifact; pair join and
  the cascade are unchanged.

## Kill rules

1. No k≠2 row in `compound_scenarios` or `score_realizations`.
2. `eligible_compounds` requires `k==2` until a later ADR names the estimator.
3. Any UI number on a triad other than the pairwise `p̂`s, clearly labeled
   as pairwise, kills the card.
4. Pairwise-max, hop-independence product, and Fréchet-bound-as-`p̂` do not
   exist as conversion helpers in join / web payload builders.
5. No catalog writeback. Join / LLM / graduation never insert into
   `emerging_risks.json`.
6. Dark Matter remains pairs. A triad elicitor is a new prompt version,
   new canary, new claim kind — or it does not ship.
7. If A2 is mentioned in UI copy, the sentence must say it is not implemented.
8. Briefing cascade copy keeps the honesty sentence (one hop, no k=3 score).
   Snapshot / asset tests fail if it disappears.
9. Empty-N_S k-way diagnostics must not outrank data-supported pairs in any
   operator-facing sort.

## Alternatives considered

- **Compound-of-compound object class** — rejected. Reverses ADR-004.
  C has no signal series, no channels, no `evidence_refs`. Synthesizing
  C as A∩B then scoring (C,F) is a 4-way count in costume.
- **Ship T6 `score_combo(k=3)` as the triad `p̂`** — rejected. That is the
  independence product (plus a zero-count shrink) in the cell the paper
  says is systematically wrong.
- **Pairwise-max / min / product as the storm** — rejected. Max is OR.
  Min is a Fréchet upper bound sold as a point. Product of annual pair
  `p̂`s is not P(all fire in one quarter).
- **Tree-of-pairs as production estimator in this ADR** — deferred. Honest
  as a workshop diagnostic (hub conditionals, N_ijk=0 ⇒ mixed, not
  statistical). Not honest enough to persist as `p_hat` without B-cal
  against planted triples, and R0's generator is a common-factor shock, so
  a hub-tree may win by construction.
- **Wait for A2 before any diagram** — rejected for the **skin**. The pair
  graph and hub fire are already in the live join. Waiting would hide the
  coupled-atom geometry we can name without a new score.
- **Unrestricted C(n,3) batch / progressive widening** — rejected. ADR-003
  already dropped the loop. Completeness theatre.

## Residual risks

- Operators will see a wedge of two AND-gates and infer a scored storm.
  Caption and kill rule 8 exist because this will happen anyway.
- Disjoint daisy-chains cannot be said inside this model. If that is the
  actual product ask, this ADR is the wrong object — open a k=4 **atom-set**
  ADR after A2, still without compound nodes.
- R1 pairwise joints are themselves mixed. Any later tree diagnostic
  inherits that.
- ToDo #2 (directional lag) is a different occurrence definition. Do not
  let hub fire be read as fuse→detonator.
