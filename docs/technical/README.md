# Contingency Atlas — Technical Documentation

**Audience:** engineers, model validators, and risk sponsors.
**Scope:** the system as it exists in `main` today — the eight-phase
operational-risk pipeline, the deterministic compound-scenario scoring
engine, artifact ingestion, the review queue with advisory machine
assistance and deep research, telemetry, and the local dashboard. The
aspirational redesign program that produced the engine is documented
separately in [`docs/redesignv1/`](../redesignv1/README.md); this set
describes *what is built and running*, including what was tried and
deliberately removed.

## Document index

| Document | Contents |
|----------|----------|
| [architecture.md](architecture.md) | System context, components, planes, data flow, technology choices |
| [dfd.md](dfd.md) | Data flow diagrams (context level + decomposition) |
| [erd.md](erd.md) | Entity-relationship model for all stores |
| [design.md](design.md) | Scoring engine internals: formulas, estimators, Monte Carlo, EVT, hashing |
| [evaluation-analysis.md](evaluation-analysis.md) | The falsification record: Task A/B/C, ablations, calibration, kill criteria status |
| [api.md](api.md) | HTTP API reference for the dashboard server and CLIs |
| [review-and-research.md](review-and-research.md) | The human boundary in the browser: validation reports, decisions, register lock, advisory pre-analysis, deep research, adopt-refs |
| [operations.md](operations.md) | Setup, runbooks, cadence, configuration, troubleshooting |
| [security.md](security.md) | Data classes, egress gateway, local-detail boundary, network posture |
| [testing.md](testing.md) | Test suite layout, fixtures, coverage map, known flake |
| [adr/](adr/) | Architecture Decision Records (ADR-001 … ADR-017) |
| [glossary.md](glossary.md) | Controlled vocabulary used across the system |

## Reading order

1. [architecture.md](architecture.md) for the shape of the system.
2. [design.md](design.md) for how scenarios are scored.
3. [evaluation-analysis.md](evaluation-analysis.md) for the evidence that
   the scoring is worth anything (and where it is not proven).
4. [operations.md](operations.md) to run it.
5. ADRs as needed when a decision looks odd — each oddity has a record.

## System in one paragraph

Contingency Atlas monitors public sources for emerging banking
operational-risk signals, maintains a scenario library, and — monthly —
joins the risk catalog into compound scenarios (pairs of emerging risks
that could co-materialize), scored by a deterministic hybrid
frequency × severity engine with dependence estimated from observed
co-occurrence. Nothing influences synthesis without human-attested
evidence: pending records queue in the dashboard's Review card, where
deterministic validation, an advisory LLM pre-analysis, and a bounded
deep-research loop assist — but never replace — the reviewer
(ADR-013). Every score is reproducible from stored components; every
LLM call is telemetry-instrumented; local-detail content never crosses the
egress gateway. An optional MITRE ATLAS catalog can add pinned,
human-reviewed, display-only AI-adversary context to scenarios without
entering the score path (ADR-016). The engine's claims are falsifiable and have
been tested against pre-registered criteria on synthetic regimes. The public
package exposes only synthetic scoring demonstrations and public evidence
inputs; it has no organizational event-extract adapter.
