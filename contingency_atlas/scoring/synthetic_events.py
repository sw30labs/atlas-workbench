"""Deterministic fictional events for the synthetic scoring-cell demo.

The generator exists only to exercise scoring, sparse-group suppression, and
egress checks with planted structure.  It is not shaped as an organizational
extract and has no adapter for operator-supplied files.  Every identifier,
segment, amount, and optional narrative is fictional.
"""

from __future__ import annotations

import csv
import random
from dataclasses import dataclass
from pathlib import Path

SCORING_CELL_CODES = (
    "DEMO-CELL-IDENTITY",
    "DEMO-CELL-EXTERNAL",
    "DEMO-CELL-PAYMENTS",
    "DEMO-CELL-TECHNOLOGY",
)
SEGMENT_CODES = (
    "DEMO-SEGMENT-01",
    "DEMO-SEGMENT-02",
    "DEMO-SEGMENT-03",
    "DEMO-SEGMENT-04",
    "DEMO-SEGMENT-05",
)
CHANNELS = ("identity", "cloud", "payments")

# Atoms in one planted channel share a synthetic scoring-cell/segment group.
# Shock periods therefore aggregate enough events to exercise suppression.
CHANNEL_GROUPS = {
    "identity": ("DEMO-CELL-TECHNOLOGY", "DEMO-SEGMENT-05"),
    "cloud": ("DEMO-CELL-TECHNOLOGY", "DEMO-SEGMENT-04"),
    "payments": ("DEMO-CELL-PAYMENTS", "DEMO-SEGMENT-02"),
}

_FICTIONAL_NAMES = ("J. Doe", "A. Smith", "R. Patel", "M. Chen", "S. Weber")


@dataclass
class SyntheticEventConfig:
    n_atoms: int = 15
    n_quarters: int = 24
    seed: int = 20260801
    start_year: int = 2020
    start_quarter: int = 1
    shock_pi: tuple[float, ...] = (0.18, 0.22, 0.28)
    q_base_range: tuple[float, float] = (0.01, 0.03)
    shock_add_range: tuple[float, float] = (0.50, 0.70)
    sev_mu_range: tuple[float, float] = (10.0, 13.0)
    sev_sigma_range: tuple[float, float] = (0.8, 1.4)
    narrative_fraction: float = 0.3
    invalid_rows: int = 3


def _quarter_label(year: int, quarter: int, offset: int) -> str:
    q0 = (year * 4 + (quarter - 1)) + offset
    result_year, result_quarter = divmod(q0, 4)
    month = result_quarter * 3 + 2
    return f"{result_year}-{month:02d}-15"


def generate_synthetic_events(
    config: SyntheticEventConfig | None = None,
) -> tuple[list[dict], dict]:
    """Return fictional event rows and their planted truth manifest."""
    cfg = config or SyntheticEventConfig()
    if cfg.n_atoms < len(CHANNELS) or cfg.n_atoms % len(CHANNELS):
        raise ValueError("n_atoms must be a positive multiple of the channel count")
    rng = random.Random(cfg.seed)
    atoms_per_channel = cfg.n_atoms // len(CHANNELS)

    atoms: list[dict] = []
    for index in range(cfg.n_atoms):
        channel = CHANNELS[index // atoms_per_channel % len(CHANNELS)]
        scoring_cell_code, segment_code = CHANNEL_GROUPS[channel]
        atoms.append(
            {
                "risk_id": f"demo-risk-{channel}-{index % atoms_per_channel + 1:02d}",
                "channel": channel,
                "scoring_cell_code": scoring_cell_code,
                "segment_code": segment_code,
                "q_base": rng.uniform(*cfg.q_base_range),
                "shock_add": rng.uniform(*cfg.shock_add_range),
                "sev_mu": rng.uniform(*cfg.sev_mu_range),
                "sev_sigma": rng.uniform(*cfg.sev_sigma_range),
            }
        )

    shock_rates = [cfg.shock_pi[index % len(cfg.shock_pi)] for index in range(len(CHANNELS))]
    records: list[dict] = []
    sequence = 0
    for period in range(cfg.n_quarters):
        shocked = [rng.random() < shock_rates[index] for index in range(len(CHANNELS))]
        for atom in atoms:
            channel_index = CHANNELS.index(atom["channel"])
            probability = atom["q_base"]
            count = 0
            if shocked[channel_index]:
                probability = min(probability + atom["shock_add"], 0.9)
                if rng.random() < probability:
                    count = 1 + int(rng.expovariate(1 / 0.8))
            elif rng.random() < probability:
                count = 1
            for _ in range(min(count, 3)):
                sequence += 1
                record = {
                    "event_id": f"DEMO-EVENT-{sequence:06d}",
                    "scoring_cell_code": atom["scoring_cell_code"],
                    "segment_code": atom["segment_code"],
                    "date": _quarter_label(cfg.start_year, cfg.start_quarter, period),
                    "impact_amount": round(
                        rng.lognormvariate(atom["sev_mu"], atom["sev_sigma"]), 2
                    ),
                    "risk_id": atom["risk_id"],
                }
                if rng.random() < cfg.narrative_fraction:
                    record["narrative"] = (
                        f"Operator {rng.choice(_FICTIONAL_NAMES)} "
                        f"(contact {rng.choice(_FICTIONAL_NAMES).lower().replace(' ', '').replace('.', '')}"
                        "@example.invalid) filed a fictional demo note; "
                        "free text exists only to test stripping."
                    )
                records.append(record)

    invalid = (
        {
            "event_id": "DEMO-EVENT-INVALID-1",
            "segment_code": "DEMO-SEGMENT-01",
            "date": "2024-02-15",
            "impact_amount": 100.0,
        },
        {
            "event_id": "DEMO-EVENT-INVALID-2",
            "scoring_cell_code": "DEMO-CELL-IDENTITY",
            "segment_code": "DEMO-SEGMENT-01",
            "date": "not-a-date",
            "impact_amount": 100.0,
        },
        {
            "event_id": "DEMO-EVENT-INVALID-3",
            "scoring_cell_code": "DEMO-CELL-IDENTITY",
            "segment_code": "DEMO-SEGMENT-01",
            "date": "2024-02-15",
            "impact_amount": -5.0,
        },
    )
    records.extend(invalid[: cfg.invalid_rows])
    rng.shuffle(records)

    truth = {
        "regime": "synthetic-cell-demo",
        "seed": cfg.seed,
        "n_quarters": cfg.n_quarters,
        "start_year": cfg.start_year,
        "start_quarter": cfg.start_quarter,
        "channels": {
            channel: sorted(
                atom["risk_id"] for atom in atoms if atom["channel"] == channel
            )
            for channel in CHANNELS
        },
        "atoms": atoms,
        "planted_edges": [
            sorted((left["risk_id"], right["risk_id"]))
            for index, left in enumerate(atoms)
            for right in atoms[index + 1 :]
            if left["channel"] == right["channel"]
        ],
    }
    return records, truth


def write_synthetic_events_csv(records: list[dict], path: str | Path) -> Path:
    """Write generated rows for inspection; this is not a file-ingest API."""
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "event_id",
        "scoring_cell_code",
        "segment_code",
        "date",
        "impact_amount",
        "risk_id",
        "narrative",
    ]
    with output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)
    return output
