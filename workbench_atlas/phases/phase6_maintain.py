"""Phase 6: Maintenance & Governance.

Keeps the scenario library healthy over time:

  - Retire scenarios whose underlying risks have faded or materialized
  - Merge overlapping scenarios
  - Flag stale scenarios (no score refresh, outdated rationale)
  - Log pipeline runs for audit trail
  - Optional LLM assist for retirement / merge recommendations

Fully autonomous by default; writes decisions to pipeline_runs and
updates scenario status fields.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from ..llm import OMLXClient, LLMError
from ..db import (
    from_json_str,
    list_evidence_events,
    list_scenarios,
    update_scenario_fields,
    retire_scenario,
    log_pipeline_run,
    now_iso,
    get_scenario,
    record_signpost_assessment,
)
from ..provenance import is_approved_record

logger = logging.getLogger(__name__)


@dataclass
class MaintenanceAction:
    """A single maintenance action taken or recommended."""
    scenario_id: str
    action: str  # retire | merge | refresh | keep
    reason: str
    applied: bool = False
    merge_into: str = ""  # for merge: survivor scenario id


@dataclass
class MaintenanceResult:
    """Result of a maintenance cycle."""
    actions: list[MaintenanceAction] = field(default_factory=list)
    retired: int = 0
    merged: int = 0
    flagged_stale: int = 0
    kept: int = 0
    signposts_evaluated: int = 0
    signpost_evaluations: list["SignpostEvaluation"] = field(
        default_factory=list
    )
    errors: list[str] = field(default_factory=list)


@dataclass
class SignpostEvaluation:
    """A pending-review assessment against newly approved evidence."""
    scenario_id: str
    classification: str
    rationale: str
    evidence_refs: list[str] = field(default_factory=list)
    considered_refs: list[str] = field(default_factory=list)
    review_status: str = "pending"
    applied: bool = False


PROMPT_VERSION = "phase6-maintenance-v1"

STALE_PROMPT = """You are reviewing operational risk scenarios for library hygiene.

Flag scenarios that should be RETIRED if:
- The risk has fully materialized and is now managed as BAU
- The scenario is a near-duplicate of another (prefer the richer one)
- Drivers have permanently faded

Flag REFRESH if rationale is outdated but the risk still applies.
Flag KEEP if still relevant.

Scenarios (id, title, category, status):
{scenarios_json}

Return JSON:
{{
  "recommendations": [
    {{"scenario_id": "...", "action": "retire|refresh|keep|merge", "reason": "...", "merge_into": "optional id"}}
  ]
}}"""

SIGNPOST_PROMPT = """You are testing a banking operational-risk scenario
hypothesis against a packet of human-approved public-source evidence.

Use only the supplied evidence. Do not browse, invent facts or statistics, or
assert a weakness at a named organization. Compare the packet with both the
strengthening and weakening signposts. Select exactly one classification:

- strengthened: approved evidence matches one or more strengthening signposts
- weakened: approved evidence matches one or more weakening signposts
- materialized: approved evidence directly documents the core event mechanism,
  not merely a precursor
- superseded: approved evidence directly invalidates or replaces a central
  premise of the scenario
- unchanged: evidence is irrelevant, ambiguous, balanced, or insufficient

Materialized and superseded require especially direct evidence. This output is
a model-assisted research assessment pending human review; it must not retire,
reactivate, or otherwise change the scenario lifecycle.

Scenario:
{scenario_json}

New approved evidence:
{evidence_json}

