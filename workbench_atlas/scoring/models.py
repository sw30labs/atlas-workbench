"""Core data structures for the scoring core (redesign data-model.md).

Implements the Phase 1 subset of the redesign data model:
RiskAtom, ChannelGraph, IncidentEvent/IncidentFeature, ScoreRealization,
plus canonical-JSON hashing and the scenario structural state hash.

Hashing rules (data-model.md sections 4-5):
  * canonical JSON: sorted keys at every level, no insignificant whitespace,
    UTF-8.
  * scores, timestamps, narrative prose and token counts are NEVER part of
    structural hashes.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from typing import Any

from . import ALGORITHM_VERSION


CANONICAL_SCORING_PLANES = frozenset(
    {"public", "aggregate", "local_detail"}
)


def validate_scoring_plane(plane: object) -> str:
    """Return a canonical scoring plane or reject an unknown/legacy label."""
    if not isinstance(plane, str) or plane not in CANONICAL_SCORING_PLANES:
        raise ValueError(
            "plane must be one of "
            f"{tuple(sorted(CANONICAL_SCORING_PLANES))}, got {plane!r}"
        )
    return plane

# ---------------------------------------------------------------------------
# Canonical JSON + hashing
# ---------------------------------------------------------------------------


def canonical_json(obj: Any) -> str:
    """Canonical JSON per data-model.md section 5: sorted keys, compact."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def hash_object(obj: Any) -> str:
    return sha256_hex(canonical_json(obj))


# ---------------------------------------------------------------------------
# RiskAtom (data-model.md section 2.1)
# ---------------------------------------------------------------------------


