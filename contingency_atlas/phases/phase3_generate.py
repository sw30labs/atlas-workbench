"""Phase 3: Scenario Generation.

Generates new Contingency Atlas scenarios by fusing:
  - Human-approved emerging risks (from Phase 2)
  - Human-approved historical event patterns (from event_evidence.db)

The LLM identifies *convergences* — where multiple known risks could
synchronize into a high-impact event that's individually foreseeable but
collectively surprising. This is the core "Contingency Atlas" synthesis step.

Uses the Ralph Loop (Tenacity) with LLM-as-a-Judge for QA before insertion.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx

from ..llm import OMLXClient, LLMError
from ..provenance import is_approved_record
from ..db import (
    list_scenarios,
    upsert_scenario,
    list_evidence_events,
    to_json_str,
    log_qa,
    list_qa_scenario_ids,
)
from ..loops import ralph_loop
from ..judge import JudgeResult
from ..categories import (
    derive_category,
    derive_evidence_strength,
    derive_financial_impact_band,
    derive_event_driver_class,
    derive_primary_cause_class,
    derive_impact_band,
    normalize_external_drivers,
    normalize_momentum,
    normalize_signal_maturity,
)

logger = logging.getLogger(__name__)
PROMPT_VERSION = "phase3-scenario-generation-v1"


# ── Generation prompt ──

GENERATE_PROMPT = """You are generating an operational-risk scenario for an
independent banking foresight engine.

Construct a partially foreseeable convergence whose timing and scale remain
uncertain. It should be neither a routine known issue nor an unfalsifiable
surprise. Its research value comes from identifying how multiple observable
risks or dependencies could synchronize into a high-impact event.

Available inputs:
- Emerging risks: {emerging_risks}
- Recent historical loss patterns: {loss_patterns}

Generate a scenario that combines 2-4 of these risks into a plausible
convergence. Return JSON with this structure:
{{
  "core_risks": ["risk1", "risk2", ...],
  "external_drivers": ["one or more canonical driver labels from the inputs"],
  "signal_maturity": "weak|emerging|accelerating|established|materialized|fading",
  "momentum": "accelerating|stable|slowing",
  "scenario_event": {{
    "name": "short event name",
    "description": "2-3 sentence description of the scenario",
    "scenario_horizon": "e.g. 2026-2028",
    "detailed_rationale": ["reason1", "reason2", ...],
    "affected_capabilities": ["banking capability or dependency", ...],
    "transmission_mechanism": "how signals reach dependencies, pressure controls, and create impacts",
    "second_order_effects": ["consequence beyond the immediate disruption", ...],
    "cross_industry_analogues": ["evidence-grounded mechanism analogue", ...],
    "impacts": {{
      "element_of_surprise": "why this would surprise",
      "likelihood": "qualitative: Very Low / Low / Moderate / High / Very High",
      "financial_impact": "estimated $ range and mechanism",
      "operational_impact": "operational consequences",
      "reputational_impact": "reputational consequences",
      "regulatory_impact": "regulatory consequences"
    }}
  }},
  "signposts": {{
    "strengthening": ["observable development that increases credibility", ...],
    "weakening": ["observable development that reduces credibility", ...]
  }},
  "conclusion": {{
    "summary": "1 sentence summary",
    "preparation_strategy": ["action1", "action2", ...],
    "defensive_options": ["near-term risk reduction option", ...],
    "resilience_opportunities": ["capability redesign or recovery improvement", ...],
    "strategic_options": ["reversible preparedness option", ...],
    "option_expiry_signals": ["observable condition after which an option loses value", ...],
    "challenged_assumptions": ["assumption this scenario tests", ...],
    "discussion_prompts": ["question for a short risk-team discussion", ...],
    "additional_information": "brief context"
  }},
  "theme_tags": ["tag1", "tag2"]
}}

Be specific. Ground claims in the supplied public-source patterns, named public
incidents, security advisories, or regulatory frameworks. Do not attribute a
hypothetical event to a named real organization, and do not invent statistics.
Use supplied event IDs or URLs when explaining evidence; model output is a
hypothesis and must never be presented as an observed fact.

