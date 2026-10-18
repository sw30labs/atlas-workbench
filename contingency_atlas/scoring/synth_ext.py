"""Synthetic external feature series with planted associations.

Aligned to a generated event series (R0-style): one feature is planted to
co-activate with a channel (recoverable, should pass FDR + holdout), one
is planted too weak to meet minimum support (demonstrates the support
filter), and two are pure noise (should fail FDR). Used by
``atlas-eval ext-demo`` and tests.
"""

from __future__ import annotations

import random

from .channels import occurrence_series
from .models import IncidentEvent, RiskAtom

DEFINITIONS = {
    "ai-capex-concentration-high": {
        "name": "AI capex concentration elevated (planted strong)",
        "family": "macro",
        "channel": "identity",  # planted association target
        "recall": 0.95,
        "background": 0.12,
    },
    "regional-outage-cluster": {
        "name": "Regional outage cluster (planted weak)",
        "family": "cyber",
        "channel": "cloud",
        "recall": 0.5,
        "background": 0.02,  # support will fall below s_min
    },
    "rate-hike-quarter": {
        "name": "Rate-hike quarter (noise)",
        "family": "macro",
        "channel": None,
        "recall": 0.0,
        "background": 0.30,
    },
    "sanctions-regime-active": {
        "name": "Sanctions regime active (noise, blocky)",
        "family": "geo",
        "channel": None,
        "recall": 0.0,
        "background": 0.0,  # block-driven, see below
        "blocks": ((10, 4), (26, 5)),
    },
}


def generate_ext_series(
    events: list[IncidentEvent],
    atoms: list[RiskAtom],
    n_periods: int,
    *,
    seed: int = 20260801,
    registered_at: str = "2026-01-01",
) -> dict[str, dict]:
    """Build quarterly binary series for every definition.

    Planted features switch on (with probability ``recall``) in quarters
    where at least one atom of their target channel fired, plus a
    background rate. Noise features are Bernoulli or fixed blocks.
    """
    rng = random.Random(seed)
    series_map = occurrence_series(events, n_periods)
    channel_of = {a.risk_id: a.channels[0] for a in atoms if a.channels}
    channel_quarters: dict[str, set[int]] = {}
    for rid, ch in channel_of.items():
        for t in series_map.get(rid, set()):
            channel_quarters.setdefault(ch, set()).add(t)

    out: dict[str, dict] = {}
    for fid, spec in DEFINITIONS.items():
        values: dict[int, float] = {}
        for t in range(n_periods):
            on = False
            if spec["channel"] is not None:
                fired = t in channel_quarters.get(spec["channel"], set())
                if fired and rng.random() < spec["recall"]:
                    on = True
                elif not fired and rng.random() < spec["background"]:
                    on = True
            elif spec.get("blocks"):
                on = any(start <= t < start + length for start, length in spec["blocks"])
            else:
                on = rng.random() < spec["background"]
            values[t] = 1.0 if on else 0.0
        out[fid] = {
            "name": spec["name"],
            "family": spec["family"],
            "registered_at": registered_at,
            "status": "candidate",
            "series": values,
        }
    return out
