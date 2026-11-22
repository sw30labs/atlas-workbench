"""Phase 5: Pattern Detection & Causal Analysis.

This is the most sophisticated phase. It performs **cross-source causal
analysis** to identify non-obvious relationships between:

  - Reviewed evidence events with recorded-loss fields (event_evidence.db)
  - Emerging risks (emerging_risks.json)
  - Generated scenarios (atlas.db)

Three analysis layers:

  1. **Temporal Causality** — Does event A precede event B within a lag window
     significantly more often than chance? Uses a permutation-test confidence
     interval (no LLM needed, fully deterministic).

  2. **Thematic Correlation** — Do categories co-occur more than expected?
     χ²-style association with confidence scoring.

  3. **LLM Causal Synthesis (DeepAgents)** — A multi-agent causal inquiry:
     a *hypothesis agent* proposes mechanisms, a *critic agent* challenges them
     with counterfactuals, and a *synthesis agent* writes the final finding.

Outputs are written to `pattern_detections` and `causal_findings` tables.
"""

from __future__ import annotations

import bisect
import json
import logging
import random
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..llm import OMLXClient
from ..db import list_evidence_events, list_scenarios, to_json_str, from_json_str
from ..provenance import is_approved_record

logger = logging.getLogger(__name__)


def _load_approved_risks(
    file_path: str | None,
    *,
    limit: int = 4,
) -> list[dict[str, Any]]:
    """Load only human-approved or approved-synthetic emerging risks."""
    if not file_path:
        return []
    path = Path(file_path)
    if not path.is_file():
        return []
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not load emerging risks: %s", exc)
        return []
    if not isinstance(value, list):
        return []
    approved = [
        risk
        for risk in value
        if isinstance(risk, dict)
        and is_approved_record(risk)
        and risk.get("issue")
    ]
    return approved[: max(0, int(limit))]


# ── Data models ──

@dataclass
class CausalFinding:
    """A causal relationship finding."""
    cause_event: str
    effect_event: str
    lag_days: int
    confidence: float
    sample_count: int
    counterexamples: int
    mechanism: str
    evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "cause_event": self.cause_event,
            "effect_event": self.effect_event,
            "lag_days": self.lag_days,
            "confidence": self.confidence,
            "sample_count": self.sample_count,
            "counterexamples": self.counterexamples,
            "mechanism": self.mechanism,
            "evidence": self.evidence,
        }


@dataclass
class PatternDetection:
    """A cross-source pattern detection."""
    pattern_type: str
    description: str
    confidence: float
    evidence: list[str]
    source_ids: list[str] = field(default_factory=list)


@dataclass
class AnalysisResult:
    """Result of a full pattern detection cycle."""
    temporal_findings: list[CausalFinding] = field(default_factory=list)
    thematic_patterns: list[PatternDetection] = field(default_factory=list)
    llm_synthesis: list[PatternDetection] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


# ── Layer 1: Temporal Causality (deterministic) ──

