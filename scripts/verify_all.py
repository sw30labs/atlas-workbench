#!/usr/bin/env python3
"""Run an offline integrity check against temporary, synthetic data."""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    from workbench_atlas.config import Settings
    from workbench_atlas.db import (
        add_evidence_event,
        get_evidence_event_stats,
        get_scenario,
        to_json_str,
        upsert_scenario,
    )
    from workbench_atlas.pipeline import build_pipeline, run_pipeline
    from workbench_atlas.scouting import load_registry, registry_summary
    from workbench_atlas.schema import (
        EVIDENCE_EVENT_COLUMNS,
        SCENARIO_COLUMNS,
        get_schema_info,
        migrate,
        migrate_evidence_events,
    )

    failures: list[str] = []

    def check(name: str, condition: bool, detail: str = "") -> None:
        marker = "PASS" if condition else "FAIL"
        print(f"  {marker}  {name}" + (f" — {detail}" if detail else ""))
        if not condition:
            failures.append(name)

    with tempfile.TemporaryDirectory(prefix="workbench-atlas-verify-") as raw:
        workspace = Path(raw)
        scenario_db = workspace / "scenarios.db"
        evidence_db = workspace / "evidence-events.db"
        risks_path = workspace / "emerging_risks.json"
        risks_path.write_text(
            json.dumps([
                {
                    "issue": "Synthetic cloud concentration",
                    "description": "A fictional shared-service dependency for testing.",
                    "status": "active",
                    "evidence_refs": ["SYN-EVT-000"],
                    "source_type": "synthetic",
                    "provenance_note": "Synthetic fixture; not a real risk signal.",
                    "review_status": "approved_synthetic",
                }
            ])
        )

        migrate(str(scenario_db))
        migrate_evidence_events(str(evidence_db))
        info = get_schema_info(str(scenario_db))
        scenario_names = {name for name, _ in info["scenarios_columns"]}
        check(
            "scenario schema",
            scenario_names == {name for name, _ in SCENARIO_COLUMNS},
            f"{len(scenario_names)} columns",
        )
        check(
            "supporting tables",
            {
                "scenarios",
                "qa_logs",
                "pattern_detections",
                "causal_findings",
                "pipeline_runs",
            }.issubset(info["tables"]),
        )

        scenario = {
            "scenario_id": "SYN-001",
            "origin": "synthetic_fixture",
            "risk_category": "Digital resilience",
            "title": "Synthetic shared-service outage",
            "description": "A fictional service outage disrupts a hypothetical bank.",
            "created_year": 2026,
            "financial_impact_band": "Major",
            "event_driver_class": "Technology disruption",
            "primary_cause_class": "Process design",
            "impact_band": "High",
            "external_drivers": to_json_str(["Technology"]),
            "signal_maturity": "emerging",
            "momentum": "stable",
            "evidence_strength": "limited",
            "affected_capabilities": to_json_str(["Service recovery"]),
            "transmission_mechanism": (
                "A fictional shared dependency delays service recovery."
            ),
            "second_order_effects": to_json_str(
                ["Manual queues grow after technical restoration."]
            ),
            "strengthening_signposts": to_json_str(
                ["More fictional services adopt the dependency."]
            ),
            "weakening_signposts": to_json_str(
                ["A tested independent recovery path becomes available."]
            ),
            "discussion_prompts": to_json_str(
                ["Which fictional recovery dependency has no substitute?"]
            ),
            "signpost_status": "not_evaluated",
            "signpost_review_status": "not_reviewed",
            "status": "active",
        }
        check("scenario insert", upsert_scenario(str(scenario_db), scenario))
        check(
            "scenario read",
            (get_scenario(str(scenario_db), "SYN-001") or {}).get("title")
            == scenario["title"],
        )
        stored_scenario = get_scenario(str(scenario_db), "SYN-001") or {}
        check(
            "radar scenario fields",
            (
                stored_scenario.get("external_drivers")
                == scenario["external_drivers"]
                and stored_scenario.get("signpost_status") == "not_evaluated"
            ),
        )

        scout_summary = registry_summary(load_registry())
        check(
            "scout registry",
            (
                scout_summary["network_collection_default"] is False
                and not scout_summary["missing_categories"]
            ),
            f"{scout_summary['source_count']} reviewed sources",
        )

        start = date(2025, 1, 1)
        for index in range(10):
            source_date = start + timedelta(days=index * 14)
            event = {
                "event_id": f"SYN-EVT-{index:03d}",
                "headline": f"Synthetic public event {index}",
                "organization": "Northstar Bank (fictional)",
                "country": "Exampleland",
                "region": "North America",
                "loss_amount_usd": float((index + 1) * 1_000_000),
                "source_date": source_date.isoformat(),
                "risk_category": (
                    "Digital resilience"
                    if index % 2 == 0
                    else "Processing integrity"
                ),
                "event_driver_class": "Technology disruption",
                "primary_cause_class": "Process design",
                "source_url": f"https://example.com/synthetic/{index}",
                "source_publisher": "Example Public Register",
                "source_title": f"Synthetic notice {index}",
                "retrieved_at": "2026-01-01T00:00:00+00:00",
                "source_type": "synthetic",
                "record_type": "event",
                "provenance_class": "synthetic",
                "source_locator": None,
                "independently_authored_summary": "A fictional record used only for tests.",
                "research_inference": "No factual inference; synthetic test record.",
                "rights_review": "not_applicable_synthetic",
                "content_fingerprint": None,
                "review_status": "approved_synthetic",
                "reviewed_at": "2026-01-01",
                "taxonomy_version": "atlas-1.0",
                "provenance_note": "Synthetic fixture; no real organization or event.",
            }
            check(
                f"evidence-event insert {index + 1}",
                add_evidence_event(str(evidence_db), event),
            )

        stats = get_evidence_event_stats(str(evidence_db))
        check(
            "evidence-event schema column count",
            len(EVIDENCE_EVENT_COLUMNS) == 27,
        )
        check("evidence-event aggregate", stats["total"] == 10, str(stats["total"]))
        check("graph compiles", build_pipeline() is not None)

        settings = Settings(
            workspace=workspace,
            scenario_db_path=scenario_db,
            evidence_db_path=evidence_db,
            emerging_risks_path=risks_path,
            driftlab_enabled=False,
        )
        result = run_pipeline(settings, skip_llm=True, num_scenarios=0)
        summary = result.summary()
        for phase in ("phase4", "phase5", "phase6"):
            check(
                f"{phase} offline",
                summary["phases"][phase]["status"] == "ok",
                str(summary["phases"][phase]),
            )
        check("pipeline errors", not result.errors, str(result.errors))

    if failures:
        print(f"RESULT: FAIL ({len(failures)} checks)")
        return 1
    print("RESULT: ALL OFFLINE CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