External-driver labels must be selected only from: Technology, Institutional
change, Demographic change, Environment, Societal values, Cross-driver
convergence. Treat opportunities as operational resilience or preparedness
options, not unsupported commercial forecasts. Signposts must be observable
and falsifiable.
"""

SCENARIO_ID_TEMPLATE = "ATLAS-GEN-{year}-{seq:03d}"


@dataclass
class GenerationResult:
    """Result of a generation cycle."""
    generated: list[dict[str, Any]] = field(default_factory=list)
    inserted: int = 0
    rejected: int = 0
    errors: list[str] = field(default_factory=list)


# ── Helper functions ──

def _build_loss_patterns(db_path: str, limit: int = 20) -> str:
    """Extract recent loss patterns from the evidence database for context."""
    try:
        evidence_events = list_evidence_events(
            db_path,
            limit=limit,
            approved_only=True,
        )
    except Exception as e:
        logger.warning(f"Could not load loss patterns: {e}")
        return "No recent evidence events available."
    patterns = []
    for n in evidence_events[:limit]:
        headline = n.get("headline", "")
        cat = n.get("risk_category", "")
        loss = n.get("loss_amount_usd")
        event_id = n.get("event_id", "unidentified")
        source_url = n.get("source_url")
        if headline:
            loss_str = f" (${loss:,.0f})" if loss else ""
            source = f"; source={source_url}" if source_url else "; synthetic"
            patterns.append(
                f"- [{event_id}] {cat}: {headline}{loss_str}{source}"
            )
    return "\n".join(patterns) if patterns else "No recent evidence events available."


def _format_emerging_risks(risks: list[dict[str, Any]], limit: int = 15) -> str:
    """Format emerging risks for the prompt."""
    items = []
    approved = [
        risk
        for risk in risks
        if (
            isinstance(risk, dict)
            and is_approved_record(risk)
            and risk.get("issue")
        )
    ]
    for risk in approved[:limit]:
        risk_refs = risk.get("evidence_refs") or []
        if isinstance(risk_refs, str):
            risk_refs = [risk_refs]
        refs = ", ".join(
            str(ref) for ref in risk_refs
        )
        suffix = f" [refs: {refs}]" if refs else ""
        drivers = normalize_external_drivers(
            risk.get("external_drivers"),
            fallback_text=f"{risk.get('issue', '')} {risk.get('description', '')}",
        )
        radar = (
            f"drivers={', '.join(drivers)}; "
            f"maturity={normalize_signal_maturity(risk.get('signal_maturity'))}; "
            f"momentum={normalize_momentum(risk.get('momentum'), legacy_trend=risk.get('trend'))}"
        )
        items.append(
            f"- {risk['issue']}: {risk.get('description', '')[:100]} "
            f"[{radar}]{suffix}"
        )
    return "\n".join(items) if items else "No emerging risks available."


def _collect_evidence_refs(db_path: str, limit: int = 20) -> list[str]:
    try:
        rows = list_evidence_events(db_path, limit=limit, approved_only=True)
    except Exception:
        return []
    refs: list[str] = []
    for row in rows:
        for value in (row.get("event_id"), row.get("source_url")):
            if value and str(value) not in refs:
                refs.append(str(value))
    return refs


def _collect_risk_refs(risks: list[dict[str, Any]]) -> list[str]:
    refs: list[str] = []
    for risk in risks:
        if not isinstance(risk, dict) or not is_approved_record(risk):
            continue
        risk_refs = risk.get("evidence_refs") or []
        if isinstance(risk_refs, str):
            risk_refs = [risk_refs]
        for value in risk_refs:
            if value and str(value) not in refs:
                refs.append(str(value))
    return refs


def generate_scenario(
    client: OMLXClient,
    *,
    emerging_risks: list[dict[str, Any]],
    loss_patterns: str,
    existing_scenario_ids: list[str] | None = None,
    scenario_id: str | None = None,
) -> dict[str, Any] | None:
    """Generate a single scenario via the LLM.

    Returns a dict with the full scenario structure, or None on failure.
    ``scenario_id`` (when the caller has already assigned one) tags the
    telemetry so generation tokens attribute to the scenario.
    """
    prompt = GENERATE_PROMPT.format(
        emerging_risks=_format_emerging_risks(emerging_risks),
        loss_patterns=loss_patterns,
    )

    try:
        result = client.chat_json(
            [{"role": "user", "content": prompt}],
            temperature=0.5,  # Creative but not random
            max_tokens=4096,
            scenario_id=scenario_id,
        )
    except (LLMError, httpx.HTTPError) as e:
        logger.warning(f"Scenario generation LLM call failed: {e}")
        return None
    except Exception as e:
        logger.error(f"Scenario generation failed unexpectedly: {e}", exc_info=True)
        return None

    # Validate structure
    if "scenario_event" not in result or "core_risks" not in result:
        logger.warning("Generated scenario missing required fields")
        return None

    event = result["scenario_event"]
    if "name" not in event or "description" not in event:
        logger.warning("Generated scenario missing event name/description")
        return None

    return result


def _clean_list(value: object) -> list[str]:
    """Return a compact list from model output or a stored JSON list."""
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            decoded = None
        value = decoded if isinstance(decoded, list) else [value]
    if not isinstance(value, (list, tuple, set)):
        return []
    return [
        str(item).strip()
        for item in value
        if str(item).strip()
    ]


def convert_to_db_row(
    scenario: dict[str, Any],
    scenario_id: str,
    *,
    category: str | None = None,
    evidence_refs: list[str] | None = None,
) -> dict[str, Any]:
    """Convert a generated scenario to a DB row matching the extended schema.

    Maps the rich event structure to the DB columns and derives the neutral
    category, impact bands, event-driver class, and primary-cause class.
    """
    event = scenario.get("scenario_event", {})
    if not isinstance(event, dict):
        event = {}
    impacts = event.get("impacts", {})
    if not isinstance(impacts, dict):
        impacts = {}
    conclusion = scenario.get("conclusion", {})
    if not isinstance(conclusion, dict):
        conclusion = {}
    signposts = scenario.get("signposts", {})
    if not isinstance(signposts, dict):
        signposts = {}

    # Derive category
    if not category:
        category = derive_category(scenario)

    # Derive financial impact band
    fin_impact = impacts.get("financial_impact", "")
    fin_band = derive_financial_impact_band(fin_impact)

    event_driver = derive_event_driver_class(scenario.get("core_risks", []))
    primary_cause = derive_primary_cause_class(scenario.get("core_risks", []))

    impact_band = derive_impact_band(impacts.get("likelihood", ""), fin_impact)
    refs = _clean_list(evidence_refs or [])
    external_drivers = normalize_external_drivers(
        scenario.get("external_drivers") or event.get("external_drivers"),
        fallback_text=scenario,
    )
    signal_maturity = normalize_signal_maturity(
        scenario.get("signal_maturity")
    )
    momentum = normalize_momentum(scenario.get("momentum"))
    strengthening_signposts = _clean_list(
        signposts.get("strengthening")
        or scenario.get("strengthening_signposts")
    )
    weakening_signposts = _clean_list(
        signposts.get("weakening")
        or scenario.get("weakening_signposts")
    )

    row = {
        "scenario_id": scenario_id,
        "origin": "pipeline_generated",
        "risk_category": category,
        "title": event.get("name", scenario_id),
        "description": event.get("description", ""),
        "created_year": datetime.now(timezone.utc).year,
        "financial_impact_band": fin_band,
        "event_driver_class": event_driver,
        "primary_cause_class": primary_cause,
        "impact_band": impact_band,
        # Extended columns
        "core_risks": to_json_str(_clean_list(scenario.get("core_risks", []))),
        "scenario_event_name": event.get("name", ""),
        "scenario_horizon": event.get("scenario_horizon", ""),
        "likelihood": impacts.get("likelihood", ""),
        "financial_impact_detail": fin_impact,
        "operational_impact": impacts.get("operational_impact", ""),
        "reputational_impact": impacts.get("reputational_impact", ""),
        "regulatory_impact": impacts.get("regulatory_impact", ""),
        "element_of_surprise": impacts.get("element_of_surprise", ""),
        "preparation_strategy": to_json_str(
            _clean_list(conclusion.get("preparation_strategy", []))
        ),
        "detailed_rationale": to_json_str(
            _clean_list(event.get("detailed_rationale", []))
        ),
        "additional_information": conclusion.get("additional_information", ""),
        "theme_tags": to_json_str(_clean_list(scenario.get("theme_tags", []))),
        "evidence_refs": to_json_str(refs),
        "external_drivers": to_json_str(external_drivers),
        "signal_maturity": signal_maturity,
        "momentum": momentum,
        "evidence_strength": derive_evidence_strength(refs),
        "affected_capabilities": to_json_str(
            _clean_list(
                event.get("affected_capabilities")
                or scenario.get("affected_capabilities")
            )
        ),
        "transmission_mechanism": str(
            event.get("transmission_mechanism")
            or scenario.get("transmission_mechanism")
            or ""
        ).strip(),
        "second_order_effects": to_json_str(
            _clean_list(
                event.get("second_order_effects")
                or scenario.get("second_order_effects")
            )
        ),
        "cross_industry_analogues": to_json_str(
            _clean_list(
                event.get("cross_industry_analogues")
                or scenario.get("cross_industry_analogues")
            )
        ),
        "strengthening_signposts": to_json_str(strengthening_signposts),
        "weakening_signposts": to_json_str(weakening_signposts),
        "defensive_options": to_json_str(
            _clean_list(
                conclusion.get("defensive_options")
                or scenario.get("defensive_options")
            )
        ),
        "resilience_opportunities": to_json_str(
            _clean_list(
                conclusion.get("resilience_opportunities")
                or scenario.get("resilience_opportunities")
            )
        ),
        "strategic_options": to_json_str(
            _clean_list(
                conclusion.get("strategic_options")
                or scenario.get("strategic_options")
            )
        ),
        "option_expiry_signals": to_json_str(
            _clean_list(
                conclusion.get("option_expiry_signals")
                or scenario.get("option_expiry_signals")
            )
        ),
        "challenged_assumptions": to_json_str(
            _clean_list(
                conclusion.get("challenged_assumptions")
                or scenario.get("challenged_assumptions")
            )
        ),
        "discussion_prompts": to_json_str(
            _clean_list(
                conclusion.get("discussion_prompts")
                or scenario.get("discussion_prompts")
            )
        ),
        "signpost_status": "not_evaluated",
        "signpost_assessment": "",
        "signpost_evidence_refs": to_json_str([]),
        "signpost_checked_evidence_refs": to_json_str([]),
        "signposts_last_evaluated": None,
        "signpost_review_status": "not_reviewed",
        "judge_score": None,
        "judge_feedback": "",
        "judge_iterations": 0,
        "urgency_score": None,
        "time_horizon": "",
        "controllability": "",
        "last_scored": None,
        "status": "draft",  # Starts as draft until judged
    }
    return row


# ── Main generation cycle ──

def run_generation_cycle(
    client: OMLXClient,
    *,
    db_path: str,
    emerging_risks_path: str,
    evidence_db_path: str | None = None,
    num_scenarios: int = 5,
    use_ralph_loop: bool = True,
    judge_threshold: float = 0.75,
    max_judge_iterations: int = 3,
) -> GenerationResult:
    """Run a full scenario generation cycle.

    For each scenario:
      1. Generate from LLM (approved emerging risks + approved evidence patterns)
      2. Convert to DB row
      3. Run Ralph Loop (Tenacity + LLM-as-Judge) for QA
      4. If passed, insert into DB; else reject

    Returns a GenerationResult with stats.
    """
    from pathlib import Path
    import json

    result = GenerationResult()
    # Load emerging risks
    risks_path = Path(emerging_risks_path)
    if risks_path.exists():
        with open(risks_path) as f:
            emerging_risks = json.load(f)
    else:
        emerging_risks = []

    # Load loss patterns from the evidence database (separate from scenario storage)
    loss_patterns = _build_loss_patterns(evidence_db_path or db_path)
    evidence_refs = _collect_evidence_refs(evidence_db_path or db_path)
    evidence_refs.extend(
        ref
        for ref in _collect_risk_refs(emerging_risks)
        if ref not in evidence_refs
    )
    if not evidence_refs:
        result.errors.append(
            "Scenario generation requires referenced evidence or cited risk signals"
        )
        return result

    # Get existing scenario IDs (to avoid collisions). Include qa_logs IDs:
    # rejected scenarios never reach the scenarios table, but reusing their
    # IDs would cross-contaminate the QA audit trail.
    existing = list_scenarios(db_path, status=None)
    existing_ids = [s["scenario_id"] for s in existing]
    try:
        existing_ids.extend(list_qa_scenario_ids(db_path))
    except Exception as e:
        logger.warning(f"Could not load qa_logs scenario ids: {e}")

    year = datetime.now(timezone.utc).year
    seq = 1

    for i in range(num_scenarios):
        # Peek the next free scenario id WITHOUT consuming it, so both the
        # generation call and the judge loop attribute their telemetry to
        # it. A failed generation must not burn an id — consumption happens
        # below, only after a scenario exists.
        peek_seq = seq
        scenario_id = SCENARIO_ID_TEMPLATE.format(year=year, seq=peek_seq)
        peek_seq += 1
        while scenario_id in existing_ids:
            scenario_id = SCENARIO_ID_TEMPLATE.format(year=year, seq=peek_seq)
            peek_seq += 1

        # Generate scenario
        scenario = generate_scenario(
            client,
            emerging_risks=emerging_risks,
            loss_patterns=loss_patterns,
            existing_scenario_ids=existing_ids,
            scenario_id=scenario_id,
        )
        if scenario is None:
            result.errors.append(f"Generation failed for scenario {i+1}")
            continue

        result.generated.append(scenario)

        # Consume the peeked id and reserve it — even if this scenario is
        # rejected, its ID has been used in qa_logs and must not be
        # reassigned
        seq = peek_seq
        existing_ids.append(scenario_id)

        # Convert to DB row
        row = convert_to_db_row(
            scenario,
            scenario_id,
            evidence_refs=evidence_refs,
        )

        # Run Ralph Loop for QA
        if use_ralph_loop:
            # Use the DB row's dict as the scenario for the judge
            judge_scenario_dict = {
                "title": row["title"],
                "description": row["description"],
                "risk_category": row["risk_category"],
                "financial_impact_band": row["financial_impact_band"],
                "core_risks": scenario.get("core_risks", []),
                "preparation_strategy": scenario.get("conclusion", {}).get("preparation_strategy", []),
                "detailed_rationale": scenario.get("scenario_event", {}).get("detailed_rationale", []),
                "evidence_refs": evidence_refs,
                "external_drivers": scenario.get("external_drivers", []),
                "signal_maturity": row["signal_maturity"],
                "momentum": row["momentum"],
                "evidence_strength": row["evidence_strength"],
                "affected_capabilities": scenario.get("scenario_event", {}).get(
                    "affected_capabilities", []
                ),
                "transmission_mechanism": scenario.get("scenario_event", {}).get(
                    "transmission_mechanism", ""
                ),
                "second_order_effects": scenario.get("scenario_event", {}).get(
                    "second_order_effects", []
                ),
                "cross_industry_analogues": scenario.get(
                    "scenario_event", {}
                ).get("cross_industry_analogues", []),
                "strengthening_signposts": scenario.get("signposts", {}).get(
                    "strengthening", []
                ),
                "weakening_signposts": scenario.get("signposts", {}).get(
                    "weakening", []
                ),
                "defensive_options": scenario.get("conclusion", {}).get(
                    "defensive_options", []
                ),
                "resilience_opportunities": scenario.get("conclusion", {}).get(
                    "resilience_opportunities", []
                ),
                "strategic_options": scenario.get("conclusion", {}).get(
                    "strategic_options", []
                ),
                "option_expiry_signals": scenario.get("conclusion", {}).get(
                    "option_expiry_signals", []
                ),
                "challenged_assumptions": scenario.get("conclusion", {}).get(
                    "challenged_assumptions", []
                ),
                "discussion_prompts": scenario.get("conclusion", {}).get(
                    "discussion_prompts", []
                ),
            }
            ralph = ralph_loop(
                judge_scenario_dict,
                client,
                threshold=judge_threshold,
                max_iterations=max_judge_iterations,
                final_judge_model=client.settings.llm_model_final_judge,
                scenario_id=scenario_id,
            )

            if ralph.error:
                # Judge backend failure — not a quality verdict; don't record
                # a rejection in qa_logs or counts.
                result.errors.append(f"Judge failed for {scenario_id}: {ralph.feedback}")
                logger.warning(f"Scenario {scenario_id} skipped: {ralph.feedback}")
                continue

            # Update row with judge results
            row["judge_score"] = ralph.final_score
            row["judge_feedback"] = ralph.feedback
            row["judge_iterations"] = ralph.iterations

            try:
                log_qa(
                    db_path,
                    scenario_id,
                    phase="phase3",
                    judge_model=client.settings.llm_model_judge,
                    judge_score=ralph.final_score,
                    judge_feedback=ralph.feedback,
                    iterations=ralph.iterations,
                    passed=ralph.passed,
                )
            except Exception as e:
                logger.warning(f"Could not write qa_log for {scenario_id}: {e}")

            if not ralph.passed:
                result.rejected += 1
                logger.info(f"Scenario {scenario_id} rejected (score {ralph.final_score:.2f})")
                continue

            # Persist everything the loop improved — the judge scored the
            # improved description AND preparation strategy, not the originals
            row["description"] = judge_scenario_dict.get(
                "description", row["description"]
            )
            row["preparation_strategy"] = to_json_str(
                judge_scenario_dict.get("preparation_strategy", [])
            )
            notes = judge_scenario_dict.get("additional_information")
            if notes:
                base = row.get("additional_information") or ""
                row["additional_information"] = f"{base}\n{notes}".strip() if base else notes
            row["status"] = "active"

        # Insert (gated at the db.upsert_scenario boundary — C-01)
        try:
            upsert_scenario(db_path, row)
        except sqlite3.Error as e:
            logger.error(f"Failed to insert scenario {scenario_id}: {e}")
            result.errors.append(f"DB insert failed for {scenario_id}: {e}")
            continue
        result.inserted += 1
        logger.info(f"Inserted scenario {scenario_id} (score {row.get('judge_score') or 0:.2f})")

    logger.info(
        f"Generation cycle: {result.inserted} inserted, "
        f"{result.rejected} rejected, {len(result.errors)} errors"
    )
    return result
