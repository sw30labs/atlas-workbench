"""Read-only source-quality diagnostics and honest enterprise pilot measures."""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from itertools import combinations
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from .config import Settings, get_settings
from .provenance import canonical_public_url, url_dedup_variants

APPROVED = {"approved", "approved_synthetic"}


def _read_table(path: Path, table: str) -> tuple[list[dict], str]:
    if not path.is_file():
        return [], "missing_store"
    conn = None
    try:
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
            return [], "missing_table"
        # Only internal constant table names reach this helper.
        return [dict(row) for row in conn.execute(f'SELECT * FROM "{table}"')], "available"
    except sqlite3.DatabaseError:
        return [], "unreadable_store"
    finally:
        if conn:
            conn.close()


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _timestamp(value: object) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, datetime.min.time())
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _domain(value: object) -> str:
    try:
        parsed = urlsplit(_text(value))
        return (parsed.hostname or "").lower() if parsed.scheme.lower() in {"http", "https"} else ""
    except ValueError:
        return ""


def _groups(records: list[dict], key) -> dict:
    grouped: dict[str, list[str]] = defaultdict(list)
    for record in records:
        if label := key(record):
            grouped[label].append(record["event_id"])
    known = sum(map(len, grouped.values()))
    rows = [{"label": label, "count": len(ids), "share_of_known": len(ids) / known,
             "event_ids": sorted(ids)} for label, ids in grouped.items()]
    rows.sort(key=lambda row: (-row["count"], row["label"]))
    return {"groups": rows, "known_records": known, "missing_records": len(records) - known,
            "distinct_labels": len(rows), "largest_share_of_known": rows[0]["share_of_known"] if rows else None,
            "concentration_hhi": sum(row["share_of_known"] ** 2 for row in rows) if rows else None}


def _recency(records: list[dict], field: str, now: datetime, stale_days: int) -> dict:
    rows = []
    counts = Counter()
    for record in records:
        raw = record.get(field)
        parsed = _timestamp(raw)
        if not _text(raw):
            state, age = "missing", None
        elif parsed is None:
            state, age = "invalid", None
        else:
            age = (now.date() - parsed.date()).days
            state = "future" if age < 0 else "older_than_threshold" if age > stale_days else "within_threshold"
        counts[state] += 1
        rows.append({"event_id": record["event_id"], "value": raw, "status": state, "age_days": age})
    return {"field": field, "status": "not_recorded" if counts["missing"] == len(records) else "measured",
            "counts": {key: counts[key] for key in ("missing", "invalid", "future", "older_than_threshold", "within_threshold")},
            "records": rows}


def _refs(value: object) -> tuple[list[str], bool]:
    if value is None or value == "":
        return [], False
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return [], True
    if not isinstance(value, list) or any(not isinstance(ref, str) or not ref.strip() for ref in value):
        return [], True
    return list(dict.fromkeys(ref.strip() for ref in value)), False


