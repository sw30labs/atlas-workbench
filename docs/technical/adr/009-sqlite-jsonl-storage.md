# ADR-009: SQLite + JSON/JSONL storage; no DuckDB yet

- **Status:** accepted
- **Date:** 2026-08-01

## Context

The redesign data model sketched Parquet + DuckDB for local synthetic
analytics. The current scale: tens of scenarios, ~16 risk atoms, ~120
pairs, hundreds of events per run.

## Decision

All stores are SQLite (`atlas.db`, `event_evidence.db`) plus JSON/JSONL
files (`emerging_risks.json`, `compound_scenarios.json`,
`generated/telemetry/*.jsonl`). Migrations are additive and idempotent.
No DuckDB dependency is introduced at this scale.

## Consequences

- Zero extra dependencies; databases bootstrap on first run without
  committed binaries.
- JSONL telemetry is append-friendly, greppable, and trivially
  aggregated (the Costs page reads it directly).
- If cell-level analytics grow by orders of magnitude, DuckDB can be added at
  the feature-store layer without touching the transaction stores.

## Alternatives considered

- **DuckDB now** — rejected: analytic workload doesn't exist yet at
  meaningful volume (documented deviation in the redesign decision log).
- **Postgres** — rejected for the single-operator phase; revisit
  for multi-user deployment.
