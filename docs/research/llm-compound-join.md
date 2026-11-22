# Dark Matter Join — the LLM Compound Join (shadow lane)

**Status:** v1 implemented (playground card DARK MATTER JOIN). Companion to
[`whitepaper/HYBRID_SCORING_PROPOSAL.md`](whitepaper/HYBRID_SCORING_PROPOSAL.md)
— this lives inside its §0 exception: the LLM writes *structure*, deterministic
code converts structure to numbers, humans gate, both realizations stay
auditable. Rung: **L1** (attention) — no contact with any production score.

## The idea

The deterministic join (`compound_join.py`) censors candidate *generation*:
`channel_blocked_pairs` only scores pairs whose atoms share a channel tag —
the visible matter. A missing tag means a pair is **never scored**. The Dark
Matter Join lifts that censorship without touching the scorer:

> **The LLM does the joining; the engine does the scoring; humans dispose.**

1. One LLM call over the full approved inventory (public payload: risk_id, title,
   channel tags — never descriptions, never register prose) proposes pairs
   that share **no** channel tag, each with a named transmission mechanism.
2. Deterministic validation keeps only *dark-matter* pairs (not connected in
   the production channel graph) with all mandatory fields present.
3. The production-fitted `DetScorer` scores each surviving pair — the same
   shadow discipline as `_shadow_coverage`: engine numbers, in-memory only.
4. Persistence, three lanes, none of them production:
   - each join is filed as a pending `edge_add` **channel proposal** (the
     existing P1 review path — it proposes, you dispose; citation URL-clamped,
     `auto_rejected` on unresolvable);
   - every scored join is **pre-registered** in the judgment ledger as a
     `llm_join` claim: the LLM's falsifiable signature plus the frozen
     deterministic forecast (engine p̂, horizon 4 quarters, all_must_fire) —
     frozen *before* the resolving window, resolving automatically on the
     next REALITY CHECK run;
   - a display-only artifact `compound_join_llm.json` next to the risk
     inventory powers the compounds view's DARK MATTER WATCH strip.

**Graduation freeze holds by construction:** nothing here writes
`compound_scenarios`, `score_realizations`, or the deterministic artifact's
`compounds` list. Approving a proposal changes only its review row — the
approved edge then also appears in the deterministic join's WHAT-IF shadow
pass (existing P1 machinery), never in production.

## The falsifiable-signature rule (#5, baked in)

A join without a `signature` — an observable, time-bounded statement of the
form "if this coupling is real, we should see X within N quarters" — is
**rejected at parse time**. `mechanism` and `disanalogy` (the strongest
reason the join might *not* co-materialize, P2's disconfirming-evidence slot)
are equally mandatory. The signature is what the ledger freezes and resolves:
the card is a prospective calibration experiment on the LLM's joining skill,
not a demo. The optional canary runs the same elicitor on a fictional R0
world — every join it returns there is a measured confabulation.

## Kill rules (fireable, house style)

- Reviewers reject > 50% of dark-matter proposals over a quarter → recalibrate
  the prompt or stop proposing.
- After 4 quarters of ledger resolutions, resolved `llm_join` claims show a
  co-fire rate indistinguishable from the pairs' frozen engine p̂ average →
  the joins add no signal beyond the scorer; keep the dossiers, retire the card.
- Canary: any consistent nonzero confabulation rate on fictional atoms →
  freeze the elicitor out of this lane until the cause is found.

## Components

| Piece | Location |
|---|---|
| Orchestration + prompt + validation | `contingency_atlas/compound_join_llm.py` |
| Ledger claim kind `llm_join` + resolution | `contingency_atlas/scoring/judgment_ledger.py` |
| Review proposals (edge_add, pending) | `contingency_atlas/proposals.py` (unchanged store) |
| Job wiring (`compound_join_llm` phase) | `contingency_atlas/web/runner.py` |
| Payload key `llm_join` | `contingency_atlas/web/server.py` |
| Card + run modal + DARK MATTER WATCH strip | `contingency_atlas/web/static/index.html` |
| Tests (MockClient, no network) | `tests/test_compound_join_llm.py` |

---

## Future modes — ToDo (recorded 2026-08-05, not yet scheduled)

Four extensions proposed and accepted in design discussion. Each keeps the
same contract: LLM proposes structure, engine scores, humans dispose,
graduation freeze. The v1 proposal schema was kept extensible for these.

### ToDo #2 — TRIGGER CHAINS (directional, lagged joins)

The engine scores symmetric co-occurrence; reality has fuses and detonators.
Let the LLM propose **directional** joins: "A is the precondition, B detonates
within w quarters" (schema slot: `lag_quarters`, `direction`). Code then tests
the lead-lag claim on the actual signal series — does B fire in the w quarters
*after* A fired, above base rate? (A4 windowed co-occurrence machinery.)
Output: compounds labeled *fuse → detonator* with an empirical lead-lag number
the LLM never touched. Needs: windowed-co-occurrence sensitivity persisted
(A4), a lead-lag statistic with a pre-registered threshold, ledger claim shape
`{direction, lag_quarters}` resolving on lagged co-fire.

### ToDo #3 — BLIND-SPOT BOARD (adversarial inversion)

Flip the objective from max-p̂ to max-regret: prompt the LLM with the current
deterministic top-N and ask "which dismissed or unranked pairing would have
the highest impact *if true*?" Engine scores the answers; the card displays
high-impact/low-attention pairs. The join as a red team — it exists to find
where the deterministic ranking is confidently blind. Needs: an
impact-attention display ordering (never a score), dossier-thickness control
(P4 lesson: matched dossiers, or the raters separate by counting evidence
rows), and the P6 boilerplate spot-audit extended to board entries.

### ToDo #4 — HISTORY RE-CAST (analogues in reverse)

PRECEDENT maps current compounds → historical analogues. Invert it:
start from history's great compounds (housing+liquidity 2008, model+deployment
Knight Capital 2012, Suez+semiconductors 2021) and have the LLM cast *today's
atoms* into those roles; the engine scores the re-cast pair. The outside view
as a generator, not an annotation. Needs: a curated seed list of historical
compound events (cited, date-checked), the P2 mechanism-map as the casting
schema, and the contamination canary extended (a re-cast onto fictional atoms
is a confabulation by construction).

### ToDo #6 — PERFECT STORMS (k=3 triads)

The production join is pairs-only; graduation already computes k-way holdout
survival (`all_must_fire` subset check). Let the LLM propose triads ("the
storm needs all three"), displayed with the engine's pairwise scores and an
explicit honesty label that no k-way joint was scored. Workshop gold, zero
score-path contact. Needs: display schema for k=3, the k-way scoring question
deferred until the common-factor estimator (A2) exists — never pairwise-max
masquerading as a triad score.
