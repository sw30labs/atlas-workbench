# ADR-005: Automatic graduation with deterministic eligibility and LLM prose

- **Status:** accepted
- **Date:** 2026-08-01

## Context

When a compound scenario keeps proving itself, it should become a full
library scenario. The open question was whether promotion needs a human
step and whether an LLM may participate.

## Decision

Graduation (Phase 8) is automatic, with a strict split of labor:

- **Eligibility is deterministic**: the compound must be in the join's
  top-N output **and** its risk pair must have co-occurred in the
  holdout quarters after the scoring window. In-sample-only compounds
  never graduate. Idempotent via the `compound_graduations` ledger.
- **The LLM only writes the document** (title, transmission, options,
  signposts) from the structural context. It never decides what
  graduates and never touches numbers.
- **Score fields come from the stored realization** (p̂ band, q95 band),
  inserted by code.
- **The Phase 3 QA judge gates the document** at ≥ 0.75; a reject
  blocks insertion.

## Consequences

- No human promotion step is required for the mechanical case; humans
  still review via the normal library QA and maintenance flows.
- Every graduated scenario persists `origin=compound_graduation` plus its
  `compound_id` and `state_hash`; `ATLAS-CMP-*` remains only the display-ID
  format. Consumers never infer provenance from that prefix.
- Register rot is bounded by the holdout-survival requirement and the
  judge gate, not by manual review volume.

## Alternatives considered

- **Human curation per promotion** — the owner's explicit call was to
  automate; guardrails above are the substitute.
- **LLM decides eligibility** — rejected: invented confidence is not
  evidence; eligibility must be computable from data.
