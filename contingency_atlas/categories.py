"""Independent, project-native taxonomy helpers for Contingency Atlas.

The rules are intentionally compact and generic. They classify the synthetic
demo corpus and model-generated hypotheses without reproducing a third-party
taxonomy, identifier system, or scenario library.
"""

from __future__ import annotations

import re
from typing import Any

from .db import from_json_str


TAXONOMY_VERSION = "atlas-1.0"

# External drivers are a second, multi-label lens over the operational-risk
# taxonomy.  The labels are intentionally project-native and describe broad
# sources of change rather than risk outcomes.
EXTERNAL_DRIVERS = (
    "Technology",
    "Institutional change",
    "Demographic change",
    "Environment",
    "Societal values",
    "Cross-driver convergence",
)
SIGNAL_MATURITY_STAGES = (
    "weak",
    "emerging",
    "accelerating",
    "established",
    "materialized",
    "fading",
)
MOMENTUM_STATES = ("accelerating", "stable", "slowing")
EVIDENCE_STRENGTH_LEVELS = ("unrated", "limited", "moderate", "strong")

EXTERNAL_DRIVER_RULES: list[tuple[str, tuple[str, ...]]] = [
    (
        "Technology",
        (
            "technology",
            "digital",
            "cyber",
            "cloud",
            "artificial intelligence",
            "machine learning",
            "algorithm",
            "automation",
            "quantum",
            "data infrastructure",
        ),
    ),
    (
        "Institutional change",
        (
            "institutional",
            "regulation",
            "regulator",
            "legislation",
            "public policy",
            "legal change",
            "standard setting",
            "central bank",
            "sanction",
        ),
    ),
    (
        "Demographic change",
        (
            "demographic",
            "population",
            "ageing",
            "aging",
            "migration",
            "generational",
            "skills shortage",
            "talent shortage",
            "workforce composition",
        ),
    ),
    (
        "Environment",
        (
            "environment",
            "climate",
            "flood",
            "storm",
            "wildfire",
            "extreme heat",
            "water stress",
            "biodiversity",
            "carbon",
            "energy transition",
        ),
    ),
    (
        "Societal values",
        (
            "societal",
            "social norm",
            "public trust",
            "consumer trust",
            "privacy expectation",
            "misinformation",
            "ethics",
            "inequality",
            "public expectation",
        ),
    ),
    (
        "Cross-driver convergence",
        (
            "cross-driver",
            "cross-domain",
            "compound risk",
            "risk convergence",
            "interconnected drivers",
        ),
    ),
]

# Ordered rules: the first matching project category wins. Terms describe broad
# operational-risk concepts rather than named incidents or provider categories.
CATEGORY_RULES: list[tuple[str, tuple[str, ...]]] = [
    (
        "Financial crime controls",
        (
            "money laundering",
            "sanction",
            "fraud control",
            "transaction monitoring",
            "screening backlog",
            "financial crime",
        ),
    ),
    (
        "Third-party dependency",
        (
            "third party",
            "third-party",
            "vendor",
            "supplier",
            "outsourc",
            "shared service",
            "service provider",
        ),
    ),
    (
        "Digital resilience",
        (
            "cyber",
            "ransomware",
            "identity",
            "credential",
            "cloud",
            "technology outage",
            "system outage",
            "service interruption",
        ),
    ),
    (
        "Model risk",
        (
            "model drift",
            "model failure",
            "machine learning",
            "algorithm",
            "automated decision",
            "artificial intelligence",
        ),
    ),
    (
        "Data governance",
        (
            "data quality",
            "data lineage",
            "reference data",
            "data integrity",
            "data drift",
            "record accuracy",
        ),
    ),
    (
        "Processing integrity",
        (
            "processing error",
            "process error",
            "duplicate payment",
            "reconciliation",
            "settlement failure",
            "change failure",
            "control breakdown",
        ),
    ),
    (
        "Workforce and conduct",
        (
            "conduct",
            "mis-selling",
            "insider",
            "workforce",
            "staff",
            "human error",
            "privileged access misuse",
        ),
    ),
    (
        "Physical resilience",
        (
            "flood",
            "storm",
            "wildfire",
            "earthquake",
            "physical site",
            "operations center",
            "power loss",
            "climate",
        ),
    ),
    (
        "Legal and regulatory",
        (
            "legal",
            "litigation",
            "regulatory change",
            "compliance obligation",
            "court",
        ),
    ),
    (
        "Market infrastructure",
        (
            "settlement",
            "clearing",
            "collateral",
            "liquidity",
            "market infrastructure",
            "payment rail",
        ),
    ),
    (
        "Geopolitical disruption",
        (
            "geopolitical",
            "trade restriction",
            "cross-border disruption",
            "civil unrest",
            "armed conflict",
        ),
    ),
]

