# ADR-013: Advisory-only machine assistance at the review boundary

- **Status:** accepted
- **Date:** 2026-08-02

## Context

The human review boundary is the system's core invariant: nothing
influences the governed corpus without human-attested evidence. It is
also the workflow's slowest step, and the CLI made it opaque —
validation failures surfaced only as exceptions after a decision was
attempted, and evidence-poor records left the reviewer to do manual
web research. Adding LLM help at exactly this boundary risks the
boundary degrading into a rubber stamp: if the machine pre-suggests a
decision, the human tends to click it.

## Decision

Machine assistance at the review boundary is **advisory-only and
proposal-only**, with three enforced properties:

1. **No machine writes to review state.** The pre-analysis
   (`review_assist.py`) writes only a suggestion cache; deep research
   (`research.py`) writes only `pending` evidence candidates
   (provenance-stamped `origin: deep_research`) and a memo. The four
   decision functions in `review.py` remain the only path to
   `approved`/`rejected`, and they require the same explicit human
   attestations from every front end.
2. **The UI cannot pre-commit the human.** Suggestions render in
   clearly labeled panels, never pre-select a decision, and never
   pre-tick the attestation checkboxes. The model is prompted that it
   cannot attest public access or reuse rights and must list them
   under `human_must_verify`.
3. **Drift is measured.** Every suggestion is stored next to the
   eventual human decision; the card surfaces the agreement ratio. A
   reviewer who always agrees with the machine is visible in data
   ("if you always agree, you have stopped reviewing").

Deep research additionally constrains the machine's reach: the model
may only select URLs the search step actually returned; every fetch
hop must be a public HTTP(S) URL; page/byte/iteration budgets are hard
caps; and "no credible evidence found" is a first-class verdict so the
loop is never pressured to fabricate support.

## Consequences

- The review boundary keeps its meaning: SEC review's acceptance of
  automatic compound graduation rests on approved-only inputs, which
  rests on real human review — now supported, not replaced.
- Reviewer time shifts from mechanical checking (done by the
  deterministic reports) to judgment.
- Two new advisory stores (`review_suggestions`, `review_research`)
  and one governed register mutation (`adopt-refs`, approved-evidence
  ids only, under the register lock) are added; all are auditable.
- The dashboard gains outbound web traffic (search + capped page
  fetches) — see `security.md` §7 for the posture.

## Alternatives considered

- **Auto-approve above a confidence threshold** — rejected outright:
  converts the human boundary into a model parameter.
- **Pre-ticked attestations when the machine recommends approve** —
  rejected: the attestations are legal/human claims the model cannot
  make; friction there is the control.
- **Research writing approved evidence directly** ("the model checked
  the source") — rejected: provenance review includes rights and
  access judgments outside the model's competence, and it would make
  the corpus self-sourcing without human admission.
