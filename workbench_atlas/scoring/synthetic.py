"""R0 synthetic loss generator with planted causal channels.

Regime R0 (evaluation-plan.md section 1): a known generative process with
planted channels and noise, used to unit-test that the scorer recovers
structure. Nothing here claims to represent a real institution.

Generative story (common-factor model, algorithm-spec.md section 4.3):

* Each channel ``c`` has an i.i.d. quarterly shock ``Z[c,t] ~ Bern(pi_c)``.
* Atom ``i`` in channel ``ch(i)`` fires in quarter ``t`` with probability
  ``q_base_i`` normally, and ``min(q_base_i * mult_i + shock_add_i, 0.9)``
  when its channel is shocked — a shock quarter lifts the whole channel
  coherently, which is what "shared channel" means. Atoms in different
  channels co-fire only by chance.
* Each firing produces one loss event with severity ~ Lognormal(mu_i, si_i).

The planted channel membership is the ground truth for Task C; the loss
events are the only thing the scorer is allowed to see.
"""

from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass, field
from pathlib import Path

from .models import IncidentEvent, IncidentFeature, RiskAtom

ATOM_NAMES = (
    "identity-plane outage synchronization",
    "privileged-session broker fatigue",
    "certificate rotation cascade",
    "directory replication conflict",
    "access-review backlog accumulation",
    "cloud region failover congestion",
    "object-store consistency drift",
    "managed-kubernetes control-plane saturation",
    "egress-cost driven architecture lock-in",
    "shared DNS resolver dependency",
    "payment-gateway timeout amplification",
    "liquidity-queue reconciliation backlog",
    "sanctions-screening rule churn",
    "card-authorization stand-in drift",
    "settlement cut-off compression",
)

CHANNEL_NAMES = ("identity", "cloud", "payments")


@dataclass
class R0Config:
    n_atoms: int = 15
    n_channels: int = 3
    n_quarters: int = 40
    seed: int = 20260801
    # channel shock probabilities (cycled if fewer than n_channels)
    shock_pi: tuple[float, ...] = (0.15, 0.20, 0.25)
    # atom base quarterly occurrence probability range (idiosyncratic noise;
    # kept very low so cross-channel chance co-occurrence stays rare)
    q_base_range: tuple[float, float] = (0.004, 0.012)
    # shock response: q_shock = q_base * mult + shock_add (coherent channel
    # lift), capped at 0.9
    shock_mult_range: tuple[float, float] = (2.0, 4.0)
    shock_add_range: tuple[float, float] = (0.55, 0.75)
    # lognormal severity parameter ranges (log USD)
    sev_mu_range: tuple[float, float] = (10.5, 13.5)
    sev_sigma_range: tuple[float, float] = (0.8, 1.4)
    # R0' (count regime): "poisson_counts" makes a firing quarter produce
    # max(1, Poisson(count_lambda_i)) events instead of exactly 1, giving
    # per-cell count variation for Task A calibration and richer severity
    # samples. Occurrence series (and therefore Task B/C) are unchanged.
    count_model: str = "bernoulli"  # | "poisson_counts"
    count_lambda_range: tuple[float, float] = (0.8, 2.5)


def _poisson_draw(lam: float, rng: random.Random) -> int:
    """Knuth sampler (fine for small lambda)."""
    threshold = math.exp(-lam)
    k, p = 0, 1.0
    while p > threshold:
        k += 1
        p *= rng.random()
    return k - 1


@dataclass
class R0Dataset:
    config: R0Config
    atoms: list[RiskAtom]
    events: list[IncidentEvent]
    truth: dict = field(default_factory=dict)

    def write(self, out_dir: str | Path) -> dict[str, Path]:
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        risks_path = out / "r0_risks.json"
        events_path = out / "r0_events.jsonl"
        truth_path = out / "r0_truth.json"
        risks_path.write_text(json.dumps([a.to_dict() for a in self.atoms], indent=2))
        events_path.write_text(
            "".join(json.dumps(e.to_dict()) + "\n" for e in self.events)
        )
        truth_path.write_text(json.dumps(self.truth, indent=2, sort_keys=True))
        return {"risks": risks_path, "events": events_path, "truth": truth_path}