def _parse_date(s: str | None) -> datetime | None:
    """Parse a YYYY-MM-DD date string."""
    if not s:
        return None
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(s))
    if not m:
        return None
    try:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def temporal_causality(
    events: list[dict[str, Any]],
    *,
    lag_days: int = 180,
    min_samples: int = 5,
    n_permutations: int = 1000,
    random_seed: int = 42,
) -> list[CausalFinding]:
    """Detect temporal causal relationships between event categories.

    For each ordered pair of categories (A, B), counts how often a B-event
    occurs within `lag_days` after an A-event. Compares against a permutation
    null distribution to compute a confidence score.

    Args:
        events: List of evidence events with category + date
        lag_days: Max lag window for causation
        min_samples: Minimum A-events to consider a pair
        n_permutations: Permutation test iterations
        random_seed: Seed for reproducibility

    Returns:
        List of CausalFinding with confidence > 0.6 (by default threshold)
    """
    from datetime import timedelta

    rng = random.Random(random_seed)

    # Build timeline: (date, category, headline)
    timeline = []
    for e in events:
        d = _parse_date(e.get("source_date"))
        if d is None:
            continue
        cat = e.get("risk_category") or "Unknown"
        timeline.append((d, cat, e.get("headline", "")))
    timeline.sort(key=lambda x: x[0])

    if len(timeline) < 10:
        return []

    categories = sorted({c for _, c, _ in timeline})
    findings = []

    for cause_cat in categories:
        cause_events = [(d, h) for d, c, h in timeline if c == cause_cat]
        if len(cause_events) < min_samples:
            continue
        cause_dates = [d for d, _ in cause_events]

        for effect_cat in categories:
            if cause_cat == effect_cat:
                continue
            effect_events = [d for d, c, _ in timeline if c == effect_cat]
            if len(effect_events) < min_samples:
                continue
            effect_dates = sorted(effect_events)

            # Observed count: effect within lag after cause
            observed = 0
            for cd in cause_dates:
                lo = bisect.bisect_left(effect_dates, cd)
                upper = cd + timedelta(days=lag_days)
                hi = bisect.bisect_right(effect_dates, upper)
                observed += hi - lo

            # Permutation null: pool all dates, shuffle, then re-split into
            # cause-sized and effect-sized groups. This preserves the original
            # marginal date distributions while breaking category-date linkage.
            combined_dates = cause_dates + effect_dates
            null_counts = []
            for _ in range(n_permutations):
                perm = combined_dates[:]
                rng.shuffle(perm)
                perm_causes = sorted(perm[:len(cause_dates)])
                perm_effects = sorted(perm[len(cause_dates):])
                cnt = 0
                for cd in perm_causes:
                    lo = bisect.bisect_left(perm_effects, cd)
                    upper = cd + timedelta(days=lag_days)
                    hi = bisect.bisect_right(perm_effects, upper)
                    cnt += hi - lo
                null_counts.append(cnt)

            null_mean = sum(null_counts) / len(null_counts)
            null_std = (
                sum((x - null_mean) ** 2 for x in null_counts) / len(null_counts)
            ) ** 0.5

            if null_std == 0:
                logger.debug(
                    "Skipping %s -> %s: null distribution has zero variance "
                    "(too few permutations or sparse data)",
                    cause_cat, effect_cat,
                )
                continue

            z_score = (observed - null_mean) / null_std
            # Confidence: map z-score to 0-1 (cumulative normal approx)
            confidence = min(max(z_score / 3.0, 0.0), 1.0)
            counterexamples = sum(1 for x in null_counts if x >= observed)

            if confidence >= 0.6 and observed > null_mean:
                findings.append(CausalFinding(
                    cause_event=cause_cat,
                    effect_event=effect_cat,
                    lag_days=lag_days,
                    confidence=round(confidence, 3),
                    sample_count=len(cause_events),
                    counterexamples=counterexamples,
                    mechanism="Temporal precedence within lag window (permutation-tested)",
                    evidence=[
                        f"{observed} effect events within {lag_days}d of {len(cause_events)} cause events",
                        f"Null mean: {null_mean:.1f}, z-score: {z_score:.2f}",
                    ],
                ))

    findings.sort(key=lambda f: f.confidence, reverse=True)
    return findings


# ── Layer 2: Thematic Correlation (deterministic) ──

def _scenario_theme_labels(scenario: dict[str, Any]) -> set[str]:
    """Extract multi-label theme set from category + theme_tags + core_risks."""
    labels: set[str] = set()
    cat = scenario.get("risk_category")
    if cat:
        labels.add(str(cat))

    for field_name in ("theme_tags", "core_risks"):
        raw = scenario.get(field_name)
        if not raw:
            continue
        if isinstance(raw, str):
            raw = from_json_str(raw) or [raw]
        if isinstance(raw, list):
            for item in raw:
                if item:
                    labels.add(str(item))
    return labels


