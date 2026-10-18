"""LLM-as-a-Judge for Contingency Atlas scenario quality evaluation.

The judge evaluates scenarios on:
  1. Clarity — is the description clear and specific?
  2. Foresight value — does it connect traceable drivers while preserving uncertainty?
  3. Evidence grounding — does it cite traceable public-source patterns?
  4. Actionability — does the preparation strategy make sense?
  5. No hallucination — are claims verifiable?

Returns a score 0-1 and structured feedback. The Ralph Loop (loops.py)
uses this to iterate on scenarios until the score meets threshold.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from .llm import OMLXClient, LLMError

logger = logging.getLogger(__name__)
PROMPT_VERSION = "scenario-judge-v1"


@dataclass
class JudgeResult:
    """Result of an LLM-as-a-Judge evaluation."""

    score: float  # 0.0 to 1.0
    feedback: str
    criteria: dict[str, float] = field(default_factory=dict)
    passed: bool = False
    iteration: int = 0
    # True when the judge itself failed (backend outage, unparseable output).
    # Callers must not treat an errored result as a genuine quality rejection.
    error: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "feedback": self.feedback,
            "criteria": self.criteria,
            "passed": self.passed,
            "iteration": self.iteration,
            "error": self.error,
        }


JUDGE_SYSTEM_PROMPT = """You are a risk scenario judge for an independent banking foresight engine.

For each scenario, score these criteria from 0.0 to 1.0:
- clarity: Is the description clear, specific, and jargon-free?
- foresight_value: Does it connect traceable drivers into a consequential,
  non-obvious convergence through a coherent transmission mechanism while
  keeping uncertainty explicit? Are strengthening and weakening signposts
  observable and genuinely capable of changing the assessment?
- evidence_grounding: Does it cite traceable public reporting, security advisories, regulatory material, or supplied evidence?
- actionability: Do the defensive, resilience, and strategic options make
  concrete sense, preserve room to act, and identify when options may expire?
- no_hallucination: Are specific claims (CVEs, dates, institutions) verifiable?

Return JSON: {"score": float, "criteria": {"clarity": float, ...}, "feedback": "brief", "passed": true/false}
"passed" is true if overall score >= 0.75. Be rigorous — do not rubber-stamp."""


def build_judge_prompt(scenario: dict[str, Any]) -> list[dict[str, str]]:
    """Build the chat messages for judging a scenario."""
    # Extract key fields
    title = scenario.get("title", "")
    desc = scenario.get("description", "")
    category = scenario.get("risk_category", "")
    impact = scenario.get("financial_impact_band", "")
    prep = scenario.get("preparation_strategy", "")
    rationale = scenario.get("detailed_rationale", "")
    core_risks = scenario.get("core_risks", "")
    evidence_refs = scenario.get("evidence_refs", "")
    external_drivers = scenario.get("external_drivers", "")
    maturity = scenario.get("signal_maturity", "")
    momentum = scenario.get("momentum", "")
    evidence_strength = scenario.get("evidence_strength", "")
    capabilities = scenario.get("affected_capabilities", "")
    transmission = scenario.get("transmission_mechanism", "")
    second_order = scenario.get("second_order_effects", "")
    analogues = scenario.get("cross_industry_analogues", "")
    strengthening = scenario.get("strengthening_signposts", "")
    weakening = scenario.get("weakening_signposts", "")
    defensive = scenario.get("defensive_options", "")
    resilience = scenario.get("resilience_opportunities", "")
    strategic = scenario.get("strategic_options", "")
    option_expiry = scenario.get("option_expiry_signals", "")
    assumptions = scenario.get("challenged_assumptions", "")
    prompts = scenario.get("discussion_prompts", "")

    user_content = f"""Evaluate this Contingency Atlas scenario:

Title: {title}
Category: {category}
Description: {desc}
Financial Impact: {impact}
Core Risks: {core_risks}
External Drivers: {external_drivers}
Signal Maturity: {maturity}
Momentum: {momentum}
Evidence Strength: {evidence_strength}
Affected Capabilities: {capabilities}
Transmission Mechanism: {transmission}
Second-order Effects: {second_order}
Cross-industry Analogues: {analogues}
Strengthening Signposts: {strengthening}
Weakening Signposts: {weakening}
Preparation Strategy: {prep}
Defensive Options: {defensive}
Resilience Opportunities: {resilience}
Strategic Options: {strategic}
Option-expiry Signals: {option_expiry}
Challenged Assumptions: {assumptions}
Discussion Prompts: {prompts}
Rationale: {rationale}
Evidence References: {evidence_refs}

Score each criterion 0-1 and return JSON with "score", "criteria", "feedback", "passed"."""

    return [
        {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]


def judge_scenario(
    scenario: dict[str, Any],
    client: OMLXClient,
    *,
    threshold: float = 0.75,
    model: str | None = None,
    scenario_id: str | None = None,
) -> JudgeResult:
    """Evaluate a single scenario with the LLM-as-a-Judge.

    Returns a JudgeResult with score, criteria, and feedback.
    ``scenario_id`` tags telemetry so the judge tokens attribute to the
    scenario being judged.
    """
    messages = build_judge_prompt(scenario)
    try:
        result = client.chat_json(
            messages,
            model=model or client.settings.llm_model_judge,
            temperature=0.0,
            scenario_id=scenario_id,
        )
    except (LLMError, json.JSONDecodeError) as e:
        logger.warning(f"Judge LLM call failed: {e}")
        return JudgeResult(
            score=0.0,
            feedback=f"Judge error: {e}",
            passed=False,
            error=True,
        )

    if not isinstance(result, dict):
        return JudgeResult(
            score=0.0,
            feedback=f"Judge returned non-object JSON: {type(result).__name__}",
            passed=False,
            error=True,
        )

    try:
        score = float(result.get("score", 0.0))
    except (TypeError, ValueError):
        return JudgeResult(
            score=0.0,
            feedback=f"Judge returned non-numeric score: {result.get('score')!r}",
            passed=False,
            error=True,
        )
    score = max(0.0, min(1.0, score))
    criteria = result.get("criteria", {})
    if not isinstance(criteria, dict):
        criteria = {}
    feedback = str(result.get("feedback", "") or "")
    passed = score >= threshold

    return JudgeResult(
        score=score,
        feedback=feedback,
        criteria=criteria,
        passed=passed,
    )


def batch_judge(
    scenarios: list[dict[str, Any]],
    client: OMLXClient,
    *,
    threshold: float = 0.75,
) -> list[JudgeResult]:
    """Judge multiple scenarios. Returns results in order."""
    results = []
    for s in scenarios:
        r = judge_scenario(s, client, threshold=threshold)
        results.append(r)
    return results