RISK_CATEGORIES = tuple(category for category, _ in CATEGORY_RULES) + (
    "Multi-risk convergence",
)
EVENT_DRIVER_CLASSES = (
    "Technology disruption",
    "Third-party failure",
    "Process failure",
    "Human action",
    "External disruption",
    "Physical disruption",
    "Model failure",
    "Data integrity",
    "Cross-domain convergence",
)
PRIMARY_CAUSE_CLASSES = (
    "Architecture concentration",
    "Supplier concentration",
    "Change failure",
    "Monitoring gap",
    "Access control",
    "Data quality",
    "Site dependency",
    "Governance weakness",
    "External dependency",
    "Process design",
)
# The geographic vocabulary both evidence producers draw from. It lives here,
# not inside one lane's prompt, because the deep-research lane shipped without
# these fields at all and the two lanes silently disagreed about the contract.
REGIONS = (
    "North America",
    "Europe",
    "Asia Pacific",
    "Africa",
    "Middle East",
    "Latin America",
    # "Global" is the honest answer when an event has no single geography.
    # Country names are intentionally excluded because they do not satisfy the
    # region-level contract.
    "Global",
)
FINANCIAL_IMPACT_BANDS = (
    "Extreme",
    "Major",
    "Significant",
    "Moderate",
    "Limited",
)
IMPACT_BANDS = ("Critical", "High", "Medium", "Low")


def _text_blob(scenario: dict[str, Any]) -> str:
    """Collect normalized searchable text from a scenario."""
    parts = [
        scenario.get("title") or "",
        scenario.get("description") or "",
        scenario.get("scenario_event_name") or "",
    ]
    for field_name in (
        "core_risks",
        "theme_tags",
        "external_drivers",
        "affected_capabilities",
        "second_order_effects",
    ):
        raw = scenario.get(field_name)
        if not raw:
            continue
        if isinstance(raw, str):
            raw = from_json_str(raw) or raw
        if isinstance(raw, list):
            parts.extend(str(item) for item in raw)
        else:
            parts.append(str(raw))
    return " ".join(parts).lower()


def derive_external_drivers(value: object) -> list[str]:
    """Infer canonical external-driver labels from scenario or signal text."""
    if isinstance(value, dict):
        blob = _text_blob(value)
    elif isinstance(value, (list, tuple, set)):
        blob = " ".join(str(item) for item in value).lower()
    else:
        blob = str(value or "").lower()

    labels = [
        label
        for label, terms in EXTERNAL_DRIVER_RULES
        if any(term in blob for term in terms)
    ]
    return labels


