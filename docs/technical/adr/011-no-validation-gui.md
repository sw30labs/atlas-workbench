# ADR-011: Validation as one-off gate; no validation GUI page

- **Status:** accepted
- **Date:** 2026-08-01

## Context

The evaluation harness (Task A/B/C, ablations, calibration) produces
reports. A GUI page was proposed to display them.

## Decision

No validation dashboard page. Algorithm validation is a **one-off gate
per algorithm version** (re-run on algorithm change, not an operational
view). The record lives in eval artifacts
(`generated/**/eval_report.*`, `ablations.*`, `calibration_report.*`),
the redesign decision log, and `docs/redesignv1/model-inventory.yaml`.

## Consequences

- No maintenance cost for a page read ~twice a year.
- The falsification story stays close to the artifacts that prove it
  (regenerable from a single command), not to a UI that can drift from
  them.
- The operational GUI keeps only per-item, recurring views (Compounds,
  Costs).

## Alternatives considered

- **Validation page** — rejected by owner decision ("does not merit a
  visualization option").
