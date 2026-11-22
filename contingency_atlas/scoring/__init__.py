"""Deterministic scenario-scoring core (redesign Phase 1).

This package implements the falsifiable core of the Contingency Atlas
redesign (``docs/research/redesignv1/``): hybrid frequency x severity scoring of
compound risk scenarios, channel-graph candidate generation, and the
evaluation harness that decides whether the approach lives or dies.

Design rules (algorithm-spec.md):
  * The scorer is deterministic code, never an LLM.
  * LLM/expert input, when present, is tagged ``judgment`` and kept out of
    the observation path.
  * Every score persists its full component decomposition for audit.

No third-party dependencies beyond the standard library: Monte Carlo and
posterior draws use ``random`` with explicit seeds for reproducibility.
"""

ALGORITHM_VERSION = "0.3.0"
"""Semver of the scoring algorithm. Breaking change = score components change
meaning (see docs/research/redesignv1/algorithm-spec.md section 14).

0.2.0 -> 0.3.0: unknown severity remains null; monetary research fits require
measured training losses for each constituent. Realization identities pin the
full training observations and scoring configuration, including severity.

0.1.0 -> 0.2.0 (hybrid-scoring program): interval machinery is now two-level
posterior draws, Fréchet-clipped per draw, with median + p_hat>p_hi flag;
realization identity gained data_hash + judgment_version; horizon hashed in
quarters; loss reporting is regime-aware. The point estimator (dependence
primary) is unchanged; the A1 mixture ships as challenger only."""
