# ADR-019: Regulatory regime mismatch is intake, signposts, and workshop display — not a score

- **Status:** proposed
- **Date:** 2026-08-30
- **Does not reverse:** ADR-001, ADR-004, ADR-006, ADR-014, ADR-016, ADR-017
- **Narrows under:** ADR-018 (no k-way `p̂`, compounds never nodes)
- **Does not bump:** `atlas-1.0` categories or `CHANNELS`

## Context

A global FS operator cannot assume one model stack. Public-law clocks
(years), supervisor guidelines (quarters), and open-weight releases (days)
do not match. The same checkpoint can be a GPAI model if its output is used
in the Union, a PRA “model” in London, a download in Singapore, and an
EAR-sensitive inference path if prompts carry controlled technical data.
That is **regime mismatch**: dual or triple control stacks, not a single
group LLM.

What is **not** the current public record (research snapshot, 2026-08-30;
enacted vs proposed labelled):

- The United States has **not** enacted a ban or license on open-weight
  publication. America’s AI Action Plan (Jul 2025) encourages open-weight
  US models. EO 14409 (Jun 2026) disclaims mandatory licensing of model
  release. The Jan 2025 BIS Diffusion IFR licensed **closed** weights at
  ≥10^26 training ops; Commerce announced non-enforcement (May 2025); the
  text remains in the CFR, unenforced (GAO B-337935, May 2026). **Compute**,
  not published weights, is the binding export chokepoint.
- State frontier statutes (CA SB 53 in force Jan 2026; NY RAISE from Jan
  2027; IL SB 315 audits from 2028) are **developer disclosure / incident /
  audit** duties above ~10^26 FLOPs and ~$500M revenue, not a bank ban on
  possessing Llama-class weights. Texas TRAIGA is a different target
  (intent-based harms), not weight licensing.
- US bank MRM (SR 26-2 / OCC 2026-13 / FDIC FIL, Apr 2026) **carves
  generative and agentic AI out of scope** and promises an RFI. TPRM,
  GLBA/Reg S-P, and NYDFS Part 500 still bite on **data and vendors**.
- EU AI Act (Reg. 2024/1689 as amended 2026/1744) is extra-territorial
  where **output is used in the Union**. GPAI duties apply from Aug 2025
  (enforcement Aug 2026). FOSS GPAI is a **partial** carve-out; systemic-risk
  models do not get it. Annex III high-risk (credit etc.) was deferred to
  Dec 2027. UK has no horizontal AI Act (SS1/23 + Consumer Duty). Singapore
  MAS AI risk guidelines remain consultation as of Aug 2026.
- PRC-origin open weights can be MIT/Apache *and* still fail an EU/UK
  **licence territory** (e.g. Hunyuan community licence excludes EU/UK/SK),
  Entity List screening, and PRC outbound-data rules if China-book prompts
  leave the mainland.

The catalog already occupies this cell. Do not mint a twin atom named
after the metaphor:

- `open-weight-model-frontier-parity-and-distillation-disputes`
- `production-dependence-on-prc-origin-open-weight-models-under-regulatory-scrutiny`
- `domestic-frontier-ai-vendor-supply-chain-designation`
- existing k=2 compounds among those atoms (`compound_scenarios.json`)
- rejected `chip-export-policy-volatility-and-smuggling-exposure` — reopen
  only via ADR-014, do not resurrect in this ADR
- pending surveillance / copyright intake — ADR-017: not scorable

Taxonomy already has the lenses: category `Legal and regulatory` /
`Geopolitical disruption` / `Financial crime controls` / `Third-party
dependency`; overlay `Institutional change`; channels `legal`,
`geopolitical`, `model`, `third-party`. Keyword tagging already routes
`regulat` and `export control` into the pair join. A new CHANNEL or a
“hostage” RiskAtom would churn the graph hash and duplicate those pairs.

A bill is not an `IncidentEvent` in a quarterly occurrence series. Scoring
“Congress intends to restrict open-source models” as `p̂` is a news-volume
process, not `all_must_fire`. “EU Act AND Texas statute AND BIS 4E091” is
a k-way joint ADR-018 forbids as production. Extra-territorial reach and
forum-shopping are often **OR / option-value**, sold as AND.

Nothing in this product is legal or regulatory advice (`DISCLAIMER.md`).

## Decision

**Regime mismatch is a claim kind, not a new object class and not a
number.** Research may track public instruments and operating-stack
incompatibility. The engine does not emit a “regulatory hostage” `p̂` or
urgency index.

1. **Reuse existing vocabulary.** Map operator sentences onto
   `Institutional change`, `Legal and regulatory`, `Geopolitical
   disruption`, channels `legal` / `geopolitical` / `model`, signposts, and
   `strategic_options` (dual-stack, region routing, licence-territory).
   No new category, driver, cause, or `CHANNELS` member in this ADR.