def thematic_correlation(
    scenarios: list[dict[str, Any]],
    *,
    min_cooccurrence: int = 3,
    confidence_threshold: float = 0.6,
) -> list[PatternDetection]:
    """Detect thematic correlations via multi-label co-occurrence.

    Each scenario contributes labels from risk_category, theme_tags, and
    core_risks. Pairwise lift is computed only when *both* labels appear on
    the same scenario (true co-occurrence, not category OR-counts).
    """
    from collections import Counter

    label_sets = [_scenario_theme_labels(s) for s in scenarios]
    label_counts: Counter[str] = Counter()
    for labels in label_sets:
        for lab in labels:
            label_counts[lab] += 1

    labels = sorted(label_counts.keys())
    n = len(scenarios) or 1
    patterns = []

    for i, lab_a in enumerate(labels):
        for lab_b in labels[i + 1:]:
            # True co-occurrence: same scenario carries both labels
            cooccur_ids = []
            for s, labs in zip(scenarios, label_sets):
                if lab_a in labs and lab_b in labs:
                    cooccur_ids.append(s.get("scenario_id", ""))
            cooccur = len(cooccur_ids)
            if cooccur < min_cooccurrence:
                continue

            p_a = label_counts[lab_a] / n
            p_b = label_counts[lab_b] / n
            p_ab = cooccur / n
            lift = p_ab / (p_a * p_b) if p_a * p_b > 0 else 0

            # lift > 1 indicates positive association; map to [0, 1]
            confidence = min(max((lift - 1.0) / 2.0, 0.0), 1.0)
            if confidence >= confidence_threshold:
                patterns.append(PatternDetection(
                    pattern_type="thematic_correlation",
                    description=(
                        f"Labels '{lab_a}' and '{lab_b}' co-occur on {cooccur} "
                        f"scenarios (lift={lift:.2f})"
                    ),
                    confidence=round(confidence, 3),
                    evidence=[
                        f"P({lab_a})={p_a:.2f}, P({lab_b})={p_b:.2f}, P(both)={p_ab:.2f}",
                        f"Lift={lift:.2f} (>1 means positively associated)",
                    ],
                    source_ids=cooccur_ids[:10],
                ))

    patterns.sort(key=lambda p: p.confidence, reverse=True)
    return patterns


# ── Layer 3: LLM Causal Synthesis (DeepAgents) ──

PROMPT_VERSION = "phase5-causal-synthesis-v1"

HYPOTHESIS_PROMPT = """You are a Hypothesis Agent in a causal analysis system for bank operational risk.

Given these observed patterns across historical losses, emerging risks, and
scenario hypotheses, propose 1-2 causal mechanisms explaining *why* they might
be linked. Be specific about the transmission channel.

Observed patterns:
{patterns_json}

For each hypothesis, return JSON:
{{
  "hypotheses": [
    {{
      "cause": "category or risk",
      "effect": "category or risk",
      "mechanism": "specific transmission channel",
      "plausibility": 0.0-1.0,
      "evidence_needed": "what would confirm this"
    }}
  ]
}}"""

CRITIC_PROMPT = """You are a Critic Agent. Challenge these causal hypotheses with
counterfactuals and alternative explanations. For each, state whether it
survives scrutiny.

Hypotheses:
{hypotheses_json}

Return JSON:
{{
  "critiques": [
    {{
      "hypothesis_index": 0,
      "verdict": "survives" | "weak" | "rejected",
      "challenge": "counterfactual or alternative explanation",
      "confidence": 0.0-1.0
    }}
  ]
}}"""

SYNTHESIS_PROMPT = """You are a Synthesis Agent. Combine the surviving hypotheses
and critiques into a final causal finding.

Hypotheses: {hypotheses_json}
Critiques: {critiques_json}

Return JSON:
{{
  "findings": [
    {{
      "pattern_type": "causal_mechanism",
      "description": "final causal narrative",
      "confidence": 0.0-1.0,
      "evidence": ["point 1", "point 2"]
    }}
  ]
}}"""


def _as_object(payload: Any, key: str) -> list[Any]:
    """Accept {"key": [...]}, a bare list, or any other shape.

    A model that answers with a top-level array is valid JSON. This stage
    is advisory, so a wrong shape becomes an empty list instead of an
    AttributeError that aborts the run.
    """
    if isinstance(payload, dict):
        value = payload.get(key)
        return value if isinstance(value, list) else []
    if isinstance(payload, list):
        return payload
    return []


