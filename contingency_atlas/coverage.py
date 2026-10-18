"""Deterministic scenario-coverage recommendations over the approved corpus.

This module deliberately does not call an LLM and does not mutate a store.  It
answers a narrow post-review question: given the evidence, risks, and scenarios
that are already governed, how many *new* scenarios would close the visible
coverage gaps without encouraging duplicate generation?

Pending or ``needs_update`` records never enter the calculation.  Duplicate
similarity is advisory only: candidate duplicates narrow the exploratory upper
bound, but are never silently merged or removed from the recommendation basis.
That preserves the human identity gate used by :mod:`contingency_atlas.dedup`.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from .provenance import url_dedup_variants


APPROVED_STATUSES = frozenset({"approved", "approved_synthetic"})
ACTIVE_SCENARIO_STATUS = "active"
MAX_RECOMMENDED_SCENARIOS = 10

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_GENERIC_TOKENS = frozenset(
    {
        "a",
        "an",
        "and",
        "for",
        "in",
        "of",
        "on",
        "or",
        "risk",
        "risks",
        "the",
        "threat",
        "threats",
        "to",
    }
)


@dataclass(frozen=True)
class ScenarioCoverageRecommendation:
    """JSON-friendly result returned by :func:`recommend_scenario_coverage`."""

    recommended: int
    range_min: int
    range_max: int
    cap: int
    approved_evidence_count: int
    approved_risk_count: int
    eligible_risk_count: int
    active_scenario_count: int
    covered_risk_ids: tuple[str, ...]
    uncovered_risk_ids: tuple[str, ...]
    uncovered_categories: tuple[str, ...]
    excluded_unresolved_risk_ids: tuple[str, ...]
    duplicate_risk_candidates: tuple[tuple[str, str], ...]
    duplicate_scenario_candidates: tuple[tuple[str, str], ...]
    ignored_unapproved_evidence_count: int
    ignored_unapproved_risk_count: int
    rationale: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        """Return a stable JSON-serializable representation for the web API."""

        return {
            "recommended": self.recommended,
            "range": {"min": self.range_min, "max": self.range_max},
            "cap": self.cap,
            "basis": {
                "approved_evidence": self.approved_evidence_count,
                "approved_risks": self.approved_risk_count,
                "eligible_risks": self.eligible_risk_count,
                "active_scenarios": self.active_scenario_count,
                "covered_risks": list(self.covered_risk_ids),
                "uncovered_risks": list(self.uncovered_risk_ids),
                "uncovered_categories": list(self.uncovered_categories),
                "excluded_unresolved_risks": list(
                    self.excluded_unresolved_risk_ids
                ),
                "ignored_unapproved_evidence": (
                    self.ignored_unapproved_evidence_count
                ),
                "ignored_unapproved_risks": self.ignored_unapproved_risk_count,
            },
            "duplicate_candidates": {
                "risks": [list(pair) for pair in self.duplicate_risk_candidates],
                "scenarios": [
                    list(pair) for pair in self.duplicate_scenario_candidates
                ],
            },
            "rationale": list(self.rationale),
        }


def _text(value: object) -> str:
    return str(value).strip() if value is not None else ""


def _normalized(value: object) -> str:
    return " ".join(_tokens(value))


def _tokens(value: object) -> tuple[str, ...]:
    return tuple(
        token
        for token in _TOKEN_RE.findall(_text(value).casefold())
        if token not in _GENERIC_TOKENS
    )


def _token_set(value: object) -> set[str]:
    return set(_tokens(value))


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _field_list(value: object, *, split_commas: bool = False) -> list[str]:
    """Normalize list-like JSON/database fields without splitting URLs."""

    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return []
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            decoded = None
        if isinstance(decoded, list):
            value = decoded
        elif split_commas and "," in raw:
            value = raw.split(",")
        else:
            value = [raw]
    if not isinstance(value, (list, tuple, set, frozenset)):
        return []
    return [_text(item) for item in value if _text(item)]


def _record_id(record: Mapping[str, Any], kind: str, index: int) -> str:
    candidates = {
        "risk": ("risk_id", "id", "issue", "title"),
        "scenario": ("scenario_id", "id", "title"),
        "evidence": ("event_id", "id", "source_url", "content_fingerprint"),
    }[kind]
    for key in candidates:
        if value := _text(record.get(key)):
            return value
    return f"{kind}-{index + 1}"


def _unique_by_persisted_id(
    records: list[Mapping[str, Any]], kind: str
) -> list[tuple[str, Mapping[str, Any]]]:
    """Collapse repeated API rows, never semantic records with distinct IDs."""

    unique: list[tuple[str, Mapping[str, Any]]] = []
    seen: set[str] = set()
    for index, record in enumerate(records):
        record_id = _record_id(record, kind, index)
        persisted_keys = {
            "risk": ("risk_id", "id"),
            "scenario": ("scenario_id", "id"),
            "evidence": ("event_id", "id"),
        }[kind]
        persisted = next(
            (_text(record.get(key)) for key in persisted_keys if _text(record.get(key))),
            "",
        )
        key = f"persisted:{persisted}" if persisted else f"row:{index}"
        if key in seen:
            continue
        seen.add(key)
        unique.append((record_id, record))
    return unique


def _approved_evidence_refs(
    records: list[Mapping[str, Any]],
) -> tuple[set[str], int]:
    refs: set[str] = set()
    unique = _unique_by_persisted_id(records, "evidence")
    for _record_id_value, record in unique:
        event_id = _text(record.get("event_id"))
        if event_id:
            refs.add(event_id)
        source_url = _text(record.get("source_url"))
        if source_url:
            refs.update(url_dedup_variants(source_url))
    return refs, len(unique)


def _risk_refs_resolve(record: Mapping[str, Any], approved_refs: set[str]) -> bool:
    refs = _field_list(record.get("evidence_refs"))
    if not refs:
        return False

    def resolves(ref: str) -> bool:
        if ref in approved_refs:
            return True
        if ref.lower().startswith(("http://", "https://")) and any(
            variant in approved_refs for variant in url_dedup_variants(ref)
        ):
            return True
        return False

    # Approval requires every reference to resolve.  Treating one good ref as
    # sufficient would let a subsequently attached pending ref influence the
    # recommendation through an otherwise-approved risk.
    return all(resolves(ref) for ref in refs)


def _risk_aliases(record_id: str, record: Mapping[str, Any]) -> tuple[str, ...]:
    aliases = {
        _normalized(record_id),
        _normalized(record.get("risk_id")),
        _normalized(record.get("issue")),
        _normalized(record.get("title")),
    }
    return tuple(sorted(alias for alias in aliases if alias))


def _labels_match(candidate: str, aliases: tuple[str, ...]) -> bool:
    candidate_tokens = _token_set(candidate)
    if not candidate_tokens:
        return False
    candidate_normalized = " ".join(sorted(candidate_tokens))
    for alias in aliases:
        alias_tokens = set(alias.split())
        if not alias_tokens:
            continue
        if candidate_normalized == " ".join(sorted(alias_tokens)):
            return True
        shorter, longer = sorted((candidate_tokens, alias_tokens), key=len)
        if len(shorter) >= 2 and shorter <= longer:
            return True
        if _jaccard(candidate_tokens, alias_tokens) >= 0.75:
            return True
    return False


def _scenario_covers_risk(
    scenario_id: str,
    scenario: Mapping[str, Any],
    risk_id: str,
    risk: Mapping[str, Any],
) -> bool:
    related = set(_field_list(risk.get("related_scenarios")))
    if scenario_id in related:
        return True
    aliases = _risk_aliases(risk_id, risk)
    return any(
        _labels_match(label, aliases)
        for label in _field_list(scenario.get("core_risks"), split_commas=True)
    )


def _risk_duplicate_pairs(
    risks: list[tuple[str, Mapping[str, Any]]],
) -> tuple[tuple[str, str], ...]:
    """Return conservative semantic candidates; never collapse them."""

    pairs: list[tuple[str, str]] = []
    ordered = sorted(risks, key=lambda item: item[0])
    for index, (left_id, left) in enumerate(ordered):
        left_title = _text(left.get("issue") or left.get("title"))
        left_title_tokens = _token_set(left_title)
        left_text_tokens = _token_set(
            f"{left_title} {_text(left.get('description'))}"
        )
        for right_id, right in ordered[index + 1 :]:
            right_title = _text(right.get("issue") or right.get("title"))
            right_title_tokens = _token_set(right_title)
            right_text_tokens = _token_set(
                f"{right_title} {_text(right.get('description'))}"
            )
            title_similarity = _jaccard(left_title_tokens, right_title_tokens)
            text_similarity = _jaccard(left_text_tokens, right_text_tokens)
            same_title = bool(
                _normalized(left_title)
                and _normalized(left_title) == _normalized(right_title)
            )
            if same_title or (0.6 * title_similarity + 0.4 * text_similarity) >= 0.6:
                pairs.append((left_id, right_id))
    return tuple(pairs)


def _scenario_duplicate_pairs(
    scenarios: list[tuple[str, Mapping[str, Any]]],
) -> tuple[tuple[str, str], ...]:
    """Flag near-identical active scenarios without treating them as merged."""

    pairs: list[tuple[str, str]] = []
    ordered = sorted(scenarios, key=lambda item: item[0])
    for index, (left_id, left) in enumerate(ordered):
        left_title = _token_set(left.get("title"))
        left_core = {
            _normalized(value)
            for value in _field_list(left.get("core_risks"), split_commas=True)
            if _normalized(value)
        }
        for right_id, right in ordered[index + 1 :]:
            right_title = _token_set(right.get("title"))
            right_core = {
                _normalized(value)
                for value in _field_list(right.get("core_risks"), split_commas=True)
                if _normalized(value)
            }
            same_title = bool(left_title and left_title == right_title)
            same_core = bool(left_core and left_core == right_core)
            near_title = _jaccard(left_title, right_title) >= 0.8
            if same_title or (same_core and near_title):
                pairs.append((left_id, right_id))
    return tuple(pairs)


def _clamp_cap(value: object) -> int:
    try:
        cap = int(value)
    except (TypeError, ValueError):
        cap = MAX_RECOMMENDED_SCENARIOS
    return max(0, min(cap, MAX_RECOMMENDED_SCENARIOS))


def recommend_scenario_coverage(
    evidence_events: Iterable[Mapping[str, Any]],
    risks: Iterable[Mapping[str, Any]],
    scenarios: Iterable[Mapping[str, Any]],
    *,
    max_scenarios: int = MAX_RECOMMENDED_SCENARIOS,
) -> ScenarioCoverageRecommendation:
    """Recommend ``0..10`` new scenarios from approved, evidence-backed gaps.

    The lower bound assumes one scenario can responsibly cover three uncovered
    risk atoms, the recommendation assumes two, and the upper bound assumes
    one.  Duplicate candidates reduce only the exploratory upper bound because
    similarity is not identity and this function has no authority to merge.

    ``evidence_events``, ``risks``, and ``scenarios`` may be database rows or
    JSON records.  The function performs no I/O, making it directly callable
    from the dashboard server after it has loaded the current corpus.
    """

    evidence_rows = [record for record in evidence_events if isinstance(record, Mapping)]
    risk_rows = [record for record in risks if isinstance(record, Mapping)]
    scenario_rows = [record for record in scenarios if isinstance(record, Mapping)]
    cap = _clamp_cap(max_scenarios)

    approved_evidence_rows = [
        record
        for record in evidence_rows
        if _text(record.get("review_status")) in APPROVED_STATUSES
    ]
    approved_refs, approved_evidence_count = _approved_evidence_refs(
        approved_evidence_rows
    )

    approved_risk_rows = [
        record
        for record in risk_rows
        if _text(record.get("review_status")) in APPROVED_STATUSES
        and _text(record.get("status") or "active") not in {"faded", "retired"}
        and not _text(record.get("merged_into"))
    ]
    approved_risks = _unique_by_persisted_id(approved_risk_rows, "risk")
    eligible_risks: list[tuple[str, Mapping[str, Any]]] = []
    unresolved_risk_ids: list[str] = []
    for risk_id, record in approved_risks:
        if _risk_refs_resolve(record, approved_refs):
            eligible_risks.append((risk_id, record))
        else:
            unresolved_risk_ids.append(risk_id)

    active_scenario_rows = [
        record
        for record in scenario_rows
        if _text(record.get("status") or ACTIVE_SCENARIO_STATUS)
        == ACTIVE_SCENARIO_STATUS
    ]
    active_scenarios = _unique_by_persisted_id(active_scenario_rows, "scenario")

    covered: list[str] = []
    uncovered: list[tuple[str, Mapping[str, Any]]] = []
    for risk_id, risk in eligible_risks:
        if any(
            _scenario_covers_risk(scenario_id, scenario, risk_id, risk)
            for scenario_id, scenario in active_scenarios
        ):
            covered.append(risk_id)
        else:
            uncovered.append((risk_id, risk))

    scenario_categories = {
        _normalized(scenario.get("risk_category"))
        for _scenario_id, scenario in active_scenarios
        if _normalized(scenario.get("risk_category"))
    }
    uncovered_categories = sorted(
        {
            category
            for _risk_id, risk in uncovered
            if (
                category := _text(
                    risk.get("atlas_category") or risk.get("risk_category")
                )
            )
            and _normalized(category) not in scenario_categories
        }
    )

    risk_duplicates = _risk_duplicate_pairs(eligible_risks)
    scenario_duplicates = _scenario_duplicate_pairs(active_scenarios)
    uncovered_count = len(uncovered)
    if uncovered_count and cap:
        range_min = min(cap, math.ceil(uncovered_count / 3))
        recommended = min(cap, math.ceil(uncovered_count / 2))
        range_max = min(cap, uncovered_count)
        uncovered_ids = {risk_id for risk_id, _risk in uncovered}
        uncovered_risk_duplicate_count = sum(
            left in uncovered_ids and right in uncovered_ids
            for left, right in risk_duplicates
        )
        duplicate_pressure = (
            uncovered_risk_duplicate_count + len(scenario_duplicates)
        )
        range_max = max(
            recommended,
            range_max - min(duplicate_pressure, range_max - recommended),
        )
    else:
        range_min = recommended = range_max = 0

    ignored_evidence = len(evidence_rows) - len(approved_evidence_rows)
    ignored_risks = len(risk_rows) - len(approved_risk_rows)
    rationale: list[str] = [
        (
            "Used only the approved corpus: "
            f"{approved_evidence_count} evidence record(s), "
            f"{len(approved_risks)} active approved risk(s), and "
            f"{len(active_scenarios)} active scenario(s)."
        )
    ]
    if ignored_evidence or ignored_risks:
        rationale.append(
            f"Ignored {ignored_evidence} unapproved evidence record(s) and "
            f"{ignored_risks} pending, changed, rejected, or inactive risk record(s)."
        )
    if unresolved_risk_ids:
        rationale.append(
            f"Excluded {len(unresolved_risk_ids)} approved risk record(s) whose "
            "references did not resolve to approved evidence."
        )
    if not eligible_risks:
        rationale.append(
            "No approved, evidence-backed risk atoms are eligible for new scenario generation."
        )
    elif not uncovered:
        rationale.append(
            "Every eligible approved risk is already represented by an active scenario."
        )
    else:
        rationale.append(
            f"Found {uncovered_count} approved risk coverage gap(s)"
            + (
                f" across {len(uncovered_categories)} uncovered categor"
                f"{'y' if len(uncovered_categories) == 1 else 'ies'}"
                if uncovered_categories
                else ""
            )
            + "."
        )
        rationale.append(
            "The range assumes roughly three, two, or one uncovered risk atom(s) "
            "per new scenario, respectively, subject to the run cap."
        )
    duplicate_count = len(risk_duplicates) + len(scenario_duplicates)
    if duplicate_count:
        rationale.append(
            f"Flagged {len(risk_duplicates)} risk and "
            f"{len(scenario_duplicates)} scenario duplicate candidate pair(s); "
            "they narrow the exploratory upper bound but remain human review decisions."
        )
    if cap == 0 and uncovered:
        rationale.append("The configured scenario cap is zero, so no run is recommended.")

    return ScenarioCoverageRecommendation(
        recommended=recommended,
        range_min=range_min,
        range_max=range_max,
        cap=cap,
        approved_evidence_count=approved_evidence_count,
        approved_risk_count=len(approved_risks),
        eligible_risk_count=len(eligible_risks),
        active_scenario_count=len(active_scenarios),
        covered_risk_ids=tuple(sorted(covered)),
        uncovered_risk_ids=tuple(sorted(risk_id for risk_id, _risk in uncovered)),
        uncovered_categories=tuple(uncovered_categories),
        excluded_unresolved_risk_ids=tuple(sorted(unresolved_risk_ids)),
        duplicate_risk_candidates=risk_duplicates,
        duplicate_scenario_candidates=scenario_duplicates,
        ignored_unapproved_evidence_count=ignored_evidence,
        ignored_unapproved_risk_count=ignored_risks,
        rationale=tuple(rationale),
    )
