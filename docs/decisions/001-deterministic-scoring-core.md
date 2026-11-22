# ADR-001: Deterministic scoring core; no LLM in the score path

- **Status:** accepted
- **Date:** 2026-08-01

## Context

The system needs defensible numbers for compound scenarios (probability
of co-materialization, loss quantiles). The legacy pipeline produced
qualitative urgency via LLM. Any number that influences risk decisions
will be treated as a model by validators — they will ask "reproduce this
score."

## Decision

The scoring core is deterministic code only: Poisson/lognormal marginals
with empirical-Bayes shrinkage, shrunk-empirical pair dependence, Monte
Carlo propagation, GPD tail overlay. **No LLM produces or adjusts any
frequency, severity, dependence, or tail figure.** LLM use is confined
to clearly marked judgment/prose roles (coherence critique, document
drafting, extraction), which never feed the score vector.

## Consequences

- Every score is reproducible from `(state, algorithm_version, seed)`
  plus train data — the audit blob carries all components.
- Novelty lives in search/evaluation orchestration, not in the loss
  algorithm ("boring where numbers must survive validation").
- The evaluation harness can falsify the method: baselines B0–B3 and
  ablation A1 exist precisely because the score path is code.
- LLM elicitation is a future option only as a tagged `judgment` channel
  (unused in production today).

## Alternatives considered

- **LLM point estimates of probability/severity** — fast, unvalidatable;
  rejected (the redesign's own rule: "LLM scoring of loss: forbidden as
  primary").
- **LLM proposes modifiers, humans accept** — kept as a possible Phase 4
  extension; requires the policy-accept gate first.