@dataclass
class RiskAtom:
    risk_id: str
    title: str
    description: str = ""
    scoring_cell_code: str = ""
    atlas_category: str = ""
    channels: list[str] = field(default_factory=list)
    external_drivers: list[str] = field(default_factory=list)
    signal_maturity: str = ""
    momentum: str = ""
    trend: str = ""
    evidence_strength: str = ""
    status: str = "monitoring"
    review_status: str = ""
    plane: str = "public"
    first_seen: str = ""
    last_reviewed: str = ""
    source_type: str = ""
    provenance: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.plane = validate_scoring_plane(self.plane)

    def to_dict(self) -> dict[str, Any]:
        return {
            "risk_id": self.risk_id,
            "title": self.title,
            "description": self.description,
            "scoring_cell_code": self.scoring_cell_code,
            "atlas_category": self.atlas_category,
            "channels": sorted(self.channels),
            "external_drivers": list(self.external_drivers),
            "signal_maturity": self.signal_maturity,
            "momentum": self.momentum,
            "trend": self.trend,
            "evidence_strength": self.evidence_strength,
            "status": self.status,
            "review_status": self.review_status,
            "plane": self.plane,
            "first_seen": self.first_seen,
            "last_reviewed": self.last_reviewed,
            "source_type": self.source_type,
            "provenance": self.provenance,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RiskAtom":
        return cls(
            risk_id=data["risk_id"],
            title=data.get("title", data["risk_id"]),
            description=data.get("description", ""),
            scoring_cell_code=data.get("scoring_cell_code", ""),
            atlas_category=data.get("atlas_category", ""),
            channels=list(data.get("channels", [])),
            external_drivers=list(data.get("external_drivers", [])),
            signal_maturity=data.get("signal_maturity", ""),
            momentum=data.get("momentum", ""),
            trend=data.get("trend", ""),
            evidence_strength=data.get("evidence_strength", ""),
            status=data.get("status", "monitoring"),
            review_status=data.get("review_status", ""),
            plane=data.get("plane", "public"),
            first_seen=data.get("first_seen", ""),
            last_reviewed=data.get("last_reviewed", ""),
            source_type=data.get("source_type", ""),
            provenance=dict(data.get("provenance", {})),
        )


# ---------------------------------------------------------------------------
# ChannelGraph (data-model.md section 2.2)
# ---------------------------------------------------------------------------

EDGE_REASONS = ("shared_channel", "expert", "empirical", "wildcard")


@dataclass(frozen=True)
class GraphEdge:
    src: str
    dst: str
    reason: str  # one of EDGE_REASONS
    weight: float = 1.0
    source: str = ""

    def key(self) -> tuple[str, str, str]:
        a, b = sorted((self.src, self.dst))
        return (a, b, self.reason)

    def to_dict(self) -> dict[str, Any]:
        a, b = sorted((self.src, self.dst))
        return {
            "src": a,
            "dst": b,
            "reason": self.reason,
            "weight": self.weight,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GraphEdge":
        return cls(
            src=data["src"],
            dst=data["dst"],
            reason=data["reason"],
            weight=float(data.get("weight", 1.0)),
            source=data.get("source", ""),
        )


@dataclass
class ChannelGraph:
    graph_id: str
    version: int
    nodes: list[str]
    edges: list[GraphEdge]
    created_at: str = ""

    def compute_hash(self) -> str:
        return hash_object(
            {
                "nodes": sorted(self.nodes),
                "edges": sorted(
                    (e.to_dict() for e in self.edges),
                    key=lambda d: (d["src"], d["dst"], d["reason"]),
                ),
            }
        )

    @property
    def graph_hash(self) -> str:
        return self.compute_hash()

    def adjacency(self) -> dict[str, set[str]]:
        adj: dict[str, set[str]] = {n: set() for n in self.nodes}
        for e in self.edges:
            adj.setdefault(e.src, set()).add(e.dst)
            adj.setdefault(e.dst, set()).add(e.src)
        return adj

    def are_connected(self, a: str, b: str) -> bool:
        return b in self.adjacency().get(a, set())

    def edge_pairs(self, reasons: tuple[str, ...] | None = None) -> list[tuple[str, str]]:
        """Unique unordered pairs connected by an edge (optionally filtered)."""
        pairs: set[tuple[str, str]] = set()
        for e in self.edges:
            if reasons is not None and e.reason not in reasons:
                continue
            pairs.add(tuple(sorted((e.src, e.dst))))
        return sorted(pairs)

    def to_dict(self) -> dict[str, Any]:
        return {
            "graph_id": self.graph_id,
            "version": self.version,
            "nodes": sorted(self.nodes),
            "edges": sorted(
                (e.to_dict() for e in self.edges),
                key=lambda d: (d["src"], d["dst"], d["reason"]),
            ),
            "graph_hash": self.compute_hash(),
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ChannelGraph":
        return cls(
            graph_id=data["graph_id"],
            version=int(data.get("version", 1)),
            nodes=sorted(data["nodes"]),
            edges=[GraphEdge.from_dict(e) for e in data.get("edges", [])],
            created_at=data.get("created_at", ""),
        )


# ---------------------------------------------------------------------------
# Incident events (raw, local-only) and features (aggregates)
# ---------------------------------------------------------------------------


@dataclass
class IncidentEvent:
    """One loss event. Raw store is local-only (data-model.md section 2.3)."""

    event_id: str
    risk_id: str
    period: int  # quarter index
    loss_amount: float | None

    def __post_init__(self) -> None:
        if self.loss_amount is not None:
            self.loss_amount = float(self.loss_amount)
            if not math.isfinite(self.loss_amount) or self.loss_amount < 0:
                raise ValueError("loss_amount must be a finite non-negative amount or None")

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "risk_id": self.risk_id,
            "period": self.period,
            "loss_amount": self.loss_amount,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "IncidentEvent":
        return cls(
            event_id=data["event_id"],
            risk_id=data["risk_id"],
            period=int(data["period"]),
            loss_amount=data.get("loss_amount"),
        )


@dataclass
class IncidentFeature:
    """Synthetic scoring aggregate per (cell, period). Never holds narrative."""

    cell_id: str
    scoring_cell_code: str
    segment_code: str
    period: int
    count: int
    severity_quantiles: dict[str, float] | None
    n_severity: int
    plane: str = "aggregate"
    source_system: str = "synthetic"

    def __post_init__(self) -> None:
        self.plane = validate_scoring_plane(self.plane)

    def to_dict(self) -> dict[str, Any]:
        return {
            "cell_id": self.cell_id,
            "scoring_cell_code": self.scoring_cell_code,
            "segment_code": self.segment_code,
            "period": self.period,
            "count": self.count,
            "severity_quantiles": self.severity_quantiles,
            "n_severity": self.n_severity,
            "plane": self.plane,
            "source_system": self.source_system,
        }


# ---------------------------------------------------------------------------
# ScoreRealization (data-model.md section 2.6 + algorithm-spec.md section 9)
# ---------------------------------------------------------------------------

UNCERTAINTY_CLASSES = ("statistical", "mixed", "judgment_dominant")


def pair_id_for(risk_ids: list[str]) -> str:
    """Stable compound identity: hash of the sorted risk_id set, nothing else.

    Unlike ``scn-{state_hash[:12]}`` this survives graph-version churn, slug
    renames of *other* atoms, horizon changes and estimator swaps — the
    continuity key cross-version reporting, analogue dossiers and the
    judgment ledger ride on (HYBRID_SCORING_PROPOSAL.md A7/D12). k-agnostic.
    """
    return "pair-" + sha256_hex(canonical_json(sorted(risk_ids)))[:12]


def scenario_state_hash(
    risk_ids: list[str],
    horizon: str,
    compound_def: str = "all_must_fire",
    k: int | None = None,
    transmission_id: str = "",
    scope: dict[str, Any] | None = None,
    assumptions: dict[str, Any] | None = None,
    graph_hash: str = "",
    feature_registry_hash: str = "",
    algorithm_version: str = ALGORITHM_VERSION,
) -> str:
    """Structural state hash (data-model.md section 4.1).

    Scores, seeds, timestamps and prose are deliberately excluded: two runs
    of the same state with different MC seeds are the same state, different
    realizations.
    """
    scope = scope or {}
    assumptions = assumptions or {}
    return hash_object(
        {
            "risk_ids": sorted(risk_ids),
            "horizon": horizon,
            "compound_def": compound_def,
            "k": k,
            "transmission_id": transmission_id,
            "scope": scope,
            "assumption_hash": hash_object(assumptions),
            "graph_hash": graph_hash,
            "feature_registry_hash": feature_registry_hash,
            "algorithm_major": algorithm_version.split(".")[0] + ".x",
        }
    )


@dataclass
class ScoreRealization:
    realization_id: str
    scenario_id: str
    state_hash: str
    algorithm_version: str
    model_config_hash: str
    rng_seed: str
    p_hat: float
    p_interval: tuple[float, float]
    loss_quantiles: dict[str, float | None]
    loss_mean: float | None
    components: dict[str, Any]
    uncertainty_class: str
    disagreement: float = 0.0
    novelty: float = 1.0
    evidence_support: float = 0.0
    coherence: float | None = None
    u_score: float | None = None
    created_at: str = ""
    # Dual-realization identity keys (A7): ``data_hash`` pins the fitted
    # signal series + train bounds; ``judgment_version`` distinguishes the
    # deterministic realization ("det") from any future hybrid realization
    # of the same state, so the two can coexist without colliding.
    data_hash: str = ""
    judgment_version: str = "det"

    def to_dict(self) -> dict[str, Any]:
        return {
            "realization_id": self.realization_id,
            "scenario_id": self.scenario_id,
            "state_hash": self.state_hash,
            "algorithm_version": self.algorithm_version,
            "model_config_hash": self.model_config_hash,
            "rng_seed": self.rng_seed,
            "p_hat": self.p_hat,
            "p_interval": list(self.p_interval),
            "loss_quantiles": self.loss_quantiles,
            "loss_mean": self.loss_mean,
            "components": self.components,
            "uncertainty_class": self.uncertainty_class,
            "disagreement": self.disagreement,
            "novelty": self.novelty,
            "evidence_support": self.evidence_support,
            "coherence": self.coherence,
            "U_score": self.u_score,
            "created_at": self.created_at,
            "data_hash": self.data_hash,
            "judgment_version": self.judgment_version,
        }