def generate_r0(config: R0Config | None = None) -> R0Dataset:
    """Generate the R0 dataset deterministically from ``config.seed``."""
    cfg = config or R0Config()
    if cfg.n_atoms > len(ATOM_NAMES):
        raise ValueError(f"R0 ships {len(ATOM_NAMES)} atom names, requested {cfg.n_atoms}")
    rng = random.Random(cfg.seed)

    atoms_per_channel = cfg.n_atoms // cfg.n_channels
    memberships: dict[str, str] = {}
    atoms: list[RiskAtom] = []
    for idx in range(cfg.n_atoms):
        channel = CHANNEL_NAMES[idx // atoms_per_channel % cfg.n_channels]
        risk_id = f"r0-{channel}-{idx % atoms_per_channel + 1:02d}"
        memberships[risk_id] = channel
        atoms.append(
            RiskAtom(
                risk_id=risk_id,
                title=ATOM_NAMES[idx],
                description=(
                    "Synthetic R0 atom (fictional). Planted channel membership "
                    "is ground truth for structure recovery tests."
                ),
                channels=[channel],
                signal_maturity="established",
                momentum="stable",
                trend="stable",
                status="monitoring",
                review_status="approved_synthetic",
                plane="aggregate",
                source_type="synthetic",
                provenance={"regime": "R0", "note": "synthetic generator"},
            )
        )

    pi = [cfg.shock_pi[c % len(cfg.shock_pi)] for c in range(cfg.n_channels)]
    channel_index = {ch: i for i, ch in enumerate(CHANNEL_NAMES[: cfg.n_channels])}

    q_base: dict[str, float] = {}
    mult: dict[str, float] = {}
    shock_add: dict[str, float] = {}
    mu: dict[str, float] = {}
    sigma: dict[str, float] = {}
    count_lambda: dict[str, float] = {}
    # count_lambda draws come from a SEPARATE stream: adding them to the
    # shared stream would shift every downstream draw and silently change
    # the pre-registered R0 dataset for the same seed
    rng_counts = random.Random(f"{cfg.seed}:count_lambda")
    for a in atoms:
        q_base[a.risk_id] = rng.uniform(*cfg.q_base_range)
        mult[a.risk_id] = rng.uniform(*cfg.shock_mult_range)
        shock_add[a.risk_id] = rng.uniform(*cfg.shock_add_range)
        mu[a.risk_id] = rng.uniform(*cfg.sev_mu_range)
        sigma[a.risk_id] = rng.uniform(*cfg.sev_sigma_range)
        count_lambda[a.risk_id] = rng_counts.uniform(*cfg.count_lambda_range)

    events: list[IncidentEvent] = []
    seq = 0
    for t in range(cfg.n_quarters):
        shocked = [rng.random() < pi[c] for c in range(cfg.n_channels)]
        for a in atoms:
            ch = memberships[a.risk_id]
            q = q_base[a.risk_id]
            if shocked[channel_index[ch]]:
                q = min(q * mult[a.risk_id] + shock_add[a.risk_id], 0.9)
            if rng.random() < q:
                n_events = 1
                if cfg.count_model == "poisson_counts":
                    n_events = max(1, _poisson_draw(count_lambda[a.risk_id], rng))
                for _ in range(n_events):
                    seq += 1
                    events.append(
                        IncidentEvent(
                            event_id=f"R0-EVT-{seq:05d}",
                            risk_id=a.risk_id,
                            period=t,
                            loss_amount=rng.lognormvariate(mu[a.risk_id], sigma[a.risk_id]),
                        )
                    )

    planted_edges = []
    ids = sorted(memberships)
    for i, x in enumerate(ids):
        for y in ids[i + 1 :]:
            if memberships[x] == memberships[y]:
                planted_edges.append([x, y])

    truth = {
        "regime": "R0" if cfg.count_model == "bernoulli" else "R0'",
        "seed": cfg.seed,
        "n_quarters": cfg.n_quarters,
        "count_model": cfg.count_model,
        "channels": {ch: sorted(r for r, c in memberships.items() if c == ch) for ch in CHANNEL_NAMES[: cfg.n_channels]},
        "planted_edges": planted_edges,
        "shock_pi": {ch: pi[channel_index[ch]] for ch in channel_index},
        "q_base": q_base,
        "shock_mult": mult,
        "shock_add": shock_add,
        "sev_mu": mu,
        "sev_sigma": sigma,
        "count_lambda": count_lambda,
    }
    return R0Dataset(config=cfg, atoms=atoms, events=events, truth=truth)


def aggregate_incident_features(events: list[IncidentEvent]) -> list[IncidentFeature]:
    """Aggregate raw events into per-(cell, period) IncidentFeature rows.

    cell_id == risk_id in R0 (each atom is its own event-type cell). Only
    counts and severity quantiles are stored — never narrative.
    """
    by_cell: dict[tuple[str, int], list[float | None]] = {}
    for e in events:
        by_cell.setdefault((e.risk_id, e.period), []).append(e.loss_amount)
    rows: list[IncidentFeature] = []
    for (risk_id, period), observations in sorted(by_cell.items()):
        amounts = sorted(value for value in observations if value is not None)
        n = len(amounts)
        quantiles = {
            "0.5": amounts[n // 2] if n % 2 else (amounts[n // 2 - 1] + amounts[n // 2]) / 2,
            "0.9": amounts[min(n - 1, int(0.9 * (n - 1)))],
        } if n else None
        rows.append(
            IncidentFeature(
                cell_id=risk_id,
                scoring_cell_code=risk_id,
                segment_code="synthetic-default",
                period=period,
                count=len(observations),
                severity_quantiles=quantiles,
                n_severity=n,
                plane="aggregate",
                source_system="synthetic",
            )
        )
    return rows


def load_r0_events(path: str | Path) -> list[IncidentEvent]:
    return [
        IncidentEvent.from_dict(json.loads(line))
        for line in Path(path).read_text().splitlines()
        if line.strip()
    ]


def load_r0_risks(path: str | Path) -> list[RiskAtom]:
    return [RiskAtom.from_dict(d) for d in json.loads(Path(path).read_text())]