class CausalDeepAgents:
    """Multi-agent causal synthesis using LLM (deepagents pattern).

    Three roles: Hypothesis → Critic → Synthesis. The critic challenges
    the hypothesis agent's proposals, making the final finding more robust.
    """

    def __init__(self, client: OMLXClient, *, temperature: float = 0.3) -> None:
        self.client = client
        self.temperature = temperature

    def synthesize(
        self,
        patterns: list[dict[str, Any]],
    ) -> list[PatternDetection]:
        """Run the 3-agent causal synthesis. Returns final findings."""
        if not patterns:
            return []

        patterns_json = json.dumps(patterns[:10], indent=2)

        # 1. Hypothesis agent
        try:
            hypotheses = self.client.chat_json(
                [{"role": "user", "content": HYPOTHESIS_PROMPT.format(patterns_json=patterns_json)}],
                temperature=self.temperature,
                max_tokens=2048,
            )
        except Exception as exc:
            # Breadth is intended. This stage is advisory: a transport
            # failure or a bug in the client must degrade to no findings
            # rather than abort the deterministic phases around it.
            logger.error(f"Hypothesis agent failed: {exc}")
            return []

        hyps = _as_object(hypotheses, "hypotheses")
        if not hyps:
            return []

        # 2. Critic agent
        try:
            critiques = self.client.chat_json(
                [{"role": "user", "content": CRITIC_PROMPT.format(
                    hypotheses_json=json.dumps(hyps, indent=2))}],
                temperature=self.temperature,
                max_tokens=2048,
            )
        except Exception as exc:
            logger.error(f"Critic agent failed: {exc}")
            critiques = None

        critique_items = _as_object(critiques, "critiques")

        # 3. Synthesis agent
        try:
            synthesis = self.client.chat_json(
                [{"role": "user", "content": SYNTHESIS_PROMPT.format(
                    hypotheses_json=json.dumps(hyps, indent=2),
                    critiques_json=json.dumps(critique_items, indent=2))}],
                temperature=self.temperature,
                max_tokens=2048,
            )
        except Exception as exc:
            logger.error(f"Synthesis agent failed: {exc}")
            return []

        findings = []
        for item in _as_object(synthesis, "findings"):
            if not isinstance(item, dict):
                continue
            evidence = item.get("evidence", [])
            if not isinstance(evidence, list):
                evidence = []
            findings.append(PatternDetection(
                pattern_type=item.get("pattern_type", "causal_mechanism"),
                description=item.get("description", ""),
                confidence=item.get("confidence", 0.5),
                evidence=evidence,
            ))

        return findings


# ── Main analysis cycle ──

def run_pattern_analysis(
    scenario_db_path: str,
    client: OMLXClient | None = None,
    *,
    evidence_db_path: str | None = None,
    emerging_risks_path: str | None = None,
    use_llm_synthesis: bool = True,
    confidence_threshold: float = 0.6,
    lag_days: int | None = None,
    min_samples: int | None = None,
    n_permutations: int | None = None,
) -> AnalysisResult:
    """Run the full pattern detection and causal analysis cycle.

    Layer 1: Temporal causality (deterministic, permutation-tested)
    Layer 2: Thematic correlation (deterministic, lift-based)
    Layer 3: LLM causal synthesis (deepagents: hypothesis → critic → synthesis)

    Args:
        scenario_db_path: Path to atlas.db (scenarios + findings tables)
        evidence_db_path: Path to event_evidence.db (defaults to scenario_db_path for tests)
        lag_days: Max lag window. Defaults to Settings.temporal_lag_days.
        min_samples: Min events per category. Defaults to Settings.temporal_min_samples.
        n_permutations: Permutation iterations. Defaults to Settings.temporal_n_permutations.

    Returns an AnalysisResult with all findings.
    """
    from ..config import get_settings

    settings = get_settings()
    lag_days = lag_days if lag_days is not None else settings.temporal_lag_days
    min_samples = min_samples if min_samples is not None else settings.temporal_min_samples
    n_permutations = (
        n_permutations if n_permutations is not None else settings.temporal_n_permutations
    )

    result = AnalysisResult()
    evidence_path = evidence_db_path or scenario_db_path

    # Load data
    try:
        evidence_events = list_evidence_events(
            evidence_path,
            limit=500,
            approved_only=True,
        )
    except Exception as e:
        logger.warning(f"Could not load evidence events: {e}", exc_info=True)
        evidence_events = []
    scenarios = list_scenarios(scenario_db_path, status="active")
    approved_risks = _load_approved_risks(emerging_risks_path)

    # Layer 1: Temporal causality
    try:
        result.temporal_findings = temporal_causality(
            evidence_events,
            lag_days=lag_days,
            min_samples=min_samples,
            n_permutations=n_permutations,
        )
    except Exception as e:
        logger.error(f"Temporal causality failed: {e}", exc_info=True)
        result.errors.append(str(e))

    # Layer 2: Thematic correlation
    try:
        result.thematic_patterns = thematic_correlation(
            scenarios,
            confidence_threshold=confidence_threshold,
        )
    except Exception as e:
        logger.error(f"Thematic correlation failed: {e}", exc_info=True)
        result.errors.append(str(e))

    # Layer 3: LLM synthesis
    if use_llm_synthesis and client is not None:
        try:
            # Combine patterns for LLM input
            pattern_inputs = []
            for risk in approved_risks[:4]:
                pattern_inputs.append({
                    "type": "emerging_risk",
                    "issue": risk.get("issue"),
                    "description": str(risk.get("description") or "")[:500],
                    "evidence_refs": list(risk.get("evidence_refs") or [])[:10],
                })
            for f in result.temporal_findings[:3]:
                pattern_inputs.append({
                    "type": "temporal",
                    "cause": f.cause_event,
                    "effect": f.effect_event,
                    "confidence": f.confidence,
                    "lag_days": f.lag_days,
                })
            for p in result.thematic_patterns[:3]:
                pattern_inputs.append({
                    "type": "thematic",
                    "description": p.description,
                    "confidence": p.confidence,
                })

            agents = CausalDeepAgents(client)
            result.llm_synthesis = agents.synthesize(pattern_inputs)
        except Exception as e:
            logger.error(f"LLM synthesis failed: {e}", exc_info=True)
            result.errors.append(str(e))

    total = (
        len(result.temporal_findings)
        + len(result.thematic_patterns)
        + len(result.llm_synthesis)
    )
    logger.info(
        f"Pattern analysis: {len(result.temporal_findings)} temporal, "
        f"{len(result.thematic_patterns)} thematic, "
        f"{len(result.llm_synthesis)} LLM synthesis ({total} total)"
    )
    return result