def evidence_quality(settings: Settings, *, as_of: date | datetime | str | None = None,
                     declared_regions: list[str] | None = None, stale_days: int = 180) -> dict:
    """Describe source coverage without equating publications with incidents.

    No network checks, migrations or organization-context reads occur. The
    configurable age threshold is a review queue heuristic, not expiry of truth.
    """
    now = datetime.now(timezone.utc) if as_of is None else _timestamp(as_of)
    if now is None or type(stale_days) is not int or stale_days < 1:
        raise ValueError("as_of must be a valid date and stale_days a positive integer")
    if declared_regions is not None and (not isinstance(declared_regions, list)
            or any(not isinstance(region, str) or not region.strip() for region in declared_regions)):
        raise ValueError("declared_regions must contain explicit nonempty region labels")
    evidence, evidence_status = _read_table(settings.evidence_db_path, "evidence_events")
    scenarios, scenario_status = _read_table(settings.scenario_db_path, "scenarios")
    try:
        risks = json.loads(settings.emerging_risks_path.read_text())
        if not isinstance(risks, list) or any(not isinstance(risk, dict) for risk in risks):
            raise ValueError("invalid register")
        risk_status = "available"
    except FileNotFoundError:
        risks, risk_status = [], "missing_store"
    except (OSError, ValueError):
        risks, risk_status = [], "unreadable_store"
    evidence = [{**record, "event_id": _text(record.get("event_id")) or f"unidentified-row-{i+1}"}
                for i, record in enumerate(evidence)]
    publishers = _groups(evidence, lambda r: " ".join(_text(r.get("source_publisher")).casefold().split()))
    domains = _groups(evidence, lambda r: _domain(r.get("source_url")))
    fingerprints = _groups(evidence, lambda r: _text(r.get("content_fingerprint")).casefold())
    urls = _groups(evidence, lambda r: canonical_public_url(r["source_url"])
                   if _domain(r.get("source_url")) else "")
    duplicate_fingerprints = [row for row in fingerprints["groups"] if row["count"] > 1]
    duplicate_urls = [row for row in urls["groups"] if row["count"] > 1]
    fields = ["source_publisher", "source_url", "source_date", "retrieved_at", "content_fingerprint",
              "region", "review_status"]
    missing = [{"event_id": record["event_id"], "fields": [field for field in fields if not _text(record.get(field))]}
               for record in evidence]
    missing = [row for row in missing if row["fields"]]
    recency = {
        "publication": _recency(evidence, "source_date", now, stale_days),
        "retrieval": _recency(evidence, "retrieved_at", now, stale_days),
        "event": _recency(evidence, "event_date", now, stale_days),
        "threshold_days": stale_days,
        "interpretation": "source_date is publication date; retrieved_at is acquisition date. Neither is substituted for event occurrence date.",
    }
    date_anomalies = []
    for record in evidence:
        publication, retrieval, event = (_timestamp(record.get(field)) for field in ("source_date", "retrieved_at", "event_date"))
        if publication and retrieval and retrieval.date() < publication.date():
            date_anomalies.append({"event_id": record["event_id"], "issue": "retrieval_precedes_publication"})
        if publication and event and event.date() > publication.date():
            date_anomalies.append({"event_id": record["event_id"], "issue": "event_postdates_publication_check_if_forecast"})
    stale = []
    for kind, records, key in (("evidence", evidence, "event_id"), ("risks", risks, "risk_id")):
        for i, record in enumerate(records):
            if record.get("review_status") not in APPROVED:
                continue
            raw = record.get("reviewed_at") or record.get("last_reviewed")
            reviewed = _timestamp(raw)
            age = (now.date() - reviewed.date()).days if reviewed else None
            reason = ("missing_review_date" if not _text(raw) else "invalid_review_date" if reviewed is None
                      else "future_review_date" if age < 0 else "older_than_threshold" if age > stale_days else None)
            if reason:
                stale.append({"kind": kind, "record_id": record.get(key) or record.get("issue") or f"row-{i+1}",
                              "reviewed_at": raw, "age_days": age, "reason": reason})
    by_ref: dict[str, list[dict]] = defaultdict(list)
    for record in evidence:
        by_ref[record["event_id"]].append(record)
        if _domain(record.get("source_url")):
            for ref in url_dedup_variants(record["source_url"]):
                by_ref[ref].append(record)
    broken = []
    for kind, records, key in (("risks", risks, "risk_id"), ("scenarios", scenarios, "scenario_id")):
        for i, record in enumerate(records):
            record_id = record.get(key) or record.get("issue") or f"row-{i+1}"
            refs, malformed = _refs(record.get("evidence_refs"))
            if malformed:
                broken.append({"kind": kind, "record_id": record_id, "reference": None, "status": "malformed_reference_list"})
            if not refs and not malformed:
                broken.append({"kind": kind, "record_id": record_id, "reference": None, "status": "no_evidence_references"})
            for ref in refs:
                candidates = by_ref.get(ref, [])
                if not candidates and _domain(ref):
                    candidates = [e for variant in url_dedup_variants(ref) for e in by_ref.get(variant, [])]
                status = ("not_checked_evidence_unavailable" if evidence_status != "available"
                          else "missing_evidence") if not candidates else "unapproved_evidence" if not any(
                    e.get("review_status") in APPROVED for e in candidates
                ) else None
                if status:
                    broken.append({"kind": kind, "record_id": record_id, "reference": ref, "status": status})
    regions = _groups(evidence, lambda r: " ".join(_text(r.get("region")).casefold().split()))
    declared = sorted({" ".join(region.casefold().split()) for region in declared_regions or []})
    represented = {row["label"] for row in regions["groups"]}
    availability = {"evidence": evidence_status, "risks": risk_status, "scenarios": scenario_status}
    return {
        "schema_version": 1, "as_of": now.isoformat(), "availability": availability,
        "status": "available" if all(v == "available" for v in availability.values()) else "partial",
        "summary": {"evidence_records": len(evidence), "approved_evidence": sum(r.get("review_status") in APPROVED for r in evidence),
                    "risks": len(risks), "scenarios": len(scenarios), "records_with_missing_fields": len(missing),
                    "stale_or_undated_approvals": len(stale), "broken_reference_findings": len(broken)},
        "independence": {"status": "not_validated", "publishers": publishers, "domains": domains,
                         "interpretation": "Distinct publisher labels or hostnames are not independent incidents or independent source families. Ownership, syndication and shared underlying events require explicit review."},
        "citation_breadth": {
            "legacy_field": "evidence_strength", "display_label": "Citation breadth",
            "interpretation": "The legacy evidence_strength label counts citation references. It is citation breadth, not independent corroboration, source reliability or confidence in an operational claim.",
            "records": [{"kind": kind, "record_id": record.get(key) or record.get("issue"),
                         "citation_breadth": record.get("evidence_strength"),
                         "evidence_strength": record.get("evidence_strength")}
                        for kind, records, key in (("risks", risks, "risk_id"), ("scenarios", scenarios, "scenario_id"))
                        for record in records],
        },
        "duplicates": {"fingerprint_groups": duplicate_fingerprints, "canonical_url_groups": duplicate_urls,
                       "interpretation": "Repeated content or URLs are review candidates, not silently merged records; different URLs do not establish independent events."},
        "recency": recency, "date_anomalies": date_anomalies,
        "missing_data": {"field_counts": {field: sum(field in row["fields"] for row in missing) for field in fields}, "records": missing},
        "stale_approvals": {"threshold_days": stale_days, "records": stale,
                            "interpretation": "Age triggers a review suggestion; historical source facts do not automatically expire."},
        "broken_refs": {"records": broken, "url_reachability": "not_checked"},
        "region_coverage": {"scope_status": "declared" if declared else "not_declared", "declared_regions": declared,
                            "missing_declared_regions": sorted(set(declared) - represented) if declared else [],
                            **regions, "interpretation": "Region labels describe the recorded corpus. Missing declared regions are sourcing gaps, not evidence of low risk; undeclared global coverage is not measurable."},
    }