def normalize_external_drivers(
    values: object,
    *,
    fallback_text: object = "",
) -> list[str]:
    """Normalize aliases and reject non-project external-driver labels."""
    if isinstance(values, str):
        parsed = from_json_str(values)
        if isinstance(parsed, list):
            values = parsed
        else:
            values = [part.strip() for part in values.split(",")]
    if not isinstance(values, (list, tuple, set)):
        values = [values] if values else []

    aliases = {
        "technology": "Technology",
        "technological change": "Technology",
        "institutional change": "Institutional change",
        "institutional and policy change": "Institutional change",
        "demographic change": "Demographic change",
        "demographics": "Demographic change",
        "environment": "Environment",
        "environmental change": "Environment",
        "societal values": "Societal values",
        "social and behavioral change": "Societal values",
        "social and behavioural change": "Societal values",
        "cross-driver convergence": "Cross-driver convergence",
        "cross-domain convergence": "Cross-driver convergence",
    }
    selected = {
        aliases.get(str(value).strip().lower(), "")
        for value in values
        if str(value).strip()
    }
    normalized = [label for label in EXTERNAL_DRIVERS if label in selected]
    if normalized:
        return normalized
    if fallback_text:
        return derive_external_drivers(fallback_text)
    return []


def normalize_signal_maturity(value: object, *, default: str = "emerging") -> str:
    """Return a canonical signal-maturity stage."""
    normalized = str(value or "").strip().lower()
    if normalized in SIGNAL_MATURITY_STAGES:
        return normalized
    return default


def normalize_momentum(
    value: object,
    *,
    legacy_trend: object = "",
) -> str:
    """Return canonical momentum while accepting legacy trend vocabulary."""
    aliases = {
        "rising": "accelerating",
        "accelerating": "accelerating",
        "stable": "stable",
        "steady": "stable",
        "fading": "slowing",
        "slowing": "slowing",
        "decelerating": "slowing",
    }
    return aliases.get(
        str(value or "").strip().lower(),
        aliases.get(str(legacy_trend or "").strip().lower(), "stable"),
    )


def legacy_trend_for_momentum(momentum: object) -> str:
    """Map canonical momentum back to the retained Phase 2 trend field."""
    return {
        "accelerating": "rising",
        "slowing": "fading",
    }.get(normalize_momentum(momentum), "stable")


def derive_evidence_strength(evidence_refs: object) -> str:
    """Derive citation coverage from distinct references, without model claims."""
    if isinstance(evidence_refs, str):
        parsed = from_json_str(evidence_refs)
        evidence_refs = parsed if isinstance(parsed, list) else [evidence_refs]
    if not isinstance(evidence_refs, (list, tuple, set)):
        evidence_refs = []
    distinct = {
        str(ref).strip()
        for ref in evidence_refs
        if str(ref).strip()
    }
    # Scenario provenance may retain both a local event ID and its canonical
    # source URL. Count public citations when present so one record does not
    # become two apparent sources merely because it has two identifiers.
    public_refs = {
        ref
        for ref in distinct
        if ref.lower().startswith(("https://", "http://"))
    }
    count = len(public_refs or distinct)
    if count == 0:
        return "unrated"
    if count == 1:
        return "limited"
    if count == 2:
        return "moderate"
    return "strong"


def derive_category(scenario: dict[str, Any]) -> str:
    """Derive an Atlas risk category from scenario text."""
    category, _ = infer_category(scenario)
    return category or "Multi-risk convergence"


def infer_category(scenario: dict[str, Any]) -> tuple[str | None, str]:
    """Return ``(category, matched_term)`` or ``(None, "")``."""
    blob = _text_blob(scenario)
    for category, terms in CATEGORY_RULES:
        for term in terms:
            if term in blob:
                return category, term
    return None, ""


