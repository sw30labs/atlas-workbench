"""Prepare and aggregate the in-memory fictional scoring demo.

This module intentionally has no reader for operator-supplied CSV or JSONL.
It validates rows produced by :mod:`synthetic_events`, strips optional prose,
suppresses sparse synthetic groups, and writes deterministic demo artifacts.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REQUIRED_FIELDS = (
    "event_id",
    "scoring_cell_code",
    "segment_code",
    "date",
    "impact_amount",
)
TEXT_FIELDS = (
    "narrative",
    "description",
    "notes",
    "comment",
    "free_text",
    "person",
    "employee",
    "customer",
)


def _quarter_index(iso_date: str) -> int:
    year, month = int(iso_date[:4]), int(iso_date[5:7])
    return year * 4 + (month - 1) // 3


def scoring_group_id(scoring_cell_code: str, segment_code: str) -> str:
    value = f"{scoring_cell_code}|{segment_code}"
    return "demo-group-" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


@dataclass
class SyntheticAggregationReport:
    n_generated: int = 0
    n_accepted: int = 0
    n_rejected: int = 0
    rejections: list[dict[str, Any]] = field(default_factory=list)
    text_columns_dropped: list[str] = field(default_factory=list)
    n_groups: int = 0
    n_suppressed_groups: int = 0
    n_suppressed_events: int = 0
    periods: list[int] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "n_generated": self.n_generated,
            "n_accepted": self.n_accepted,
            "n_rejected": self.n_rejected,
            "rejections": self.rejections,
            "text_columns_dropped": self.text_columns_dropped,
            "n_groups": self.n_groups,
            "n_suppressed_groups": self.n_suppressed_groups,
            "n_suppressed_events": self.n_suppressed_events,
            "periods": self.periods,
        }


def _validation_error(record: dict[str, Any], row_number: int) -> str | None:
    for field_name in REQUIRED_FIELDS:
        if field_name not in record or record[field_name] in (None, ""):
            return f"row {row_number}: missing required field {field_name}"
    try:
        _quarter_index(str(record["date"]))
    except (ValueError, TypeError, IndexError):
        return f"row {row_number}: unparseable date {record['date']!r}"
    try:
        amount = float(record["impact_amount"])
        if amount <= 0:
            return f"row {row_number}: non-positive impact_amount"
    except (ValueError, TypeError):
        return f"row {row_number}: unparseable impact_amount {record['impact_amount']!r}"
    return None


def prepare_synthetic_events(
    records: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], SyntheticAggregationReport]:
    """Validate generated rows and return prose-free copies."""
    report = SyntheticAggregationReport(n_generated=len(records))
    dropped: set[str] = set()
    accepted: list[dict[str, Any]] = []
    for row_number, source in enumerate(records, start=1):
        record = dict(source)
        for key in list(record):
            if any(part in key.lower() for part in TEXT_FIELDS):
                dropped.add(key)
                del record[key]
        error = _validation_error(record, row_number)
        if error:
            report.n_rejected += 1
            report.rejections.append({"row": row_number, "reason": error})
            continue
        accepted.append(record)
    report.n_accepted = len(accepted)
    report.text_columns_dropped = sorted(dropped)
    return accepted, report


def build_synthetic_features(
    records: list[dict[str, Any]],
    report: SyntheticAggregationReport,
    *,
    min_group_size: int = 5,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Aggregate prepared rows into scoring features and local demo events."""
    if not records:
        return [], []
    base = min(_quarter_index(str(record["date"])) for record in records)
    groups: dict[tuple[str, str, str, int], list[float]] = {}
    local_events: list[dict[str, Any]] = []
    for record in records:
        period = _quarter_index(str(record["date"])) - base
        scoring_cell_code = str(record["scoring_cell_code"])
        segment_code = str(record["segment_code"])
        group_id = scoring_group_id(scoring_cell_code, segment_code)
        groups.setdefault(
            (group_id, scoring_cell_code, segment_code, period), []
        ).append(float(record["impact_amount"]))
        local_events.append(
            {
                "event_id": str(record["event_id"]),
                "risk_id": str(record.get("risk_id") or scoring_cell_code),
                "cell_id": group_id,
                "period": period,
                "impact_amount": float(record["impact_amount"]),
                "plane": "local_detail",
            }
        )

    features: list[dict[str, Any]] = []
    suppressed_events = 0
    suppressed_groups = 0
    for (group_id, scoring_cell_code, segment_code, period), amounts in sorted(
        groups.items()
    ):
        count = len(amounts)
        if count < min_group_size:
            suppressed_groups += 1
            suppressed_events += count
            continue
        amounts.sort()
        features.append(
            {
                "cell_id": group_id,
                "scoring_cell_code": scoring_cell_code,
                "segment_code": segment_code,
                "period": period,
                "count": count,
                "severity_quantiles": {
                    "0.5": (
                        amounts[count // 2]
                        if count % 2
                        else (amounts[count // 2 - 1] + amounts[count // 2]) / 2
                    ),
                    "0.9": amounts[min(count - 1, int(0.9 * (count - 1)))],
                },
                "n_severity": count,
                "plane": "aggregate",
                "source_system": "synthetic-cell-demo",
            }
        )
    report.n_groups = len(groups)
    report.n_suppressed_groups = suppressed_groups
    report.n_suppressed_events = suppressed_events
    report.periods = sorted({key[3] for key in groups})
    return features, local_events


def severity_from_quantiles(quantiles: dict[str, float]) -> dict[str, float]:
    """Estimate lognormal parameters from synthetic q50/q90 values."""
    q50 = quantiles.get("0.5")
    q90 = quantiles.get("0.9")
    if not q50 or q50 <= 0 or not q90 or q90 <= q50:
        return {
            "mu": math.log(q50) if q50 and q50 > 0 else 0.0,
            "sigma": 1.0,
        }
    return {
        "mu": math.log(q50),
        "sigma": max((math.log(q90) - math.log(q50)) / 1.2816, 1e-6),
    }


def write_synthetic_aggregation(
    records: list[dict[str, Any]],
    out_dir: str | Path,
    *,
    min_group_size: int = 5,
) -> dict[str, Any]:
    """Write artifacts for generated records; no external-file reader exists."""
    prepared, report = prepare_synthetic_events(records)
    features, local_events = build_synthetic_features(
        prepared, report, min_group_size=min_group_size
    )
    output = Path(out_dir)
    output.mkdir(parents=True, exist_ok=True)
    features_path = output / "scoring_features.jsonl"
    events_path = output / "synthetic_scoring_events.jsonl"
    report_path = output / "aggregation_report.json"
    features_path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in features),
        encoding="utf-8",
    )
    events_path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in local_events),
        encoding="utf-8",
    )
    report_path.write_text(
        json.dumps(report.to_dict(), indent=2, sort_keys=True), encoding="utf-8"
    )
    return {
        "features": features_path,
        "local_events": events_path,
        "report": report_path,
        "n_features": len(features),
        "n_local_events": len(local_events),
        "aggregation_report": report.to_dict(),
    }