Probability = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False, strict=True)]
Number = Annotated[float, Field(allow_inf_nan=False, strict=True)]


class PilotRecord(BaseModel):
    """One blinded reviewer rating of one candidate in one retrospective case."""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    case_id: str = Field(min_length=1)
    candidate_id: str = Field(min_length=1)
    arm: Literal["baseline", "atlas"]
    reviewer_id: str = Field(min_length=1)
    blinded: StrictBool = False
    useful: StrictBool | None = None
    novel: StrictBool | None = None
    actionable: StrictBool | None = None
    duplicate: StrictBool | None = None
    time_to_brief_minutes: Annotated[float, Field(ge=0, allow_inf_nan=False, strict=True)] | None = None
    forecast_probability: Probability | None = None
    forecast_at: str | None = None
    outcome: Literal[0, 1] | None = None
    outcome_status: Literal["observed", "synthetic", "unresolved"] = "unresolved"
    outcome_source: str | None = None
    outcome_at: str | None = None
    target: str | None = None
    prediction_interval: tuple[Number, Number] | None = None
    interval_level: Probability | None = None
    observed_value: Number | None = None
    observed_value_source: str | None = None
    units: str | None = None

    @model_validator(mode="after")
    def coherent_dates_and_intervals(self):
        for field in ("forecast_at", "outcome_at"):
            raw = getattr(self, field)
            if raw is not None and _timestamp(raw) is None:
                raise ValueError(f"{field} must be an ISO date/time")
        if self.prediction_interval and self.prediction_interval[0] > self.prediction_interval[1]:
            raise ValueError("prediction interval lower bound exceeds upper bound")
        if self.interval_level is not None and not 0 < self.interval_level < 1:
            raise ValueError("interval_level must be strictly between zero and one")
        return self


def _consensus(values: list[bool | None]) -> bool | None:
    known = [v for v in values if v is not None]
    positive = sum(known)
    if not known or positive * 2 == len(known):
        return None
    return positive * 2 > len(known)


