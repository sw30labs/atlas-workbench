"""ChannelGraph construction (redesign loop-design.md section 1.2).

Edges have three Phase 1 sources:

* ``shared_channel`` — risk pair shares at least one channel tag (from the
  risk inventory; the CBGE blocking structure).
* ``empirical`` — risk pair co-occurs in the training loss data above a
  threshold (used for Task C structure recovery on R0, where the planted
  channels are hidden from the graph builder).
* ``expert`` / ``wildcard`` — reserved for Phase 2.

The graph hash covers nodes + edges only (data-model.md section 2.2).
"""

from __future__ import annotations

from collections import defaultdict

from .models import ChannelGraph, GraphEdge, IncidentEvent, RiskAtom


def occurrence_series(
    events: list[IncidentEvent], n_periods: int
) -> dict[str, set[int]]:
    """risk_id -> set of periods with at least one event."""
    series: dict[str, set[int]] = defaultdict(set)
    for e in events:
        if 0 <= e.period < n_periods:
            series[e.risk_id].add(e.period)
    return series


def cooccurrence_counts(
    series: dict[str, set[int]], risk_ids: list[str] | None = None
) -> dict[tuple[str, str], int]:
    """Pair -> number of shared periods (both atoms fired in same period)."""
    ids = risk_ids if risk_ids is not None else sorted(series)
    counts: dict[tuple[str, str], int] = defaultdict(int)
    for i, a in enumerate(ids):
        pa = series.get(a, set())
        if not pa:
            continue
        for b in ids[i + 1 :]:
            shared = len(pa & series.get(b, set()))
            if shared:
                counts[(a, b)] = shared
    return counts


def build_channel_graph(
    atoms: list[RiskAtom],
    *,
    events: list[IncidentEvent] | None = None,
    n_periods: int = 0,
    empirical_tau: int = 2,
    include_shared_channel: bool = True,
    include_empirical: bool = False,
    graph_id: str = "g_ch",
    version: int = 1,
) -> ChannelGraph:
    """Build the channel graph from inventory tags and/or empirical data.

    ``empirical_tau``: a pair earns an ``empirical`` edge when it co-occurs
    in strictly more than ``empirical_tau`` periods... no: in at least
    ``empirical_tau`` periods (loop-design: "co-occurrence > tau" with the
    default tau=2 meaning >= 2 shared quarters; documented here to keep the
    threshold unambiguous).
    """
    nodes = sorted(a.risk_id for a in atoms)
    edges: list[GraphEdge] = []

    if include_shared_channel:
        by_channel: dict[str, list[str]] = defaultdict(list)
        for a in atoms:
            for ch in a.channels:
                by_channel[ch].append(a.risk_id)
        pair_channels: dict[tuple[str, str], list[str]] = defaultdict(list)
        for ch, members in by_channel.items():
            members = sorted(members)
            for i, x in enumerate(members):
                for y in members[i + 1 :]:
                    pair_channels[(x, y)].append(ch)
        for (x, y), chs in sorted(pair_channels.items()):
            edges.append(
                GraphEdge(
                    src=x,
                    dst=y,
                    reason="shared_channel",
                    weight=float(len(chs)),
                    source=",".join(sorted(chs)),
                )
            )

    if include_empirical and events is not None and n_periods > 0:
        series = occurrence_series(events, n_periods)
        counts = cooccurrence_counts(series, nodes)
        for (x, y), n in sorted(counts.items()):
            if n < empirical_tau:
                continue
            # A pair already linked by shared_channel earns a second edge
            # with reason=empirical; both are visible in the graph.
            edges.append(
                GraphEdge(
                    src=x,
                    dst=y,
                    reason="empirical",
                    weight=float(n),
                    source=f"cooccur>={empirical_tau}",
                )
            )

    return ChannelGraph(graph_id=graph_id, version=version, nodes=nodes, edges=edges)


def recover_empirical_edges(
    events: list[IncidentEvent],
    risk_ids: list[str],
    n_periods: int,
    *,
    top_n: int,
) -> list[tuple[str, str]]:
    """Rank pairs by co-occurrence lift and return the top_n edges.

    Lift = N_ij * T / (N_i * N_j): observed co-occurrence over the
    independence expectation, so pairs that merely share busy atoms do not
    outrank genuinely coupled pairs. Ties broken by count, then pair id.

    Used by Task C (evaluation-plan.md section 3.3): the planted graph is
    hidden; recovery must reach precision >= 0.7 at top_n = #planted edges.
    """
    series = occurrence_series(events, n_periods)
    counts = cooccurrence_counts(series, risk_ids)

    def _lift(pair: tuple[str, str], n_ij: int) -> float:
        n_i = len(series.get(pair[0], set()))
        n_j = len(series.get(pair[1], set()))
        if n_ij == 0 or n_i == 0 or n_j == 0:
            return 0.0
        return n_ij * n_periods / (n_i * n_j)

    ranked = sorted(
        counts.items(), key=lambda kv: (-_lift(kv[0], kv[1]), -kv[1], kv[0])
    )
    return [pair for pair, _ in ranked[:top_n]]
