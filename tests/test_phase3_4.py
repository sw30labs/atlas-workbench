"""Tests for Phase 3 (Scenario Generation) and Phase 4 (Prioritization)."""

import json
from pathlib import Path

import pytest


# ── Phase 3: Generation tests ──

class TestPhase3Generation:
    def test_convert_to_db_row(self):
        from contingency_atlas.phases.phase3_generate import convert_to_db_row
        scenario = {
            "core_risks": ["Cloud Identity Risk", "AI-Enabled Phishing"],
            "scenario_event": {
                "name": "Cloud Identity Concentration Failure",
                "description": "A shared cloud identity provider outage cascading across hypothetical peer banks.",
                "scenario_horizon": "2026-2028",
                "detailed_rationale": [
                    "Cited public reports show identity incidents rising",
                    "Cloud concentration is increasing",
                ],
                "impacts": {
                    "element_of_surprise": "Speed of cascade",
                    "likelihood": "Moderate to High",
                    "financial_impact": "Estimated $10-30B in direct losses",
                    "operational_impact": "Payment disruptions",
                    "reputational_impact": "Trust loss",
                    "regulatory_impact": "DORA mandates",
                },
            },
            "conclusion": {
                "summary": "Cloud identity concentration is a systemic risk",
                "preparation_strategy": ["Stress-test failover", "Reduce concentration"],
                "additional_information": "Based on independently reviewed public sources",
            },
            "theme_tags": ["cyber", "ai", "third_party"],
        }
        row = convert_to_db_row(scenario, "ATLAS-TEST-001")
        assert row["scenario_id"] == "ATLAS-TEST-001"
        assert row["title"] == "Cloud Identity Concentration Failure"
        assert row["risk_category"] == "Digital resilience"
        assert row["financial_impact_band"] == "Extreme"
        assert row["event_driver_class"] == "Technology disruption"
        assert row["primary_cause_class"] == "Access control"
        assert row["status"] == "draft"
        # JSON columns
        assert json.loads(row["core_risks"]) == ["Cloud Identity Risk", "AI-Enabled Phishing"]
        assert json.loads(row["preparation_strategy"]) == ["Stress-test failover", "Reduce concentration"]
        assert json.loads(row["detailed_rationale"]) == [
            "Cited public reports show identity incidents rising",
            "Cloud concentration is increasing",
        ]
        assert json.loads(row["theme_tags"]) == ["cyber", "ai", "third_party"]
        assert json.loads(row["evidence_refs"]) == []

    def test_derive_category(self):
        from contingency_atlas.categories import derive_category
        assert (
            derive_category({"core_risks": ["Cyber Risk"], "theme_tags": ["cyber"]})
            == "Digital resilience"
        )
        assert (
            derive_category(
                {"core_risks": ["Climate Change"], "theme_tags": ["climate"]}
            )
            == "Physical resilience"
        )
        assert (
            derive_category({"core_risks": ["Sanctions"], "theme_tags": ["sanctions"]})
            == "Financial crime controls"
        )
        assert derive_category({"core_risks": ["Unknown"]}) == "Multi-risk convergence"

    def test_derive_financial_impact_band(self):
        from contingency_atlas.categories import derive_financial_impact_band
        assert derive_financial_impact_band("$10-30B in losses") == "Extreme"
        assert derive_financial_impact_band("$1.5B estimated") == "Major"
        assert derive_financial_impact_band("Material impact") == "Major"
        assert derive_financial_impact_band("Low impact") == "Limited"

    def test_generate_scenario(self, mock_omlox_client):
        from contingency_atlas.phases.phase3_generate import generate_scenario
        # Mock returns a valid scenario
        mock_omlox_client.chat_json = lambda *a, **k: {
            "core_risks": ["Cyber", "Cloud"],
            "scenario_event": {
                "name": "Cloud Outage Cascade",
                "description": "Cloud provider outage cascades across banks",
                "scenario_horizon": "2026-2028",
                "detailed_rationale": ["Reason 1"],
                "impacts": {"likelihood": "Moderate", "financial_impact": "$10B"},
            },
            "conclusion": {"summary": "Risk", "preparation_strategy": ["Action 1"]},
            "theme_tags": ["cyber"],
        }
        result = generate_scenario(
            mock_omlox_client,
            emerging_risks=[{"issue": "Cyber Risk", "description": "Cyber threat"}],
            loss_patterns="Test pattern",
        )
        assert result is not None
        assert "scenario_event" in result
        assert "core_risks" in result

    def test_run_generation_cycle(self, mock_omlox_client, tmp_workspace):
        from contingency_atlas.phases.phase3_generate import run_generation_cycle
        from contingency_atlas import loops as loops_module
        # Mock: generation returns valid scenario
        mock_omlox_client.chat_json = lambda *a, **k: {
            "core_risks": ["Cyber", "Cloud"],
            "scenario_event": {
                "name": "Cloud Outage Cascade",
                "description": "Cloud provider outage cascades across hypothetical banks, supported by cited public evidence.",
                "scenario_horizon": "2026-2028",
                "detailed_rationale": ["Reason 1", "Reason 2"],
                "impacts": {"likelihood": "Moderate", "financial_impact": "$10B", "operational_impact": "Disruptions"},
            },
            "conclusion": {"summary": "Risk", "preparation_strategy": ["Action 1", "Action 2"]},
            "theme_tags": ["cyber"],
        }
        # The ralph_loop uses loops.judge_scenario (imported at module load)
        # Mock the judge to return passing score
        original = loops_module.judge_scenario
        loops_module.judge_scenario = lambda *a, **k: type("R", (), {
            "score": 0.85, "feedback": "Good", "criteria": {}, "passed": True,
            "iteration": 1, "error": False,
        })()
        try:
            result = run_generation_cycle(
                mock_omlox_client,
                db_path=str(tmp_workspace / "atlas.db"),
                emerging_risks_path=str(tmp_workspace / "emerging_risks.json"),
                evidence_db_path=str(tmp_workspace / "event_evidence.db"),
                num_scenarios=2,
                use_ralph_loop=True,
            )
            assert result.inserted >= 1
            from contingency_atlas.db import list_scenarios

            stored = list_scenarios(
                str(tmp_workspace / "atlas.db"), status=None
            )
            assert any(json.loads(item["evidence_refs"]) for item in stored)
        finally:
            loops_module.judge_scenario = original