2. **Evidence hygiene (the only schema seam).** Intake evidence records
   that are public legal instruments MUST carry
   `instrument_status ∈ {proposed, enacted, enforcement, vacated}`
   (fail closed if missing on `source_type=regulatory_release`).
   Tighten meaning: `regulatory_release` is an official instrument from
   the issuer, not a hearing Q&A or a politician’s “intent.” Secondary
   reporting of a proposal stays `public_secondary` and `proposed`.
   Implementation is a follow-on change to intake/schema; this ADR is
   the decision, not the patch.

   | Status | May support | Must not |
   |---|---|---|
   | proposed | strengthening signpost | fire a scoring quarter; maturity `materialized`; any `p̂` |
   | enacted | evidence for a legal/regulatory hypothesis | a multi-geo joint `p̂` |
   | enforcement | operational transmission (designation, consent order) | proof every jurisdiction copies it |
   | vacated | weakening signpost | erase the history of the hypothesis |

3. **Signposts on existing scenarios, not new atoms.** Strengthening:
   enrolled act, final rule, entity listing, extra-territorial guidance,
   vendor ToS geographic cut. Weakening: bill dies, injunction, license
   exception, regime divergence that **preserves** an operating option.
   Option-expiry: last window to dual-source weights or pin inference
   region. `materialized` remains a human assessment against approved
   **primary** evidence — filing a bill is not materialization of the
   banking disruption.

4. **Workshop display under ADR-018.** Nominated multi-geo wedges
   (e.g. open-weight atom × PRC-scrutiny atom) may show the **pairwise**
   engine scores that already exist, labelled **no k-way joint was
   scored**. Dual-stack / relaxed-vs-heavy regime advantage lives in
   `strategic_options` and Atlas Brief prompts. No Briefing KPI, no
   “top hostage storms.”

5. **Not legal advice.** Foresight may not opine on extra-territorial
   reach, “are we allowed to host this checkpoint in Dublin,” or
   compliance posture. Those sentences belong to qualified counsel.

6. **Do not resurrect rejected atoms** and do not mint duplicates of the
   open-weight / PRC-scrutiny / designation rows. Near-duplicates go
   through ADR-014.

## Consequences

- The interesting operating fact (you cannot run one stack globally)
  becomes sayable without a fake joint. The catalog atoms and pair
  gates already on Briefing cascade are the geometry.
- Pending bills stay in Review (ADR-017) until attested. They cannot
  move generation, scoring, or scenario-count recommendations.
- Taxonomy and channel graph hashes do not churn. P1 `new_channel`
  remains shadow-only (ADR-014(c)).
- Follow-on engineering: `instrument_status` on evidence rows, UI fail
  closed, export/API presence. Not this ADR’s patch.
- Overlay of public instrument identifiers (jurisdiction, instrument
  id, status, URL), if wanted later, is an ADR-016-style **non-scoring**
  overlay — a separate ADR.

## Kill rules

1. No `p̂` on unenacted bills, drafts, hearings, letters, or “intent.”
2. Regulatory evidence rows require `instrument_status`. UI/export fail
   closed if missing on `regulatory_release`.
3. Pending ≠ materialized. Pending/rejected records never enter join,
   graduation, urgency, scenario planning, or workshop ranking.
4. No catalog writeback of compounds. “Regulatory hostage” is not a
   `RiskAtom`.
5. No k≠2 row and no joint `p̂` for “EU AND Texas AND BIS.”
6. No new urgency number, hostage index, or LLM modifier in the score
   path (ADR-001).
7. No new category/channel/driver in this ADR.
8. No resurrection of rejected export-control atoms without ADR-014.
9. If a sentence would be malpractice in a memo to general counsel, it
   does not belong in a score blob or Briefing KPI.
10. Headline volume is not `dependence_support`. Empty-N_S / news-only
    diagnostics must not outrank data-supported pairs.
11. If A2 or k-way is mentioned in UI copy, the sentence must say it is
    not implemented.

## Alternatives considered

- **New RiskAtom “regulatory hostage”** — rejected. Duplicate of existing
  open-weight / PRC-scrutiny / designation atoms; would fake co-occurrence
  through shared packets (ADR-014 / P8).
- **New CHANNEL `regime` or `jurisdiction`** — rejected. Graph-hash churn;
  no R0 generative truth; reverses ADR-014(c) without a B-cal program.
- **Scored triad EU × Texas × BIS** — rejected. ADR-018; wrong occurrence
  definition (enactment calendars are not quarterly Bernoullis); often OR
  sold as AND.
- **“Regulatory intensity” modifier on urgency** — rejected. Second score
  path, not reproducible from `(state, algorithm_version, seed)`.
- **Wait for a federal open-weight ban before tracking this** — rejected
  for **intake and signposts**. The dual-stack problem is already enacted
  in the EU extra-territorial GPAI clock, US compute export controls, PRC
  data/licence-territory rules, and CA/NY/IL developer statutes. Waiting
  for a ban that policy of record currently disclaims would hide the
  mismatch we can already name without a number.

## Residual risks

- Operators will still read a wedge of AND-gates as a legal conclusion.
  Caption and kill rule 9 exist because this will happen anyway.
- `instrument_status` without the follow-on patch is a paper decision.
  Do not claim the field exists in production until the intake PR lands.
- Secondary press about “intent” will keep arriving. Scout lead only.
- Closed US frontier models remain **revocable by ToS / export cutoff**
  on a days clock. That is a vendor-concentration signpost on existing
  third-party atoms, not a new class.