def persist_findings(scenario_db_path: str, result: AnalysisResult) -> int:
    """Persist analysis results to atlas.db. Returns rows written.

    Idempotent: re-running on unchanged data updates existing rows instead
    of accumulating duplicates.
    """
    from ..db import connect

    written = 0
    with connect(scenario_db_path) as conn:
        cursor = conn.cursor()

        # Pattern detections (thematic + llm), keyed by (type, description)
        for p in list(result.thematic_patterns) + list(result.llm_synthesis):
            cursor.execute(
                "SELECT id FROM pattern_detections "
                "WHERE pattern_type = ? AND description = ?",
                (p.pattern_type, p.description),
            )
            row = cursor.fetchone()
            if row:
                cursor.execute(
                    """UPDATE pattern_detections
                    SET confidence = ?, evidence = ?, source_ids = ?,
                        detected_at = datetime('now'), status = 'active'
                    WHERE id = ?""",
                    (
                        p.confidence,
                        to_json_str(p.evidence),
                        to_json_str(p.source_ids),
                        row[0],
                    ),
                )
            else:
                cursor.execute(
                    """INSERT INTO pattern_detections
                    (pattern_type, description, confidence, evidence, source_ids, status)
                    VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        p.pattern_type,
                        p.description,
                        p.confidence,
                        to_json_str(p.evidence),
                        to_json_str(p.source_ids),
                        "active",
                    ),
                )
                written += 1

        # Causal findings (temporal), keyed by (cause, effect)
        for f in result.temporal_findings:
            cursor.execute(
                "SELECT id FROM causal_findings "
                "WHERE cause_event = ? AND effect_event = ?",
                (f.cause_event, f.effect_event),
            )
            row = cursor.fetchone()
            if row:
                cursor.execute(
                    """UPDATE causal_findings
                    SET lag_days = ?, confidence = ?, sample_count = ?,
                        counterexamples = ?, mechanism = ?, detected_at = ?
                    WHERE id = ?""",
                    (
                        f.lag_days,
                        f.confidence,
                        f.sample_count,
                        f.counterexamples,
                        f.mechanism,
                        datetime.now(timezone.utc).isoformat(),
                        row[0],
                    ),
                )
            else:
                cursor.execute(
                    """INSERT INTO causal_findings
                    (cause_event, effect_event, lag_days, confidence,
                     sample_count, counterexamples, mechanism, detected_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        f.cause_event,
                        f.effect_event,
                        f.lag_days,
                        f.confidence,
                        f.sample_count,
                        f.counterexamples,
                        f.mechanism,
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
                written += 1

        conn.commit()
    return written
