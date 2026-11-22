# ADR-014: Register merges and proposal-only review item types

- **Status:** accepted
- **Date:** 2026-08-03

## Context

P8 of the hybrid-scoring program
(`docs/research/whitepaper/HYBRID_SCORING_PROPOSAL.md`) names the defect: duplicate
risk-register atoms under different names convert shared evidence into
fake co-occurrence — the exact quantity the compound scorer sells.
Identity today is exact/slugified string matching, so nothing can even
*see* a duplicate. Fixing that requires two things ADR-013 deliberately
did not authorize:

1. **A second register-mutation path.** Until now the governed risk
   register (`emerging_risks.json`) changed only through the
   evidence/risk decision functions (two human attestations) and the
   narrow adopt-refs flow. A merge retires one record and edits another
   — a mutation class of its own.
2. **Review-queue item types that are proposals, not records.** Merge
   proposals are machine-generated pairs awaiting a human disposition.
   ADR-013's rule ("machine proposes, human disposes; decision functions
   are the only write path") covers the pattern, but the item type is
   new and deserves its own ratification — the proposal's P1 channel
   enrichment called this "not a parenthetical decision."

## Decision

**(a) The second register-mutation path is ratified for approved merges
only.** A merge is applied by `review.approve_merge` →
`dedup.apply_merge`: single named reviewer with a non-empty written
reason, the whole load→save span under `register_lock`, tmp+rename
atomic write, provenance stamps on both records, and the before/after
diff stored on the proposal row. The retired record is never deleted —
it is marked `superseded` with a `merged_into` pointer, so history
survives and the merge is idempotent (re-application is a recorded
no-op). The keep/drop direction is deterministic (more evidence_refs
survives; ties break on earlier first_seen, then title) so it is
reconstructable from the register, not from a proposal author's whim.
Citations and evidence keep the existing two-attestation flow untouched
— a merge cannot approve evidence, and merged records still need their
evidence refs to resolve through the normal path before the surviving
risk can be (re-)approved.

**(b) Proposal-only review item types are ratified as ADR-013-consistent.**
Machinery may rank candidates and store `pending` rows
(`merge_proposals` now; `channel_proposals` and analogue dossiers are
the wave-5 follow-ons on the same contract). The decision functions in
`review.py` remain the only write path to `approved`/`rejected`.
Optional LLM advisory verdicts may be stored on the proposal row as
display-only advisory JSON — the human gate never reads them, the UI
labels them as machine output, and an absent or failed advisory blocks
nothing. The merge acceptance rate (approved / decided) is surfaced in
the review queue as the P8 kill-rule instrument (<20% over a quarter ⇒
recalibrate or stop proposing); it is reported, never enforced.

**(c) Explicit deferral.** This ADR does NOT authorize: P1/P2 register
or graph mutation (channel tags, expert edges, analogue-driven priors),
L2/L3 judgment-channel rights (prior mass, pooled p̂), and any amendment
of ADR-001 (no LLM produces or adjusts a score figure). Those remain out
of program; activating them requires their own ADRs and the gates the
proposal defines.

## Consequences

- Duplicate atoms can now be retired through a human-gated, auditable
  path; the co-occurrence rebuild and re-score ride the next scheduled
  join (A7's realization history keeps the before/after).
- The review queue gains a third item kind whose "validation report" is
  informational (similarity components), not an approval gate — merge
  decisions are pure human judgment with a written reason.
- Two new stores (`merge_proposals`, plus the display-only
  `refutation_memos` used by the P6 red-team tooling) are added; all are
  auditable, none is a score input.
- `approved_synthetic` demo records stay immutable through this path,
  same as every other review workflow.

## Alternatives considered

- **Machine auto-merge above a similarity threshold** — rejected
  outright: identity errors are exactly the failure mode P8 exists to
  remove; a model scalar deciding register identity is the ADR-013
  anti-pattern.
- **Delete the duplicate record** — rejected: deletion destroys the
  audit trail and makes a bad merge unrecoverable; the `superseded`
  pointer keeps both records and the provenance chain intact.
- **Two-reviewer gate for merges** — rejected as disproportionate:
  merges are reversible-by-inspection (the retired record keeps its
  full content), the written reason + provenance stamp + stored diff
  give the second pair of eyes asynchronously, and the acceptance-rate
  instrument watches for rubber-stamping at the population level.
  Evidence admission keeps the stricter two-attestation gate because
  citations feed the governed corpus directly.
