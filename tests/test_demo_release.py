"""Release-safety tests for the bundled synthetic demonstration corpus."""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_demo_fixtures_are_explicitly_synthetic():
    from contingency_atlas.categories import (
        derive_financial_impact_band,
        derive_impact_band,
    )

    evidence = json.loads(
        (ROOT / "examples" / "demo_evidence.json").read_text(encoding="utf-8")
    )
    emerging = json.loads(
        (ROOT / "examples" / "demo_emerging_risks.json").read_text(
            encoding="utf-8"
        )
    )
    scenarios = json.loads(
        (ROOT / "examples" / "demo_scenarios.json").read_text(encoding="utf-8")
    )
    manifest = json.loads(
        (ROOT / "examples" / "fixture_manifest.json").read_text(encoding="utf-8")
    )

    assert evidence
    assert all(item["source_type"] == "synthetic" for item in evidence)
    assert all(item["source_url"] is None for item in evidence)
    assert all("fictional" in item["provenance_note"].lower() for item in evidence)

    assert emerging
    assert all(item["source_type"] == "synthetic" for item in emerging)
    assert all("fictional" in item["provenance_note"].lower() for item in emerging)
    assert all(item["external_drivers"] for item in emerging)
    assert all(
        item["signal_maturity"]
        in {"weak", "emerging", "accelerating", "established", "materialized", "fading"}
        for item in emerging
    )
    assert all(item["evidence_strength"] == "moderate" for item in emerging)

    assert scenarios
    assert all(item["scenario_id"].startswith("ATLAS-DEMO-") for item in scenarios)
    assert all(item["origin"] == "synthetic_fixture" for item in scenarios)
    assert all(
        "not a prediction" in item["additional_information"].lower()
        for item in scenarios
    )
    assert all(
        item["financial_impact_band"]
        == derive_financial_impact_band(item["financial_impact_detail"])
        for item in scenarios
    )
    assert all(
        item["impact_band"]
        == derive_impact_band(
            item["likelihood"],
            item["financial_impact_detail"],
        )
        for item in scenarios
    )
    for item in scenarios:
        assert item["external_drivers"]
        assert item["affected_capabilities"]
        assert item["transmission_mechanism"]
        assert item["second_order_effects"]
        assert item["strengthening_signposts"]
        assert item["weakening_signposts"]
        assert item["defensive_options"]
        assert item["resilience_opportunities"]
        assert item["strategic_options"]
        assert item["option_expiry_signals"]
        assert item["challenged_assumptions"]
        assert item["discussion_prompts"]
        assert item["signpost_status"] == "not_evaluated"
        assert item["signpost_review_status"] == "not_reviewed"
        assert item["signpost_checked_evidence_refs"] == []
    assert manifest["provenance_class"] == "synthetic"
    assert manifest["review_status"] == "approved_synthetic"
    assert "fictional" in manifest["synthetic_notice"].lower()


def test_demo_seed_is_valid_and_idempotent(tmp_path):
    from contingency_atlas.db import list_evidence_events, list_scenarios
    from scripts.seed_demo import seed

    first = seed(tmp_path)
    second = seed(tmp_path)

    assert first["synthetic_only"] is True
    assert first["scenario_inserted"] == first["scenario_count"] == 6
    assert first["evidence_inserted"] == first["evidence_count"] == 8
    assert second["scenario_inserted"] == 0
    assert second["scenario_updated"] == 6
    assert second["evidence_inserted"] == 0

    scenarios = list_scenarios(str(tmp_path / "atlas.db"), status=None)
    evidence = list_evidence_events(str(tmp_path / "event_evidence.db"))
    assert len(scenarios) == 6
    assert all(item["origin"] == "synthetic_fixture" for item in scenarios)
    assert len(evidence) == 8
    assert all(item["source_type"] == "synthetic" for item in evidence)
    assert all(item["source_url"] is None for item in evidence)

    emerging = json.loads(
        (tmp_path / "emerging_risks.json").read_text(encoding="utf-8")
    )
    exported = json.loads(
        (tmp_path / "scenario_export.json").read_text(encoding="utf-8")
    )
    assert len(emerging) == 6
    assert len(exported) == 6
    assert all(item["external_drivers"] for item in emerging)
    assert all(item["discussion_prompts"] for item in exported)
    assert all(
        isinstance(item["external_drivers"], str)
        for item in scenarios
    )
    assert all(
        isinstance(item["discussion_prompts"], str)
        for item in scenarios
    )
