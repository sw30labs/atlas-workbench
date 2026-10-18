"""Tests for Phase 6: Maintenance & Governance."""

import pytest


class TestMaintenanceHeuristics:
    def test_find_stale_drafts(self):
        from contingency_atlas.phases.phase6_maintain import find_stale_scenarios
        scenarios = [
            {"scenario_id": "D1", "status": "draft", "judge_score": None, "created_year": 2026},
            {"scenario_id": "A1", "status": "active", "judge_score": 0.9, "created_year": 2026, "last_scored": "2026-01-01"},
            {"scenario_id": "R1", "status": "retired", "created_year": 2020},
        ]
        stale = find_stale_scenarios(scenarios)
        ids = [s["scenario_id"] for s in stale]
        assert "D1" in ids
        assert "R1" not in ids

    def test_find_duplicates(self):
        from contingency_atlas.phases.phase6_maintain import find_duplicate_candidates
        scenarios = [
            {
                "scenario_id": "FIXTURE-OPERATOR",
                "origin": "operator_authored",
                "title": "Shared Identity Concentration Failure Cascade",
                "status": "active",
            },
            {
                "scenario_id": "FIXTURE-GENERATED",
                "origin": "pipeline_generated",
                "title": "Shared Identity Concentration Failure Across Services",
                "status": "active",
                "urgency_score": 99,
            },
            {
                "scenario_id": "FIXTURE-UNRELATED",
                "origin": "pipeline_generated",
                "title": "Climate Port Flood Risk",
                "status": "active",
            },
        ]
        pairs = find_duplicate_candidates(scenarios, threshold=0.5)
        identity_pairs = [
            pair
            for pair in pairs
            if "FIXTURE-OPERATOR" in pair[:2]
            and "FIXTURE-GENERATED" in pair[:2]
        ]
        assert identity_pairs
        assert identity_pairs[0][0] == "FIXTURE-OPERATOR"
        assert identity_pairs[0][2] >= 0.5
        assert not any("FIXTURE-UNRELATED" in pair[:2] for pair in pairs)

    def test_merge_scenario(self, empty_db):
        from contingency_atlas.db import upsert_scenario, get_scenario
        from contingency_atlas.phases.phase6_maintain import merge_scenario

        upsert_scenario(empty_db, {
            "scenario_id": "KEEP-1",
            "title": "Cloud Identity Concentration Failure",
            "description": "Longer survivor description with detail",
            "risk_category": "Digital resilience",
            "status": "active",
        })
        upsert_scenario(empty_db, {
            "scenario_id": "DUP-1",
            "title": "Cloud Identity Concentration Failure Cascade",
            "description": "Short duplicate",
            "risk_category": "Digital resilience",
            "status": "active",
        })
        ok = merge_scenario(empty_db, "DUP-1", "KEEP-1", similarity=0.7)
        assert ok is True
        assert get_scenario(empty_db, "DUP-1")["status"] == "retired"
        assert get_scenario(empty_db, "KEEP-1")["status"] == "active"
        assert "DUP-1" in (get_scenario(empty_db, "KEEP-1").get("additional_information") or "")


class TestMaintenanceCycle:
    def test_run_without_llm(self, empty_db):
        from contingency_atlas.db import upsert_scenario
        from contingency_atlas.phases.phase6_maintain import run_maintenance_cycle

        upsert_scenario(empty_db, {
            "scenario_id": "MAINT-001",
            "title": "Draft Scenario Without Score",
            "risk_category": "Digital resilience",
            "status": "draft",
            "created_year": 2026,
        })
        result = run_maintenance_cycle(
            empty_db, client=None, apply=True, use_llm=False
        )
        assert result.flagged_stale >= 1
        assert isinstance(result.actions, list)

    def test_retire_via_llm_mock(self, mock_omlox_client, empty_db):
        from contingency_atlas.db import upsert_scenario, get_scenario
        from contingency_atlas.phases.phase6_maintain import run_maintenance_cycle

        upsert_scenario(empty_db, {
            "scenario_id": "MAINT-RETIRE",
            "title": "Old Faded Risk",
            "risk_category": "Digital resilience",
            "status": "active",
            "created_year": 2020,
        })
        mock_omlox_client.chat_json = lambda *a, **k: {
            "recommendations": [
                {
                    "scenario_id": "MAINT-RETIRE",
                    "action": "retire",
                    "reason": "Risk has faded",
                }
            ]
        }
        # Without AUTO_RETIRE, recommend but do not apply
        result_safe = run_maintenance_cycle(
            empty_db, client=mock_omlox_client, apply=True, use_llm=True,
            apply_retire=False,
        )
        assert result_safe.retired == 0
        got = get_scenario(empty_db, "MAINT-RETIRE")
        assert got is not None
        assert got["status"] == "active"

        # With apply_retire=True, retirement is applied
        result = run_maintenance_cycle(
            empty_db, client=mock_omlox_client, apply=True, use_llm=True,
            apply_retire=True,
        )
        assert result.retired >= 1
        got = get_scenario(empty_db, "MAINT-RETIRE")
        assert got is not None
        assert got["status"] == "retired"