# ── Phase 4: Prioritization tests ──

class TestPhase4Prioritization:
    def test_numeric_likelihood(self):
        from contingency_atlas.phases.phase4_prioritize import _numeric_likelihood
        assert _numeric_likelihood("Very High") == 1.0
        assert _numeric_likelihood("Moderate") == 0.5
        assert _numeric_likelihood("Low to Moderate") == 0.3
        assert _numeric_likelihood("") == 0.5

    def test_numeric_financial(self):
        from contingency_atlas.phases.phase4_prioritize import _numeric_financial
        assert _numeric_financial("Extreme") == 1.0
        assert _numeric_financial("Major") == 0.8
        assert _numeric_financial("Significant") == 0.6
        assert _numeric_financial("") == 0.45

    def test_infer_time_horizon(self):
        from contingency_atlas.phases.phase4_prioritize import _infer_time_horizon
        from datetime import datetime
        current = datetime.now().year
        assert _infer_time_horizon(f"{current+1}-{current+2}") in ("1y", "2y")
        assert _infer_time_horizon(f"{current+6}-") == "5y+"
        assert _infer_time_horizon("") == ""

    def test_infer_controllability(self):
        from contingency_atlas.phases.phase4_prioritize import _infer_controllability
        ext = {
            "risk_category": "Geopolitical",
            "description": "Sanctions and supply-chain disruption affect banks",
        }
        assert _infer_controllability(ext) == "external"
        internal = {
            "risk_category": "Process",
            "description": "Control failure in a hypothetical trading process",
        }
        assert _infer_controllability(internal) == "internal"

    def test_compute_urgency(self):
        from contingency_atlas.phases.phase4_prioritize import compute_urgency
        scenario = {
            "scenario_id": "TEST-001",
            "likelihood": "High",
            "financial_impact_band": "Major",
            "scenario_horizon": "2026-2027",
            "element_of_surprise": "Speed of cascade would surprise",
            "detailed_rationale": ["Reason 1", "Reason 2", "Reason 3"],
            "risk_category": "Geopolitical",
            "description": "Sanctions cascade affecting hypothetical banks",
        }
        result = compute_urgency(scenario)
        assert result.urgency_score > 0
        assert result.urgency_score <= 100
        assert 0 <= result.foresight_value <= 1
        assert result.time_horizon in ("1y", "2y", "6m")
        assert result.controllability in ("external", "internal", "mixed")

    def test_run_prioritization_cycle(self, mock_omlox_client, tmp_workspace):
        from contingency_atlas.phases.phase4_prioritize import run_prioritization_cycle
        # First insert some scenarios to score
        from contingency_atlas.phases.phase3_generate import convert_to_db_row, upsert_scenario
        scenarios = [
            {
                "core_risks": ["Cyber", "Cloud"],
                "scenario_event": {
                    "name": "Cloud Outage",
                    "description": "Cloud outage cascade",
                    "scenario_horizon": "2026-2027",
                    "detailed_rationale": ["first reason", "second reason"],
                    "impacts": {"likelihood": "High", "financial_impact": "$10B"},
                },
                "conclusion": {"summary": "Risk", "preparation_strategy": ["A1"]},
                "theme_tags": ["cyber"],
            }
        ]
        for i, s in enumerate(scenarios):
            row = convert_to_db_row(s, f"ATLAS-TEST-{i:03d}")
            row["status"] = "active"
            upsert_scenario(str(tmp_workspace / "atlas.db"), row)

        result = run_prioritization_cycle(
            str(tmp_workspace / "atlas.db"),
            mock_omlox_client,
        )
        assert result.updated >= 1

    def test_get_prioritized_portfolio(self, mock_omlox_client, tmp_workspace):
        from contingency_atlas.phases.phase4_prioritize import (
            run_prioritization_cycle, get_prioritized_portfolio
        )
        from contingency_atlas.phases.phase3_generate import convert_to_db_row, upsert_scenario
        scenarios = [
            {
                "core_risks": ["Cyber"],
                "scenario_event": {
                    "name": f"Scenario {i}",
                    "description": f"Scenario {i} description",
                    "scenario_horizon": "2026-2027",
                    "detailed_rationale": ["R1"],
                    "impacts": {"likelihood": "High", "financial_impact": "$10B"},
                },
                "conclusion": {"summary": "Risk", "preparation_strategy": ["A1"]},
                "theme_tags": ["cyber"],
            }
            for i in range(5)
        ]
        for i, s in enumerate(scenarios):
            row = convert_to_db_row(s, f"ATLAS-TEST-{i:03d}")
            row["status"] = "active"
            upsert_scenario(str(tmp_workspace / "atlas.db"), row)

        run_prioritization_cycle(str(tmp_workspace / "atlas.db"), mock_omlox_client)
        portfolio = get_prioritized_portfolio(str(tmp_workspace / "atlas.db"), top_n=3)
        assert len(portfolio) <= 3
        # Should be sorted by urgency (desc)
        urgencies = [float(s["urgency_score"]) for s in portfolio if s.get("urgency_score")]
        assert urgencies == sorted(urgencies, reverse=True)
