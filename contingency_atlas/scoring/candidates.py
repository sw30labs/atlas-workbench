"""Pair candidate generation and Task B baselines (evaluation-plan.md 3.2).

Baselines:

* B0 random — seeded uniform scores; chance-level reference.
* B1 independence — product of marginal occurrence probabilities.
* B2 shared-channel indicator — 1 if the pair shares a channel tag
  (tiny deterministic tie-break so ranking is total).
* B5 composite — graded channel weight (number of shared channels) x
  co-occurrence count N_S, independence tie-break (A6): the strongest
  baseline that uses no shrinkage machinery. Like B2 it reads channel
  tags, so on R0 it is partially oracle (planted truth) — flagged in
  the eval report.
* B3 batch det scorer — ``DetScorer.p_hat`` over the full pair sweep
  (the "Path A" of loop-design.md section 10; the multiverse loop B4 is
  Phase 2 and deliberately absent here).
"""

from __future__ import annotations

import itertools
import random

from .models import ChannelGraph, RiskAtom
from .scorer import DetScorer

BASELINES = (
    "B0_random",
    "B1_independence",
    "B2_shared_channel",
    "B5_composite",
    "B3_det_batch",
)

# Baselines that read channel tags — ground truth on R0, keyword rules in
# production. Reported as `oracle_baselines` in the eval report (A6/E1).
ORACLE_BASELINES = ("B2_shared_channel", "B5_composite")


def all_pairs(atoms: list[RiskAtom]) -> list[tuple[str, str]]:
    """Full order-2 sweep — the weekly batch safety net (Path A)."""
    ids = sorted(a.risk_id for a in atoms)
    return list(itertools.combinations(ids, 2))


def channel_blocked_pairs(
    graph: ChannelGraph, *, reasons: tuple[str, ...] = ("shared_channel", "empirical")
) -> list[tuple[str, str]]:
    """CBGE production path: only pairs connected in the channel graph."""
    return graph.edge_pairs(reasons=reasons)


def baseline_scores(
    name: str,
    pairs: list[tuple[str, str]],
    scorer: DetScorer,
    atoms: list[RiskAtom],
    *,
    seed: int = 0,
) -> dict[tuple[str, str], float]:
    """Score every pair under one baseline. Returns pair -> score."""
    if name == "B0_random":
        rng = random.Random(f"b0:{seed}")
        return {p: rng.random() for p in pairs}
    if name == "B1_independence":
        return {p: scorer.independence_joint_annual(*p) for p in pairs}
    if name == "B2_shared_channel":
        channel_sets = {a.risk_id: set(a.channels) for a in atoms}
        out = {}
        for a, b in pairs:
            shared = bool(channel_sets.get(a, set()) & channel_sets.get(b, set()))
            # tie-break by independence product so the ranking is total
            out[(a, b)] = (1.0 if shared else 0.0) + 1e-6 * scorer.independence_joint_annual(a, b)
        return out
    if name == "B5_composite":
        # graded channel weight x co-occurrence count, no shrinkage
        # machinery (A6). Same oracle caveat as B2 on R0 (planted tags).
        channel_sets = {a.risk_id: set(a.channels) for a in atoms}
        out = {}
        for a, b in pairs:
            n_shared = len(channel_sets.get(a, set()) & channel_sets.get(b, set()))
            n_s = scorer.joint_support([a, b])
            out[(a, b)] = n_shared * n_s + 1e-6 * scorer.independence_joint_annual(a, b)
        return out
    if name == "B3_det_batch":
        return {p: scorer.score_pair(*p).p_hat for p in pairs}
    raise ValueError(f"unknown baseline: {name}")
