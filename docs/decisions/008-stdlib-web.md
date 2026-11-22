# ADR-008: Stdlib-only web dashboard, no framework

- **Status:** accepted (pre-existing, reaffirmed)
- **Date:** 2026-08-01 (reaffirmed)

## Context

The dashboard serves a single-operator local use case: situation room,
scenario/loss/risk views, pipeline control, compounds, costs, token
charts, and a background job runner.

## Decision

The web layer is Python stdlib `http.server` only — hand-rolled JSON
handlers, static HTML/JS with hand-drawn canvas charts, loopback-only
binding with host-header checks, and no JavaScript framework or build
step. POSTs are JSON-only with size caps; mutation requests require a
loopback origin.

## Consequences

- Zero web dependencies, trivial audit surface, and no supply-chain or
  version-churn exposure in the UI.
- Charts are hand-drawn but fully controlled (hover, animation,
  theming) — adequate at this scale.
- The pattern is proven: the multiverse console was dropped without any
  framework teardown cost.

## Alternatives considered

- **FastAPI/Flask + React** — rejected for this use case: a framework
  and a build pipeline to serve one local operator is negative value.