def derive_financial_impact_band(text: str) -> str:
    """Map an impact description to the independent Atlas 1.0 bands."""
    if not text:
        return "Moderate"
    normalized = text.lower()
    amounts = re.findall(
        r"(?:usd|\$)?\s*([\d,.]+)\s*"
        r"(trillion|billion|million|tn|bn|mm|[btm])\b",
        normalized,
    )
    maximum = 0.0
    for amount, unit in amounts:
        try:
            value = float(amount.replace(",", ""))
        except ValueError:
            continue
        if unit in {"trillion", "tn", "t"}:
            value *= 1e12
        elif unit in {"billion", "bn", "b"}:
            value *= 1e9
        else:
            value *= 1e6
        maximum = max(maximum, value)

    if maximum >= 10e9 or "extreme" in normalized:
        return "Extreme"
    if maximum >= 1e9 or any(
        term in normalized for term in ("major", "severe", "material")
    ):
        return "Major"
    if maximum >= 100e6 or "significant" in normalized:
        return "Significant"
    if maximum >= 10e6 or "moderate" in normalized:
        return "Moderate"
    if maximum > 0 or any(
        term in normalized for term in ("limited", "minor", "low")
    ):
        return "Limited"
    return "Moderate"


def normalize_region(value: object) -> str | None:
    """Return the canonical region name, or None when it is not one of them.

    Unlike the class derivers there is no keyword fallback: geography cannot be
    guessed from risk language, so an unrecognized value becomes None rather
    than a confident wrong answer.
    """
    text = str(value or "").strip()
    if not text:
        return None
    for region in REGIONS:
        if text.lower() == region.lower():
            return region
    return None


def derive_event_driver_class(core_risks: list[str]) -> str:
    """Derive a project-native driver class from broad risk concepts."""
    risks = " ".join(core_risks).lower()
    if any(term in risks for term in ("model", "algorithm", "machine learning")):
        return "Model failure"
    if any(term in risks for term in ("data", "record", "reference")):
        return "Data integrity"
    if any(term in risks for term in ("vendor", "supplier", "third party")):
        return "Third-party failure"
    if any(
        term in risks
        for term in ("cyber", "identity", "credential", "cloud", "technology", "system")
    ):
        return "Technology disruption"
    if any(
        term in risks
        for term in ("flood", "storm", "wildfire", "earthquake", "physical", "climate")
    ):
        return "Physical disruption"
    if any(term in risks for term in ("staff", "people", "human", "conduct", "insider")):
        return "Human action"
    if any(
        term in risks
        for term in ("geopolitical", "cross-border", "sanction", "external")
    ):
        return "External disruption"
    if any(term in risks for term in ("process", "control", "change", "payment")):
        return "Process failure"
    return "Cross-domain convergence"


def derive_primary_cause_class(core_risks: list[str]) -> str:
    """Derive a project-native primary-cause class from broad risk concepts."""
    risks = " ".join(core_risks).lower()
    if any(term in risks for term in ("vendor", "supplier", "third party")):
        return "Supplier concentration"
    if any(term in risks for term in ("concentration", "shared", "common service")):
        return "Architecture concentration"
    if any(term in risks for term in ("change", "release", "configuration", "deploy")):
        return "Change failure"
    if any(term in risks for term in ("monitor", "alert", "detect", "screening")):
        return "Monitoring gap"
    if any(term in risks for term in ("access", "credential", "privilege", "identity")):
        return "Access control"
    if any(term in risks for term in ("data", "record", "reference")):
        return "Data quality"
    if any(term in risks for term in ("site", "facility", "physical", "location")):
        return "Site dependency"
    if any(term in risks for term in ("governance", "policy", "oversight")):
        return "Governance weakness"
    if any(
        term in risks
        for term in ("geopolitical", "climate", "external", "cross-border")
    ):
        return "External dependency"
    return "Process design"


def derive_impact_band(likelihood: str, financial_text: str) -> str:
    """Combine likelihood and financial impact into an Atlas impact band."""
    financial_band = derive_financial_impact_band(financial_text)
    likelihood_value = (likelihood or "").lower()

    if financial_band == "Extreme":
        return "Critical"
    if "very high" in likelihood_value and financial_band in {
        "Major",
        "Significant",
    }:
        return "Critical"
    if financial_band == "Major" or "high" in likelihood_value:
        return "High"
    if financial_band in {"Significant", "Moderate"} or "moderate" in likelihood_value:
        return "Medium"
    return "Low"