def _candidate_metric(candidates: list[list[PilotRecord]], field: str) -> dict:
    results = []
    for ratings in candidates:
        values = [(r.useful and r.novel) if r.useful is not None and r.novel is not None else None
                  for r in ratings] if field == "useful_novel" else [getattr(r, field) for r in ratings]
        results.append(_consensus(values))
    resolved = [value for value in results if value is not None]
    return {"status": "measured" if resolved else "not_validated",
            "value": sum(resolved) / len(resolved) if resolved else None,
            "numerator": sum(resolved), "resolved_candidates": len(resolved), "total_candidates": len(results),
            "missing_or_tied_candidates": len(results) - len(resolved)}


def _agreement(candidates: list[list[PilotRecord]], field: str) -> dict:
    agreements = pairs = multi = 0
    for ratings in candidates:
        values = [getattr(r, field) for r in ratings if getattr(r, field) is not None]
        multi += int(len(values) >= 2)
        for left, right in combinations(values, 2):
            pairs += 1
            agreements += left == right
    return {"status": "measured" if pairs else "not_validated", "pairwise_agreement": agreements / pairs if pairs else None,
            "agreeing_pairs": agreements, "comparable_pairs": pairs, "multi_reviewer_candidates": multi,
            "interpretation": "Descriptive pairwise agreement; not chance-adjusted reliability or independent sample size."}


def _calibration(candidates: list[list[PilotRecord]], now: datetime) -> dict:
    brier: dict[str, list[float]] = defaultdict(list)
    covered: dict[tuple[str, str, float], list[bool]] = defaultdict(list)
    excluded = Counter()
    fields = ["forecast_probability", "forecast_at", "outcome", "outcome_status", "outcome_source", "outcome_at",
              "target", "prediction_interval", "interval_level", "observed_value", "observed_value_source", "units"]
    for ratings in candidates:
        # Prediction/outcome facts are candidate-level, not one fresh event
        # for each rater. Conflicting duplicated facts fail validation.
        facts = {}
        for field in fields:
            values = {json.dumps(getattr(r, field)) for r in ratings
                      if field in r.model_fields_set and getattr(r, field) is not None}
            if len(values) > 1:
                raise ValueError(f"conflicting candidate-level {field} facts")
            facts[field] = json.loads(next(iter(values))) if values else None
        if facts["forecast_probability"] is None and facts["prediction_interval"] is None:
            continue
        forecast, outcome_at = _timestamp(facts["forecast_at"]), _timestamp(facts["outcome_at"])
        if (facts["outcome_status"] != "observed" or not forecast or not outcome_at
                or not forecast < outcome_at <= now or not _text(facts["target"])):
            excluded["unresolved_synthetic_undated_or_not_prospective"] += 1
            continue
        if facts["forecast_probability"] is not None:
            if facts["outcome"] is not None and _text(facts["outcome_source"]):
                brier[facts["target"]].append((facts["forecast_probability"] - facts["outcome"]) ** 2)
            else:
                excluded["missing_binary_outcome_or_source"] += 1
        if facts["prediction_interval"] is not None:
            if (facts["observed_value"] is not None and _text(facts["observed_value_source"])
                    and _text(facts["units"]) and facts["interval_level"] is not None):
                lo, hi = facts["prediction_interval"]
                covered[(facts["target"], facts["units"], facts["interval_level"])].append(lo <= facts["observed_value"] <= hi)
            else:
                excluded["missing_numeric_observation_source_units_or_nominal_level"] += 1
    return {
        "brier": {"status": "measured" if brier else "not_validated",
                  "by_target": [{"target": target, "value": statistics.mean(values), "n_outcomes": len(values)}
                                for target, values in sorted(brier.items())]},
        "numeric_prediction_interval_coverage": {
            "status": "measured" if covered else "not_validated",
            "groups": [{"target": key[0], "units": key[1], "nominal_level": key[2],
                        "empirical_coverage": sum(values) / len(values), "n_outcomes": len(values)}
                       for key, values in sorted(covered.items())],
            "interpretation": "Coverage is for numeric outcome prediction intervals. It does not measure whether probability confidence intervals cover an unknown true probability.",
        },
        "excluded_candidates": dict(excluded),
    }