Return JSON:
{{
  "classification": "strengthened|unchanged|weakened|materialized|superseded",
  "rationale": "brief comparison tied to explicit signposts",
  "evidence_refs": ["only exact event IDs or source URLs from the packet"]
}}"""

SIGNPOST_CLASSIFICATIONS = frozenset(
    {"strengthened", "unchanged", "weakened", "materialized", "superseded"}
)


def _stored_list(value: object) -> list[str]:
    if isinstance(value, str):
        decoded = from_json_str(value)
        value = decoded if isinstance(decoded, list) else [value]
    if not isinstance(value, (list, tuple, set)):
        return []
    return [
        str(item).strip()
        for item in value
        if str(item).strip()
    ]


def _parse_datetime(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _evidence_timestamp(record: dict[str, Any]) -> datetime | None:
    candidates = [
        _parse_datetime(record.get(field_name))
        for field_name in ("reviewed_at", "retrieved_at", "source_date")
    ]
    valid = [candidate for candidate in candidates if candidate is not None]
    return max(valid) if valid else None


def _record_refs(record: dict[str, Any]) -> list[str]:
    return [
        str(value).strip()
        for value in (record.get("event_id"), record.get("source_url"))
        if str(value or "").strip()
    ]


def _new_approved_evidence(
    scenario: dict[str, Any],
    evidence_records: list[dict[str, Any]],
    *,
    limit: int = 60,
) -> list[dict[str, Any]]:
    """Select only approved evidence newer than the scenario's last check."""
    last_evaluated = _parse_datetime(
        scenario.get("signposts_last_evaluated")
    )
    checked_refs = set(
        _stored_list(scenario.get("signpost_checked_evidence_refs"))
    )
    selected: list[dict[str, Any]] = []
    for record in evidence_records:
        if not isinstance(record, dict) or not is_approved_record(record):
            continue
        record_refs = set(_record_refs(record))
        if checked_refs and record_refs & checked_refs:
            continue
        if not checked_refs and last_evaluated is not None:
            evidence_time = _evidence_timestamp(record)
            if evidence_time is None or evidence_time <= last_evaluated:
                continue
        selected.append(record)
        if len(selected) >= max(0, int(limit)):
            break
    return selected


def evaluate_scenario_signposts(
    scenario: dict[str, Any],
    evidence_records: list[dict[str, Any]],
    client: OMLXClient,
) -> SignpostEvaluation | None:
    """Compare one scenario's signposts with approved evidence only.

    The result is deliberately marked pending and does not mutate either the
    scenario lifecycle or the evidence records.
    """
    scenario_id = str(scenario.get("scenario_id") or "").strip()
    strengthening = _stored_list(scenario.get("strengthening_signposts"))
    weakening = _stored_list(scenario.get("weakening_signposts"))
    approved = [
        record
        for record in evidence_records
        if isinstance(record, dict) and is_approved_record(record)
    ]
    if not scenario_id or not (strengthening or weakening) or not approved:
        return None

    allowed_refs: set[str] = set()
    considered_refs: list[str] = []
    evidence_payload: list[dict[str, Any]] = []
    for record in approved:
        record_refs = _record_refs(record)
        allowed_refs.update(record_refs)
        for ref in record_refs:
            if ref not in considered_refs:
                considered_refs.append(ref)
        evidence_payload.append({
            "evidence_refs": record_refs,
            "source_date": record.get("source_date"),
            "source_publisher": record.get("source_publisher"),
            "source_title": (
                record.get("source_title") or record.get("headline")
            ),
            "independently_authored_summary": record.get(
                "independently_authored_summary"
            ),
        })

    scenario_payload = {
        "scenario_id": scenario_id,
        "title": scenario.get("title"),
        "description": scenario.get("description"),
        "core_risks": _stored_list(scenario.get("core_risks")),
        "transmission_mechanism": scenario.get("transmission_mechanism"),
        "strengthening_signposts": strengthening,
        "weakening_signposts": weakening,
    }
    try:
        response = client.chat_json(
            [{"role": "user", "content": SIGNPOST_PROMPT.format(
                scenario_json=json.dumps(scenario_payload, indent=2),
                evidence_json=json.dumps(evidence_payload, indent=2),
            )}],
            temperature=0.0,
            # Was 2048, the tightest budget in the codebase: real runs reached
            # 1907 output tokens, 93% of the ceiling. Truncation now RAISES
            # rather than returning a clipped answer, so that 7% margin was the
            # difference between a signpost evaluation and a silently dropped
            # scenario. Matched to the other per-scenario generative sites.
            max_tokens=4096,
        )
    except (LLMError, httpx.HTTPError) as exc:
        logger.warning("Signpost evaluation call failed: %s", exc)
        return None
    except Exception as exc:
        logger.error(
            "Signpost evaluation failed unexpectedly: %s",
            exc,
            exc_info=True,
        )
        return None

    if not isinstance(response, dict):
        return None
    classification = str(
        response.get("classification") or "unchanged"
    ).strip().lower()
    if classification not in SIGNPOST_CLASSIFICATIONS:
        classification = "unchanged"
    raw_refs = response.get("evidence_refs") or []
    if isinstance(raw_refs, str):
        raw_refs = [raw_refs]
    filtered_refs = []
    if isinstance(raw_refs, list):
        for value in raw_refs:
            ref = str(value).strip()
            if ref in allowed_refs and ref not in filtered_refs:
                filtered_refs.append(ref)

    rationale = str(response.get("rationale") or "").strip()
    if classification != "unchanged" and not filtered_refs:
        classification = "unchanged"
        qualifier = (
            "Classification downgraded because the response cited no exact "
            "approved evidence reference."
        )
        rationale = f"{rationale} {qualifier}".strip()
    if not rationale:
        rationale = "The approved packet did not support a directional change."

    return SignpostEvaluation(
        scenario_id=scenario_id,
        classification=classification,
        rationale=rationale,
        evidence_refs=filtered_refs,
        considered_refs=considered_refs,
    )


