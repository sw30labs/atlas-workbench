# ADR-002: Data boundary with an egress gateway enforcement point

- **Status:** accepted
- **Date:** 2026-08-01

## Context

The scoring demo deliberately contains local-detail rows and planted prose
that must never reach external models. "Be careful with prompts" is a
convention, not a control; the boundary needs an executable policy.

## Decision

Data is classified as `public`, `aggregate`, or `local_detail`. The
**only** sanctioned path to external compute is the egress gateway
(`scoring/egress.py`): allow-list schemas per payload type, deny scans
(forbidden free-text keys, email-like tokens, account-like digit runs,
overlong prose), and a **hash-chained audit log** of every request with
the payload hash, decision, and caller. Red-team tests throw synthetic
local-detail content at it (deny required); `verify_audit_chain()` detects
tampering.

## Consequences

- Frontier models can never receive local-detail rows by design;
  admitted payloads are allow-listed structural codes, numeric features,
  hashes, or public references.
- Local inference remains the default. If an operator explicitly opts into a
  non-loopback LLM endpoint, the scoring-lane channel audit, analogue
  elicitation, and refutation memo request bodies must pass the gateway before
  their client transport is invoked; denied attempts emit telemetry and stop.
- Cost of the boundary: frontier models get public references and approved
  abstract structures, not local-detail prose. Accepted.

## Alternatives considered

- **Guideline-only ("don't paste local detail")** — rejected; humans
  with browsers exist, technical control is necessary (not sufficient,
  but necessary).
- **DLP at the network layer** — complementary, not a substitute for
  schema-level enforcement at the application boundary.
