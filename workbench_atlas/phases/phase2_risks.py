"""Phase 2: Emerging Risk Monitoring.

Monitors the risk landscape for new emerging risks and updates the
emerging_risks.json catalog. The LLM:

  1. Reads source material (regulatory consultations, threat intel,
     geopolitical analysis, technology trends)
  2. Identifies new risks not yet in the catalog
  3. Assesses existing risks — have they materialized? Intensified? Faded?
  4. Generates structured risk entries (issue + description)

Output: Updated emerging_risks.json with new entries and status flags.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from ..categories import (
    derive_evidence_strength,
    legacy_trend_for_momentum,
    normalize_external_drivers,
    normalize_momentum,
    normalize_signal_maturity,
)
from ..guard_hook import risk_record, validate
from ..snapshots import snapshot_before_write
from ..llm import OMLXClient, LLMError
from ..provenance import all_public_http_urls

logger = logging.getLogger(__name__)


# ── Data models ──

@dataclass
class EmergingRisk:
    """A single emerging risk entry."""
    issue: str
    description: str
    status: str = "active"  # active, monitoring, faded
    first_seen: str = ""
    last_reviewed: str = ""
    trend: str = "stable"  # rising, stable, fading
    external_drivers: list[str] = field(default_factory=list)
    signal_maturity: str = "emerging"
    momentum: str = "stable"
    evidence_strength: str = "unrated"
    first_observed: str = ""
    last_confirmed: str = ""
    related_scenarios: list[str] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    source_type: str = "public_research"
    provenance_note: str = ""
    review_status: str = "pending"

    def to_dict(self) -> dict[str, Any]:
        return {
            "issue": self.issue,
            "description": self.description,
            "status": self.status,
            "first_seen": self.first_seen,
            "last_reviewed": self.last_reviewed,
            "trend": self.trend,
            "external_drivers": self.external_drivers,
            "signal_maturity": self.signal_maturity,
            "momentum": self.momentum,
            "evidence_strength": self.evidence_strength,
            "first_observed": self.first_observed,
            "last_confirmed": self.last_confirmed,
            "related_scenarios": self.related_scenarios,
            "evidence_refs": self.evidence_refs,
            "source_type": self.source_type,
            "provenance_note": self.provenance_note,
            "review_status": self.review_status,
        }


@dataclass
class MonitoringResult:
    """Result of a risk monitoring cycle."""
    new_risks: list[EmergingRisk] = field(default_factory=list)
    updated_risks: list[EmergingRisk] = field(default_factory=list)
    retired_risks: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


# ── LLM prompts ──

PROMPT_VERSION = "phase2-risk-identification-v1"

IDENTIFY_PROMPT = """You are monitoring operational risk for a hypothetical
global bank in an independent research system.

Given the source text below, identify any NEW emerging risks that are NOT already
in this list of known risks:
{known_risks}

For each new risk, provide:
- issue: short name (e.g., "Quantum Computing Threats")
- description: 2-3 sentences explaining the risk and why it matters for a bank
- trend: "rising" if escalating, "stable" if steady, "fading" if declining
- external_drivers: one or more labels chosen only from "Technology",
  "Institutional change", "Demographic change", "Environment",
  "Societal values", "Cross-driver convergence"
- signal_maturity: one of "weak", "emerging", "accelerating", "established",
  "materialized", "fading"
- momentum: one of "accelerating", "stable", "slowing"

Return JSON: {{"new_risks": [{{"issue": "...", "description": "...",
"trend": "...", "external_drivers": ["..."], "signal_maturity": "...",
"momentum": "..."}}]}}

The description must remain a research hypothesis grounded only in the supplied
packet. Do not invent statistics, assert a weakness at a named organization, or
present model output as evidence.

Public source references:
{source_refs}

Source text:
{source_text}"""

ASSESS_PROMPT = """You are assessing whether existing research hypotheses have
changed based only on a new cited public-source packet.

For each risk below, has it:
- Intensified (growing more likely/impactful)? → trend: "rising"
- Faded (less relevant than before)? → status: "faded", trend: "fading"
- Stayed the same? → status: "active", trend: "stable"

Also reassess signal_maturity, momentum, and external_drivers using only the
allowed vocabularies supplied on each record. Treat maturity as the observable
development stage and momentum as its current direction; they are not impact
or likelihood scores.

Do not change a risk unless the supplied text supports the change. Do not invent
facts or make unsupported claims about a named organization.

Risks to assess:
{risks_json}

Public source references:
{source_refs}

Source text:
{source_text}