def find_stale_scenarios(
    scenarios: list[dict[str, Any]],
    *,
    max_age_days: int = 365,
) -> list[dict[str, Any]]:
    """Identify scenarios that look stale without LLM.

    Heuristics:
    - status is draft with no judge_score
    - last_scored is missing and creation year is older than max_age_days
    - status already retired → skip
    """
    stale = []
    current_year = datetime.now(timezone.utc).year
    age_year_threshold = max(1, max_age_days // 365)
    for s in scenarios:
        if s.get("status") == "retired":
            continue
        creation = s.get("created_year") or current_year
        try:
            age_years = current_year - int(creation)
        except (TypeError, ValueError):
            age_years = 0
        if s.get("status") == "draft" and not s.get("judge_score"):
            stale.append(s)
        elif age_years >= age_year_threshold and not s.get("last_scored"):
            stale.append(s)
        elif age_years >= age_year_threshold + 2:
            stale.append(s)
    return stale


def _title_tokens(title: str) -> set[str]:
    return {t.lower() for t in (title or "").split() if len(t) > 3}


def _prefer_survivor(a: dict[str, Any], b: dict[str, Any]) -> tuple[str, str]:
    """Choose ``(keep_id, merge_id)`` using explicit record provenance."""
    id_a = a.get("scenario_id") or ""
    id_b = b.get("scenario_id") or ""

    def score(s: dict[str, Any]) -> tuple:
        sid = s.get("scenario_id") or ""
        # Prefer compact IDs (not full-sentence IDs)
        compact = 1 if len(sid) < 48 and " " not in sid else 0
        origin_priority = {
            "operator_authored": 4,
            "synthetic_fixture": 3,
            "pipeline_generated": 2,
            "compound_graduation": 1,
            "legacy_unknown": 0,
        }.get(str(s.get("origin") or "legacy_unknown"), 0)
        desc_len = len(s.get("description") or "")
        try:
            urg = float(s.get("urgency_score") or 0)
        except (TypeError, ValueError):
            urg = 0.0
        return (compact, origin_priority, urg, desc_len)

    if score(a) >= score(b):
        return id_a, id_b
    return id_b, id_a


def find_duplicate_candidates(
    scenarios: list[dict[str, Any]],
    *,
    threshold: float = 0.50,
) -> list[tuple[str, str, float]]:
    """Find potential duplicate pairs by title token overlap.

    Returns list of (keep_id, merge_id, similarity) above Jaccard threshold.
    Skips already-retired scenarios.
    """
    active = [s for s in scenarios if (s.get("status") or "active") != "retired"]
    pairs = []
    for i, a in enumerate(active):
        ta = _title_tokens(a.get("title", ""))
        if not ta:
            continue
        for b in active[i + 1:]:
            tb = _title_tokens(b.get("title", ""))
            if not tb:
                continue
            jacc = len(ta & tb) / len(ta | tb)
            # Also flag strong shared core phrase "cloud identity"
            blob_a = f"{a.get('title', '')} {a.get('description', '')}".lower()
            blob_b = f"{b.get('title', '')} {b.get('description', '')}".lower()
            phrase_boost = 0.0
            for phrase in ("cloud identity", "stablecoin depeg", "deepfake kyc"):
                if phrase in blob_a and phrase in blob_b:
                    phrase_boost = 0.15
                    break
            score = jacc + phrase_boost
            if score >= threshold:
                keep, merge = _prefer_survivor(a, b)
                pairs.append((keep, merge, round(min(score, 1.0), 3)))
    return pairs


def merge_scenario(
    db_path: str,
    merge_id: str,
    keep_id: str,
    *,
    similarity: float | None = None,
    reason: str = "",
) -> bool:
    """Merge merge_id into keep_id by retiring the duplicate.

    Survivor (keep_id) stays active. Loser is retired with an audit note.
    Returns True if the loser was updated.
    """
    if not merge_id or not keep_id or merge_id == keep_id:
        return False
    if get_scenario(db_path, merge_id) is None:
        return False
    if get_scenario(db_path, keep_id) is None:
        return False

    sim_txt = f" similarity={similarity}" if similarity is not None else ""
    note = reason or f"Merged into {keep_id}{sim_txt}"
    ok = retire_scenario(db_path, merge_id, note)
    if ok:
        survivor = get_scenario(db_path, keep_id)
        extra = (survivor.get("additional_information") or "") if survivor else ""
        tag = f"Absorbed duplicate {merge_id}{sim_txt}"
        update_scenario_fields(
            db_path, keep_id,
            {"additional_information": f"{extra}\n{tag}".strip() if extra else tag},
        )
    return ok


def llm_maintenance_recommendations(
    scenarios: list[dict[str, Any]],
    client: OMLXClient,
    *,
    limit: int = 40,
) -> list[MaintenanceAction]:
    """Ask the LLM for retire/refresh/keep recommendations."""
    payload = [
        {
            "scenario_id": s.get("scenario_id"),
            "title": s.get("title", "")[:80],
            "category": s.get("risk_category"),
            "status": s.get("status"),
            "impact": s.get("impact_band"),
        }
        for s in scenarios[:limit]
        if s.get("scenario_id")
    ]
    if not payload:
        return []

    try:
        result = client.chat_json(
            [{"role": "user", "content": STALE_PROMPT.format(
                scenarios_json=json.dumps(payload, indent=2)
            )}],
            temperature=0.0,
            max_tokens=4096,
        )
    except (LLMError, httpx.HTTPError) as e:
        logger.warning(f"LLM maintenance recommendations call failed: {e}")
        return []
    except Exception as e:
        logger.error(f"LLM maintenance recommendations failed unexpectedly: {e}", exc_info=True)
        return []

    actions = []
    for rec in result.get("recommendations", []):
        sid = rec.get("scenario_id", "")
        action = rec.get("action", "keep")
        if action not in ("retire", "refresh", "keep", "merge"):
            action = "keep"
        reason = rec.get("reason", "")
        merge_into = rec.get("merge_into") or ""
        if merge_into and "merge into" not in reason.lower():
            reason = f"{reason} (merge into {merge_into})".strip()
        actions.append(MaintenanceAction(
            scenario_id=sid,
            action=action,
            reason=reason,
            applied=False,
            merge_into=str(merge_into) if merge_into else "",
        ))
    return actions


def run_maintenance_cycle(
    db_path: str,
    client: OMLXClient | None = None,
    *,
    apply: bool | None = None,
    use_llm: bool = True,
    run_id: str | None = None,
    apply_retire: bool | None = None,
    evidence_db_path: str | None = None,
    assess_signposts: bool = True,
) -> MaintenanceResult:
    """Run a full library maintenance cycle.

    1. Load scenarios
    2. Heuristic stale + duplicate detection
    3. Optional LLM recommendations
    4. Apply refresh actions if apply=True
    5. Apply merge or retire only if apply_retire=True
       (requires ATLAS_AUTO_RETIRE=1 by default)
    6. Optionally assess signposts against newly approved local evidence
    7. Log pipeline run

    Safety: retirements are NEVER applied unless apply_retire is True or
    ATLAS_AUTO_RETIRE=1. This includes merges, which retire the merged-away
    scenario. Refresh never touches retired scenarios and never promotes
    drafts that have not passed the judge. Signpost assessments read only the
    supplied evidence database, are stored as pending human review, and never
    change scenario lifecycle status.
    """
    import uuid
    from ..config import auto_retire_enabled

    result = MaintenanceResult()
    run_id = run_id or f"maint-{uuid.uuid4().hex[:8]}"
    started = now_iso()

    # apply defaults True for refresh bookkeeping; merge and retire are gated
    if apply is None:
        apply = True
    if apply_retire is None:
        apply_retire = auto_retire_enabled()

    scenarios = list_scenarios(db_path, status=None)
    if not scenarios:
        scenarios = list_scenarios(db_path, status="active") + list_scenarios(db_path, status="draft")
    active_scenarios = list_scenarios(db_path, status="active")

    # Evaluate falsifiable signposts only against human-approved records in the
    # supplied evidence database. This step performs no discovery or crawling.
    if (
        assess_signposts
        and use_llm
        and client is not None
        and evidence_db_path
    ):
        try:
            approved_evidence = list_evidence_events(
                evidence_db_path,
                approved_only=True,
            )
        except (sqlite3.Error, ValueError) as exc:
            message = f"Could not load approved signpost evidence: {exc}"
            logger.warning(message)
            result.errors.append(message)
            approved_evidence = []
        evaluated_at = now_iso()
        for scenario in active_scenarios:
            new_evidence = _new_approved_evidence(
                scenario,
                approved_evidence,
            )
            evaluation = evaluate_scenario_signposts(
                scenario,
                new_evidence,
                client,
            )
            if evaluation is None:
                # None means either "nothing to evaluate" (no signposts, no
                # approved evidence) or "the call failed and was logged" — and
                # from here the two are indistinguishable, so a scenario that
                # was SUPPOSED to be evaluated could vanish from the run with
                # nothing in the report. Record the skip; a maintenance pass
                # that silently examined fewer scenarios than it claims is the
                # failure this phase can least afford.
                if _stored_list(scenario.get("strengthening_signposts")) or _stored_list(
                    scenario.get("weakening_signposts")
                ):
                    result.errors.append(
                        f"Signpost evaluation produced no result for "
                        f"{scenario.get('scenario_id', '?')} — see the log for cause"
                    )
                continue
            result.signpost_evaluations.append(evaluation)
            result.signposts_evaluated += 1
            if not apply:
                continue

            try:
                snapshot = record_signpost_assessment(
                    db_path,
                    evaluation.scenario_id,
                    classification=evaluation.classification,
                    rationale=evaluation.rationale,
                    evidence_refs=evaluation.evidence_refs,
                    considered_refs=evaluation.considered_refs,
                    evaluated_at=evaluated_at,
                )
                evaluation.applied = bool(snapshot)
            except (sqlite3.Error, ValueError) as exc:
                message = (
                    f"Signpost update failed for "
                    f"{evaluation.scenario_id}: {exc}"
                )
                logger.error(message)
                result.errors.append(message)

    # Heuristic stale
    stale = find_stale_scenarios(scenarios)
    for s in stale:
        result.actions.append(MaintenanceAction(
            scenario_id=s.get("scenario_id", ""),
            action="refresh",
            reason="Heuristic: stale draft or aged without rescore",
        ))
        result.flagged_stale += 1

    # Duplicates → (keep, merge, sim)
    dups = find_duplicate_candidates(scenarios)
    for keep_id, merge_id, sim in dups:
        result.actions.append(MaintenanceAction(
            scenario_id=merge_id,
            action="merge",
            reason=f"Title/theme similarity {sim} with {keep_id}",
            merge_into=keep_id,
        ))

    # LLM recommendations
    if use_llm and client is not None:
        llm_actions = llm_maintenance_recommendations(scenarios, client)
        for a in llm_actions:
            # Parse "merge into X" from reason if merge_into not set
            if a.action == "merge" and not a.merge_into:
                import re
                m = re.search(r"merge into ([A-Za-z0-9_.\-]+)", a.reason, re.I)
                if m:
                    a.merge_into = m.group(1)
            result.actions.append(a)

    # Apply. Retire AND merge are gated by apply_retire: a merge retires the
    # merged-away scenario, so an ungated merge would bypass the retire gate.
    already_merged: set[str] = set()
    for action in result.actions:
        if action.action in ("retire", "merge") and not apply_retire:
            logger.info(
                "%s recommended for %s but not applied "
                "(set ATLAS_AUTO_RETIRE=1 to enable)",
                action.action.capitalize(),
                action.scenario_id,
            )
            continue

        if action.action == "retire" and action.scenario_id:
            if not apply:
                continue
            try:
                ok = retire_scenario(db_path, action.scenario_id, action.reason)
                action.applied = ok
                if ok:
                    result.retired += 1
            except sqlite3.Error as e:
                logger.error(f"Retire failed for {action.scenario_id}: {e}")
                result.errors.append(str(e))
            continue

        if not apply:
            continue

        if action.action == "merge" and action.scenario_id and action.merge_into:
            if action.scenario_id in already_merged:
                action.reason = f"{action.reason} (skipped: already merged)"
                continue
            # Don't merge into something already retired as a loser
            if action.merge_into in already_merged:
                action.reason = f"{action.reason} (skipped: target already merged away)"
                continue
            try:
                ok = merge_scenario(
                    db_path,
                    action.scenario_id,
                    action.merge_into,
                    reason=action.reason,
                )
                action.applied = ok
                if ok:
                    result.merged += 1
                    result.retired += 1  # merge retires the loser
                    already_merged.add(action.scenario_id)
            except sqlite3.Error as e:
                logger.error(f"Merge failed for {action.scenario_id}: {e}")
                result.errors.append(str(e))
        elif action.action == "refresh" and action.scenario_id:
            try:
                current = get_scenario(db_path, action.scenario_id)
                if current is None:
                    action.reason = f"{action.reason} (skipped: unknown scenario)"
                    continue
                status = (current.get("status") or "active").lower()
                if status == "retired":
                    action.reason = f"{action.reason} (skipped: retired scenarios are not reactivated)"
                    continue
                if status == "draft" and not current.get("judge_score"):
                    action.reason = f"{action.reason} (skipped: draft has not passed QA)"
                    continue
                update_scenario_fields(
                    db_path, action.scenario_id,
                    {"status": "active"},
                )
                action.applied = True
            except sqlite3.Error as e:
                logger.error(f"Refresh failed for {action.scenario_id}: {e}")
                result.errors.append(str(e))
        elif action.action == "keep":
            result.kept += 1
            action.applied = True

    completed = now_iso()
    try:
        log_pipeline_run(
            db_path,
            run_id=run_id,
            phase="phase6_maintain",
            status="completed" if not result.errors else "completed_with_errors",
            started_at=started,
            completed_at=completed,
            items_processed=(
                len(result.actions) + result.signposts_evaluated
            ),
            errors="; ".join(result.errors) if result.errors else None,
        )
    except Exception as e:
        logger.warning(f"Could not log pipeline run: {e}")

    logger.info(
        f"Maintenance: {result.retired} retired, {result.flagged_stale} stale, "
        f"{result.kept} kept, {result.signposts_evaluated} signposts evaluated, "
        f"{len(result.errors)} errors"
    )
    return result
