# Atlas Workbench — Redesign Pack (v1)

**Status:** design and specification only. No implementation commitment.  
**Audience:** pod lead + engineers; model validation and risk sponsors as secondary readers.  
**Date:** 2026-08-01  
**Branch context:** `feature/nc/karpathy-autoresearch-emerging-risks`

This pack redesigns Atlas Workbench from a public-signal research pipeline into an AI-native **operational-risk scenario analysis engine**: combinatorial compound scenarios, hybrid scoring evaluated on public and synthetic signals, multiverse search with checkpoints, policy-controlled model routing, and a falsification harness.

## Documents

| # | File | Contents |
|---|------|----------|
| 1 | [architecture.md](./architecture.md) | Components, data flow, confidentiality split, tech choices |
| 2 | [algorithm-spec.md](./algorithm-spec.md) | Hybrid scoring, uncertainty, EVT, calibration, falsification |
| 3 | [loop-design.md](./loop-design.md) | Generation strategy, promotion, checkpoints, branch/merge |
| 4 | [data-model.md](./data-model.md) | Schemas, state hash, lineage |
| 5 | [telemetry-spec.md](./telemetry-spec.md) | Token/cost instrumentation and derived metrics |
| 6 | [visualization-spec.md](./visualization-spec.md) | gitGraph, live console, data contract |
| 7 | [evaluation-plan.md](./evaluation-plan.md) | Holdout, temporal CV, FDR, ablations, abandon criteria |
| 8 | [risks-and-objections.md](./risks-and-objections.md) | Honest case against the system |
| 9 | [roadmap.md](./roadmap.md) | Phased plan; Phase 1 is falsifiable in weeks |
| 10 | [pitch.md](./pitch.md) | Five-minute project-review case |

## Hard disagreements with the brief (summary)

These are argued in full in the specs and in `risks-and-objections.md`. Short form:

1. **Cartesian product is the wrong generative *primitive*.** Keep combinatorial *spirit*; implement **channel-blocked, graph-guided expansion** with order caps and progressive widening. Full \(2^N\) is a liability, not a feature.
2. **Single-baseline recursive self-improvement will lock early noise.** Prefer a **multi-baseline archive + exploration budget + promotion gates with holdout checks**, not “winner becomes the only truth.”
3. **LLM fusion of public events to observed outcomes will invent causality.** Allow time-aligned *features* and pre-registered tests; ban free-form causal stories as evidence.
4. **Novelty in the loss algorithm is a model-risk liability.** Be novel in *search and orchestration*; be boring in *frequency × severity, dependence, EVT, calibration*.
5. **Existing methods (structured expert elicitation, BN over a curated DAG, standard scenario workshops) remain the gold standard for the thin tail.** Atlas multiplies coverage and audit trail; it does not replace judgment for capital or ICAAP without validation.

## Working assumptions (challenge these)

| ID | Assumption | If wrong, what changes |
|----|------------|------------------------|
| A1 | Primary users are scenario-analysis and research teams, not capital-model production in Phase 1–2 | Pitch, validation depth, algorithm conservatism |
| A2 | The project runs on synthetic and public data only | Evaluation realism and claim boundaries |
| A3 | Grouping uses project-native scoring-cell and synthetic segment codes | Data model fields |
| A4 | Local inference is available for `aggregate` and `local_detail` work | Cost model, enforcement point |
| A5 | Success metric for funding is “cost per validated scenario + calibration evidence,” not number of scenarios generated | Telemetry, pitch, roadmap |

## Open questions (non-blocking; defaults chosen in docs)

See each doc’s “Open questions” section. Defaults are marked **(REC)**. Argue before Phase 2 build freezes them.

## Consistency rules used across this pack

- **Scenario** = compound hypothesis over ≥1 emerging risks (and optional external condition), with auditable score vector.
- **Validated** ≠ true. Validated = passed promotion gates (data support, coherence, novelty, calibration checks).
- **Local-detail boundary** never sends event rows or planted prose to frontier APIs.
- **LLM outputs** are always tagged `judgment` or `narrative`; never `observation`.
- **State hash** is structural (inputs + assumptions + config), not score-valued (scores are derived).