def assess_pilot(records: list[dict], *, as_of: date | datetime | str | None = None) -> dict:
    """Summarize supplied observations; never manufacture a validation pass."""
    if not isinstance(records, list) or len(records) > 10000:
        raise ValueError("pilot input must be a list of at most 10000 reviewer records")
    now = datetime.now(timezone.utc) if as_of is None else _timestamp(as_of)
    if now is None:
        raise ValueError("as_of must be a valid date/time")
    parsed = [PilotRecord.model_validate(record) for record in records]
    candidates: dict[tuple[str, str, str], list[PilotRecord]] = defaultdict(list)
    seen = set()
    times: dict[tuple[str, str], set[float]] = defaultdict(set)
    for record in parsed:
        key = (record.arm, record.case_id, record.candidate_id)
        rating_key = (*key, record.reviewer_id)
        if rating_key in seen:
            raise ValueError("duplicate rating for the same arm/case/candidate/reviewer")
        seen.add(rating_key)
        candidates[key].append(record)
        if record.time_to_brief_minutes is not None:
            times[(record.arm, record.case_id)].add(record.time_to_brief_minutes)
    if any(len(values) > 1 for values in times.values()):
        raise ValueError("time_to_brief_minutes must be consistent within each arm and case")
    metrics = {"useful_novel_exposure_rate": "useful_novel", "duplicate_rate": "duplicate", "actionability_rate": "actionable"}
    arms = {}
    for arm in ("baseline", "atlas"):
        grouped = [values for key, values in candidates.items() if key[0] == arm]
        case_times = [next(iter(value)) for (method, _case), value in times.items() if method == arm]
        arms[arm] = {name: _candidate_metric(grouped, field) for name, field in metrics.items()}
        arms[arm].update({"candidate_count": len(grouped), "rating_count": sum(map(len, grouped)),
                          "time_to_brief": {"status": "measured" if case_times else "not_validated",
                                            "median_minutes": statistics.median(case_times) if case_times else None,
                                            "case_count": len(case_times)},
                          "calibration": _calibration(grouped, now)})
    cases = {arm: {r.case_id for r in parsed if r.arm == arm} for arm in arms}
    matched = sorted(cases["baseline"] & cases["atlas"])
    blinded = bool(parsed) and all(r.blinded for r in parsed)
    comparisons = {}
    for name, field in metrics.items():
        deltas = []
        if blinded:
            for case in matched:
                values = {arm: _candidate_metric([ratings for key, ratings in candidates.items()
                                                 if key[0] == arm and key[1] == case], field)["value"] for arm in arms}
                if all(value is not None for value in values.values()):
                    deltas.append(values["atlas"] - values["baseline"])
        comparisons[name] = {"status": "measured" if deltas else "not_validated",
                             "mean_paired_case_delta_atlas_minus_baseline": statistics.mean(deltas) if deltas else None,
                             "matched_resolved_cases": len(deltas)}
    saved_minutes = [next(iter(times[("baseline", case)])) - next(iter(times[("atlas", case)]))
                     for case in matched if blinded and ("baseline", case) in times and ("atlas", case) in times]
    comparisons["time_to_brief"] = {"status": "measured" if saved_minutes else "not_validated",
                                    "median_paired_minutes_saved": statistics.median(saved_minutes) if saved_minutes else None,
                                    "matched_cases": len(saved_minutes)}
    return {"schema_version": 1, "as_of": now.isoformat(), "validation_status": "not_validated",
            "interpretation": "Descriptive pilot measurements, not a deployment approval, causal-effect estimate or significance test. Pre-register acceptance gates and independently review outcomes.",
            "design": {"all_ratings_declared_blinded": blinded, "matched_cases": matched,
                       "unpaired_cases": {arm: sorted(values - set(matched)) for arm, values in cases.items()},
                       "rating_unit": "unique arm/case/candidate/reviewer; rates use candidate majority with ties excluded",
                       "comparison_unit": "matched blinded case; candidate counts do not create independent cases"},
            "arms": arms, "comparisons": comparisons,
            "reviewer_agreement": {field: _agreement(list(candidates.values()), field)
                                   for field in ("useful", "novel", "actionable", "duplicate")}}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Source-quality diagnostics and enterprise pilot instruments")
    sub = parser.add_subparsers(dest="command", required=True)
    pilot = sub.add_parser("pilot", help="Measure explicitly supplied blinded pilot observations")
    pilot.add_argument("--input", type=Path, required=True)
    sub.add_parser("evidence", help="Read source-quality diagnostics without modifying stores")
    args = parser.parse_args(argv)
    try:
        report = assess_pilot(json.loads(args.input.read_text())) if args.command == "pilot" else evidence_quality(get_settings())
    except (ValueError, OSError) as exc:
        parser.exit(2, f"Invalid diagnostic input: {exc}\n")
    print(json.dumps(report, indent=2, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
