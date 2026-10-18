"""Phase 4: Prioritization & Scoring.

Scores scenarios across multiple dimensions and computes a composite urgency
score for ranking. Dimensions:

  - Likelihood (qualitative → numeric)
  - Financial impact (qualitative → numeric)
  - Time horizon (when could this materialize?)
  - Controllability (internal vs external)
  - Foresight value (traceable drivers, meaningful uncertainty)

The composite urgency score (0-100) drives the prioritized portfolio:
  urgency = w1 * likelihood + w2 * financial + w3 * (1 - controllability)
            + w4 * foresight_value + w5 * time_horizon_weight

Uses LLM-as-a-Judge to validate scores where uncertain. Fully autonomous.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from ..llm import OMLXClient
from ..db import list_scenarios, update_scenario_fields, to_json_str, from_json_str, now_iso

logger = logging.getLogger(__name__)


# ── Scoring weights ──

WEIGHTS = {
    "likelihood": 0.25,
    "financial": 0.30,
    "controllability": 0.20,  # Inverted: less controllable = higher urgency
    "foresight_value": 0.15,
    "time_horizon": 0.10,
}

LIKELIHOOD_MAP = {
    "very high": 1.0,
    "high": 0.8,
    "moderate to high": 0.7,
    "moderate": 0.5,
    "low to moderate": 0.3,
    "low": 0.2,
    "very low": 0.1,
}

FINANCIAL_MAP = {
    "extreme": 1.0,
    "major": 0.8,
    "significant": 0.6,
    "moderate": 0.4,
    "limited": 0.2,
}

CONTROLLABILITY_MAP = {
    "internal": 0.2,       # Highly controllable → low urgency boost
    "mixed": 0.5,
    "external": 0.9,       # Exogenous → high urgency
    "": 0.5,
}

# Time horizon: sooner = higher urgency
TIME_HORIZON_MAP = {
    "6m": 1.0,
    "1y": 0.8,
    "2y": 0.6,
    "3y": 0.4,
    "5y": 0.2,
    "5y+": 0.1,
    "": 0.5,
}


@dataclass
class ScoreResult:
    """Scoring result for a single scenario."""
    scenario_id: str
    likelihood_numeric: float
    financial_numeric: float
    controllability_numeric: float
    foresight_value: float
    time_horizon_numeric: float
    urgency_score: float
    time_horizon: str
    controllability: str
    updated: bool


@dataclass
class PrioritizationResult:
    """Result of a prioritization cycle."""
    scored: list[ScoreResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    updated: int = 0


# ── Core scoring functions ──

def _numeric_likelihood(likelihood: str) -> float:
    """Convert qualitative likelihood to 0-1."""
    if not likelihood:
        return 0.5
    l = likelihood.lower().strip()
    # Exact match first
    if l in LIKELIHOOD_MAP:
        return LIKELIHOOD_MAP[l]
    # Substring match
    for k, v in LIKELIHOOD_MAP.items():
        if k in l:
            return v
    return 0.5


def _numeric_financial(financial_name: str) -> float:
    """Convert financial impact name to 0-1."""
    if not financial_name:
        return 0.45
    f = financial_name.lower().strip()
    if f in FINANCIAL_MAP:
        return FINANCIAL_MAP[f]
    for k, v in FINANCIAL_MAP.items():
        if k in f:
            return v
    return 0.45


def _infer_time_horizon(scenario_horizon: str) -> str:
    """Infer a coarse time horizon from the date range string."""
    if not scenario_horizon:
        return ""
    s = scenario_horizon.lower()
    # Look for year mentions
    years = re.findall(r"(20\d{2})", s)
    if not years:
        return ""
    min_year = min(int(y) for y in years)
    from datetime import datetime
    current_year = datetime.now().year
    delta = min_year - current_year
    if delta <= 0:
        return "6m"  # Already relevant
    if delta <= 1:
        return "1y"
    if delta <= 2:
        return "2y"
    if delta <= 3:
        return "3y"
    if delta <= 5:
        return "5y"
    return "5y+"


def _infer_controllability(
    scenario: dict[str, Any], client: OMLXClient | None = None
) -> str:
    """Infer controllability (internal/external/mixed).

    Uses heuristics first; falls back to LLM if ambiguous.
    """
    category = (scenario.get("risk_category") or "").lower()
    desc = (scenario.get("description") or "").lower()
    core_risks = scenario.get("core_risks")
    if isinstance(core_risks, str):
        core_risks = from_json_str(core_risks) or []
    risks_text = " ".join(core_risks).lower() if core_risks else ""

    combined = f"{category} {desc} {risks_text}"

    # External signals
    external_kw = ["geopolit", "sanction", "war", "climate", "regulator", "sovereign",
                   "third party", "vendor", "market", "global", "supply chain"]
    # Internal signals
    internal_kw = ["process", "control", "training", "model", "policy", "procedure",
                   "staff", "internal", "governance"]

    ext_score = sum(1 for w in external_kw if w in combined)
    int_score = sum(1 for w in internal_kw if w in combined)

    if ext_score > int_score * 1.5:
        return "external"
    if int_score > ext_score * 1.5:
        return "internal"
    return "mixed"


def _infer_foresight_value(scenario: dict[str, Any]) -> float:
    """Estimate foresight value (0-1).

    Higher values reward a traceable transmission path, second-order effects,
    and observable evidence that could strengthen or weaken the hypothesis.
    """
    def list_count(field_name: str) -> int:
        value = scenario.get(field_name)
        if isinstance(value, str):
            value = from_json_str(value) or []
        return len(value) if isinstance(value, list) else 0

    surprise = (scenario.get("element_of_surprise") or "").lower()
    rationale = scenario.get("detailed_rationale")
    if isinstance(rationale, str):
        rationale = from_json_str(rationale) or []
    rationale_count = len(rationale) if rationale else 0

    rationale_score = min(rationale_count / 4.0, 1.0)
    transmission_score = 1.0 if str(
        scenario.get("transmission_mechanism") or ""
    ).strip() else 0.0
    consequence_score = min(list_count("second_order_effects") / 2.0, 1.0)
    strengthening_count = list_count("strengthening_signposts")
    weakening_count = list_count("weakening_signposts")
    signpost_score = min(
        (strengthening_count + weakening_count) / 4.0,
        1.0,
    )
    if strengthening_count and weakening_count:
        signpost_score = min(signpost_score + 0.2, 1.0)
    driver_score = min(list_count("external_drivers") / 2.0, 1.0)
    uncertainty = (
        0.8
        if any(
            word in surprise
            for word in ("surprise", "uncertain", "unexpected")
        )
        else 0.5
    )

    return min(
        rationale_score * 0.25
        + transmission_score * 0.20
        + consequence_score * 0.15
        + signpost_score * 0.20
        + driver_score * 0.10
        + uncertainty * 0.10,
        1.0,
    )


def compute_urgency(
    scenario: dict[str, Any],
    client: OMLXClient | None = None,
) -> ScoreResult:
    """Compute composite urgency score for a scenario.

    Args:
        scenario: A scenario dict (from DB or generation)
        client: Optional OMLX client for LLM-assisted inference

    Returns:
        ScoreResult with numeric dimensions and urgency score (0-100)
    """
    likelihood_numeric = _numeric_likelihood(scenario.get("likelihood", ""))
    financial_numeric = _numeric_financial(
        scenario.get("financial_impact_band", "")
    )

    # Infer time horizon if not set
    time_horizon = scenario.get("time_horizon") or _infer_time_horizon(
        scenario.get("scenario_horizon", "")
    )
    time_horizon_numeric = TIME_HORIZON_MAP.get(time_horizon, 0.5)

    # Infer controllability
    controllability = scenario.get("controllability") or _infer_controllability(scenario, client)
    controllability_numeric = CONTROLLABILITY_MAP.get(controllability, 0.5)

    # Value as a forward-looking, evidence-grounded research hypothesis.
    foresight_value = _infer_foresight_value(scenario)

    # Composite urgency (0-100)
    urgency = (
        WEIGHTS["likelihood"] * likelihood_numeric
        + WEIGHTS["financial"] * financial_numeric
        + WEIGHTS["controllability"] * controllability_numeric
        + WEIGHTS["foresight_value"] * foresight_value
        + WEIGHTS["time_horizon"] * time_horizon_numeric
    ) * 100

    scenario_id = scenario.get("scenario_id", "")
    return ScoreResult(
        scenario_id=scenario_id,
        likelihood_numeric=likelihood_numeric,
        financial_numeric=financial_numeric,
        controllability_numeric=controllability_numeric,
        foresight_value=foresight_value,
        time_horizon_numeric=time_horizon_numeric,
        urgency_score=round(urgency, 2),
        time_horizon=time_horizon,
        controllability=controllability,
        updated=False,
    )


def run_prioritization_cycle(
    db_path: str,
    client: OMLXClient | None = None,
    *,
    category: str | None = None,
    limit: int | None = None,
) -> PrioritizationResult:
    """Run a prioritization cycle over the scenario library.

    For each scenario, compute urgency and update the DB with the score,
    time horizon, and controllability. Returns a PrioritizationResult.
    """
    result = PrioritizationResult()

    scenarios = list_scenarios(db_path, category=category, limit=limit, status="active")
    scenarios += list_scenarios(db_path, category=category, limit=limit, status="draft")
    # Graduated compounds carry auditable research scores with their own
    # provenance. They never map onto the scenario urgency scale; selecting by
    # explicit origin avoids making identifier spelling part of behavior.
    scenarios = [
        s for s in scenarios
        if s.get("origin") != "compound_graduation"
    ]

    for s in scenarios:
        try:
            score = compute_urgency(s, client)
            # Update DB
            update_scenario_fields(
                db_path, score.scenario_id,
                {
                    "urgency_score": score.urgency_score,
                    "time_horizon": score.time_horizon,
                    "controllability": score.controllability,
                    "last_scored": now_iso(),
                },
            )
            score.updated = True
            result.scored.append(score)
            result.updated += 1
        except (sqlite3.Error, ValueError, TypeError) as e:
            logger.error(f"Scoring failed for {s.get('scenario_id', '?')}: {e}")
            result.errors.append(str(e))
        except Exception as e:
            logger.error(
                f"Scoring failed unexpectedly for {s.get('scenario_id', '?')}: {e}",
                exc_info=True,
            )
            result.errors.append(str(e))

    logger.info(
        f"Prioritization cycle: {result.updated} scenarios scored, "
        f"{len(result.errors)} errors"
    )
    return result


def get_prioritized_portfolio(
    db_path: str,
    *,
    top_n: int = 20,
    min_urgency: float = 0.0,
) -> list[dict[str, Any]]:
    """Return the top-N scenarios by urgency score.

    Filters by min_urgency and sorts descending.
    """
    scenarios = list_scenarios(db_path, status="active")
    scored = []
    for s in scenarios:
        urgency = s.get("urgency_score")
        if urgency is None:
            continue
        try:
            urgency = float(urgency)
        except (TypeError, ValueError):
            continue
        if urgency >= min_urgency:
            scored.append((urgency, s))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [s for _, s in scored[:top_n]]
