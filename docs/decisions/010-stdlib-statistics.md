# ADR-010: Pure-stdlib statistics; no numpy/scipy

- **Status:** accepted
- **Date:** 2026-08-01

## Context

The scoring engine needs Monte Carlo, posterior draws, Poisson/NB and
lognormal fits, GPD tails, and ranking metrics. The project venv had no
numpy/scipy, and the deployment target is a small homelab.

## Decision

All statistics are implemented in pure Python stdlib: `random` with
explicit per-state seeds (MC, `betavariate`, `lognormvariate`), direct
summation Poisson/NB pmf/cdf/quantiles, method-of-moments GPD,
Mann-Whitney AUROC and average precision, Fisher exact via `math.comb`,
Benjamini–Hochberg, block bootstrap, sign test via `math.comb`.

## Consequences

- Zero numeric dependencies; everything auditable line by line.
- Performance is a non-issue at current scale: 105 pairs × 20k MC draws
  × 4 quarters completes in ~1 s; 2k-pair sweeps remain interactive.
- Reproducibility is literal: same seed, same platform, same numbers.

## Alternatives considered

- **numpy/scipy** — rejected as an added dependency for workloads that
  are seconds-scale; revisit if future workloads bring orders-of-magnitude more
  pairs.
- **PyMC / probabilistic programming** — explicitly deferred by the
  redesign ("Phase 1 simplicity").