Return JSON: {{"assessments": [{{"issue": "...", "status": "...",
"trend": "...", "external_drivers": ["..."], "signal_maturity": "...",
"momentum": "...", "notes": "..."}}]}}"""


# ── Core functions ──

def _valid_public_refs(source_refs: list[str]) -> bool:
    return all_public_http_urls(source_refs)


def normalize_risk_record(record: dict[str, Any]) -> dict[str, Any]:
    """Add radar fields to a risk record without discarding legacy fields."""
    normalized = dict(record)
    raw_refs = normalized.get("evidence_refs") or []
    if isinstance(raw_refs, str):
        raw_refs = [raw_refs]
    refs = [
        str(ref).strip()
        for ref in raw_refs
        if str(ref).strip()
    ]
    normalized["evidence_refs"] = refs

    first_seen = str(normalized.get("first_seen") or "").strip()
    first_observed = str(normalized.get("first_observed") or first_seen).strip()
    normalized["first_seen"] = first_seen or first_observed
    normalized["first_observed"] = first_observed or first_seen

    last_confirmed = str(normalized.get("last_confirmed") or "").strip()
    last_reviewed = str(
        normalized.get("last_reviewed") or last_confirmed
    ).strip()
    if not last_confirmed and str(normalized.get("review_status") or "").startswith(
        "approved"
    ):
        last_confirmed = last_reviewed
    normalized["last_reviewed"] = last_reviewed
    normalized["last_confirmed"] = last_confirmed

    status = str(normalized.get("status") or "active").lower()
    if status not in {"active", "monitoring", "faded"}:
        status = "active"
    normalized["status"] = status

    momentum = normalize_momentum(
        normalized.get("momentum"),
        legacy_trend=normalized.get("trend"),
    )
    normalized["momentum"] = momentum
    normalized["trend"] = legacy_trend_for_momentum(momentum)

    default_maturity = "fading" if status == "faded" else "emerging"
    normalized["signal_maturity"] = normalize_signal_maturity(
        normalized.get("signal_maturity"),
        default=default_maturity,
    )
    normalized["external_drivers"] = normalize_external_drivers(
        normalized.get("external_drivers"),
        fallback_text=f"{normalized.get('issue', '')} {normalized.get('description', '')}",
    )
    normalized["evidence_strength"] = derive_evidence_strength(refs)
    normalized.setdefault("related_scenarios", [])
    normalized.setdefault("source_type", "public_research")
    normalized.setdefault("provenance_note", "")
    normalized.setdefault("review_status", "pending")
    return normalized


def load_emerging_risks(file_path: str | Path) -> list[dict[str, Any]]:
    """Load the current emerging risks catalog."""
    p = Path(file_path)
    if not p.exists():
        return []
    with open(p) as f:
        data = json.load(f)
    if not isinstance(data, list):
        return []
    return [
        normalize_risk_record(record)
        for record in data
        if isinstance(record, dict)
    ]


def save_emerging_risks(file_path: str | Path, risks: list[dict[str, Any]]) -> None:
    """Save the emerging risks catalog (atomic write).

    The batch passes the ontology guard (ontology/guard.py) before the
    replace: FLAGs are logged and admitted; a REJECT raises GuardRejected
    and the previous register stands (no-op when ATLAS_GUARD=off)."""
    report = validate([risk_record(r) for r in risks], seam="save_emerging_risks")
    report.raise_if_rejected("save_emerging_risks")
    p = Path(file_path)
    # Human review decisions cannot be regenerated by re-running anything, so
    # the outgoing content is preserved before it is replaced (a 2026-08-04
    # cycle overwrote them and every backup was already too late).
    snapshot_before_write(p)
    tmp = p.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(risks, f, indent=2, ensure_ascii=False)
    os.replace(tmp, p)
    logger.info(f"Saved {len(risks)} emerging risks to {p}")


def identify_new_risks(
    source_text: str,
    known_risks: list[str],
    client: OMLXClient,
    source_refs: list[str],
) -> list[EmergingRisk]:
    """Use the LLM to identify new emerging risks from source text.

    Args:
        source_text: Intelligence source material (regulatory, threat, news)
        known_risks: List of existing risk issue names to avoid duplicates
        client: OMLX client
        source_refs: Canonical public URLs supporting the source packet

    Returns:
        List of new EmergingRisk objects
    """
    if not _valid_public_refs(source_refs):
        logger.warning("Risk identification requires canonical public source URLs")
        return []
    known_str = "\n".join(f"- {r}" for r in known_risks[:50])  # Cap context
    prompt = IDENTIFY_PROMPT.format(
        known_risks=known_str,
        source_refs="\n".join(f"- {ref}" for ref in source_refs),
        source_text=source_text[:8000],
    )

    try:
        result = client.chat_json(
            [{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=4096,
        )
    except (LLMError, httpx.HTTPError) as e:
        logger.warning(f"Risk identification LLM call failed: {e}")
        return []
    except Exception as e:
        logger.error(f"Risk identification failed unexpectedly: {e}", exc_info=True)
        return []

    new_risks = []
    now = datetime.now(timezone.utc).isoformat()
    items = result.get("new_risks", []) if isinstance(result, dict) else []
    for item in items:
        if not isinstance(item, dict):
            logger.warning(f"Skipping malformed new_risks entry: {item!r}")
            continue
        issue = str(item.get("issue") or "").strip()
        desc = str(item.get("description") or "").strip()
        if not issue or not desc:
            continue
        # Skip if already known (case-insensitive)
        if any(issue.lower() == kr.lower() for kr in known_risks):
            continue
        trend = str(item.get("trend") or "stable").lower()
        if trend not in {"rising", "stable", "fading"}:
            trend = "stable"
        momentum = normalize_momentum(
            item.get("momentum"),
            legacy_trend=trend,
        )
        maturity = normalize_signal_maturity(item.get("signal_maturity"))
        external_drivers = normalize_external_drivers(
            item.get("external_drivers"),
            fallback_text=f"{issue} {desc}",
        )
        new_risks.append(EmergingRisk(
            issue=issue,
            description=desc,
            trend=legacy_trend_for_momentum(momentum),
            first_seen=now,
            last_reviewed=now,
            external_drivers=external_drivers,
            signal_maturity=maturity,
            momentum=momentum,
            evidence_strength=derive_evidence_strength(source_refs),
            first_observed=now,
            last_confirmed=now,
            evidence_refs=list(source_refs),
            source_type="public_research",
            provenance_note=(
                "Model-assisted hypothesis derived from the cited public-source "
                "packet; pending human review."
            ),
            review_status="pending",
        ))
        logger.info(f"New risk identified: {issue}")

    return new_risks


def assess_existing_risks(
    risks: list[dict[str, Any]],
    client: OMLXClient,
    source_text: str,
    source_refs: list[str],
) -> list[dict[str, Any]]:
    """Use the LLM to assess whether existing risks have changed.

    Returns the risks with updated status and trend fields.
    """
    for index, risk in enumerate(risks):
        if isinstance(risk, dict):
            risks[index] = normalize_risk_record(risk)

    # Only assess risks that have descriptions
    assessable = [
        {
            "issue": r.get("issue", ""),
            "description": r.get("description", "")[:200],
            "external_drivers": r.get("external_drivers", []),
            "signal_maturity": r.get("signal_maturity", "emerging"),
            "momentum": r.get("momentum", "stable"),
        }
        for r in risks
        if isinstance(r, dict) and r.get("issue")
    ]
    if not assessable:
        return risks

    if not _valid_public_refs(source_refs):
        logger.warning("Risk assessment requires canonical public source URLs")
        return risks
    prompt = ASSESS_PROMPT.format(
        risks_json=json.dumps(assessable[:50], indent=2),
        source_refs="\n".join(f"- {ref}" for ref in source_refs),
        source_text=source_text[:8000],
    )

    try:
        result = client.chat_json(
            [{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=4096,
        )
    except (LLMError, httpx.HTTPError) as e:
        logger.warning(f"Risk assessment LLM call failed: {e}")
        return risks
    except Exception as e:
        logger.error(f"Risk assessment failed unexpectedly: {e}", exc_info=True)
        return risks

    raw = result.get("assessments", []) if isinstance(result, dict) else []
    assessments = {
        a["issue"]: a for a in raw if isinstance(a, dict) and a.get("issue")
    }
    now = datetime.now(timezone.utc).isoformat()

    for risk in risks:
        if not isinstance(risk, dict):
            continue
        issue = risk.get("issue", "")
        if issue in assessments:
            a = assessments[issue]
            status = str(a.get("status") or risk.get("status") or "active").lower()
            trend = str(a.get("trend") or risk.get("trend") or "stable").lower()
            risk["status"] = (
                status if status in {"active", "monitoring", "faded"} else "active"
            )
            if trend not in {"rising", "stable", "fading"}:
                trend = risk.get("trend", "stable")
            momentum = normalize_momentum(
                a.get("momentum"),
                legacy_trend=trend,
            )
            risk["momentum"] = momentum
            risk["trend"] = legacy_trend_for_momentum(momentum)
            default_maturity = (
                "fading"
                if risk["status"] == "faded"
                else risk.get("signal_maturity", "emerging")
            )
            risk["signal_maturity"] = normalize_signal_maturity(
                a.get("signal_maturity"),
                default=default_maturity,
            )
            risk["external_drivers"] = normalize_external_drivers(
                a.get("external_drivers") or risk.get("external_drivers"),
                fallback_text=f"{risk.get('issue', '')} {risk.get('description', '')}",
            )
            risk["last_reviewed"] = now
            risk["last_confirmed"] = now
            # Human decisions are terminal for the cycle. A re-assessed record
            # keeps its review state: approved moves to needs_update (the
            # DATA_PROVENANCE.md state for changed-after-review), rejected/
            # superseded stay untouched. Only undecided records go (back) to
            # pending and accrue the packet's raw URL refs; decided records
            # keep their human-curated refs. (Previously this wiped
            # review_status and provenance_note on every cycle, silently
            # discarding human approvals.)
            previous = str(risk.get("review_status") or "pending")
            decided = previous in {
                "approved", "approved_synthetic", "rejected",
                "superseded", "needs_update",
            }
            if not decided:
                risk["evidence_refs"] = sorted(
                    set(risk.get("evidence_refs") or []) | set(source_refs)
                )
                risk["review_status"] = "pending"
            elif previous in ("approved", "approved_synthetic"):
                risk["review_status"] = "needs_update"
            risk["evidence_strength"] = derive_evidence_strength(
                risk["evidence_refs"]
            )
            update_note = (
                "Assessment updated from the cited public-source packet"
                + ("; pending human review." if not decided
                   else "; previously reviewed record preserved.")
            )
            existing_note = str(risk.get("provenance_note") or "").strip()
            risk["provenance_note"] = (
                f"{existing_note}\n{update_note}" if existing_note else update_note
            )
        else:
            if not risk.get("last_reviewed"):
                risk["last_reviewed"] = now

    return risks


def run_monitoring_cycle(
    source_text: str,
    emerging_risks_path: str | Path,
    client: OMLXClient,
    *,
    source_refs: list[str] | None = None,
) -> MonitoringResult:
    """Run a full risk monitoring cycle.

    1. Load existing risks
    2. Identify new risks from source text
    3. Assess existing risks for changes
    4. Save updated catalog

    Returns a MonitoringResult with new/updated/retired risks.
    """
    result = MonitoringResult()
    now = datetime.now(timezone.utc).isoformat()
    refs = [str(ref).strip() for ref in (source_refs or []) if str(ref).strip()]
    if not source_text.strip() or not _valid_public_refs(refs):
        result.errors.append(
            "Emerging-risk monitoring requires source text and canonical "
            "public source URLs"
        )
        return result

    # Hold the register lock for the whole read-modify-write span so a
    # concurrent reviewer (CLI or dashboard) cannot interleave — their
    # update would otherwise be silently lost on the save below. Lazy
    # import: review.py imports this module at load time.
    from ..review import register_lock

    with register_lock(emerging_risks_path, timeout=30.0):
        # Load current
        existing = load_emerging_risks(emerging_risks_path)
        known_issues = [r.get("issue", "") for r in existing if isinstance(r, dict)]

        # Identify new risks
        new_risks = identify_new_risks(source_text, known_issues, client, refs)
        result.new_risks = new_risks

        # Convert new risks to dict format and append
        for nr in new_risks:
            existing.append(nr.to_dict())

        # Assess existing risks (only original ones, not just-added)
        original_count = len(existing) - len(new_risks)
        if original_count > 0:
            existing[:original_count] = assess_existing_risks(
                existing[:original_count], client, source_text, refs
            )

        # Update result
        for r in existing[:original_count]:
            if not isinstance(r, dict):
                continue
            if r.get("status") == "faded":
                result.retired_risks.append(r.get("issue", ""))
            else:
                result.updated_risks.append(EmergingRisk(
                    issue=r.get("issue", ""),
                    description=r.get("description", ""),
                    status=r.get("status", "active"),
                    first_seen=r.get("first_seen", ""),
                    trend=r.get("trend", "stable"),
                    last_reviewed=r.get("last_reviewed", now),
                    external_drivers=list(r.get("external_drivers") or []),
                    signal_maturity=r.get("signal_maturity", "emerging"),
                    momentum=r.get("momentum", "stable"),
                    evidence_strength=r.get("evidence_strength", "unrated"),
                    first_observed=r.get("first_observed", ""),
                    last_confirmed=r.get("last_confirmed", ""),
                    related_scenarios=list(r.get("related_scenarios") or []),
                    evidence_refs=list(r.get("evidence_refs") or []),
                    source_type=r.get("source_type", "public_research"),
                    provenance_note=r.get("provenance_note", ""),
                    review_status=r.get("review_status", "pending"),
                ))

        # Save
        save_emerging_risks(emerging_risks_path, existing)

    logger.info(
        f"Monitoring cycle: {len(result.new_risks)} new, "
        f"{len(result.updated_risks)} updated, "
        f"{len(result.retired_risks)} faded"
    )
    return result
