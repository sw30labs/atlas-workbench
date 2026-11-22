# ADR-004: Compound scenarios as a separate object class

- **Status:** accepted
- **Date:** 2026-08-01

## Context

Compound scenarios (pairs of emerging risks with engine-derived scores)
resemble both the legacy scenario library and the risk catalog. A wrong
home would either pollute the curated library or explode the catalog
combinatorially (16 atoms → 120 pairs).

## Decision

Compound scenarios are a **separate object class** with their own store
(`compound_scenarios` table + JSON artifact) and their own page
(Compounds view). They are **not** emerging risks (no catalog writeback)
and **not** library scenarios (no urgency mapping, no phase-3
semantics). Entry into the library happens only through graduation
(ADR-005), which creates a properly-marked library row.

## Consequences

- The legacy urgency scale and the compound p̂ are never conflated —
  different objects, different meanings, different stores.
- The catalog stays atomic; compounds reference atoms by id.
- Compound rows carry research-quantity provenance notes and uncertainty
  classes explicitly.

## Alternatives considered

- **Write compounds into the scenario library directly** — rejected:
  mixes derived hypotheses with curated scenarios; graduation exists
  precisely as the gated bridge.
- **Promote compounds to catalog risks** — rejected: combinatorial
  explosion and meaningless lifecycle semantics for pairs.
