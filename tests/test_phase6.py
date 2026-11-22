"""Tests for Phase 6: Maintenance & Governance."""

import pytest


class TestMaintenanceHeuristics:
    def test_find_stale_drafts(self):
        from workbench_atlas.phases.phase6_maintain import find_stale_scenarios
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
        from workbench_atlas.phases.phase6_maintain import find_duplicate_candidates
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
        from workbench_atlas.db import upsert_scenario, get_scenario
        from workbench_atlas.phases.phase6_maintain import merge_scenario

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
        from workbench_atlas.db import upsert_scenario
        from workbench_atlas.phases.phase6_maintain import run_maintenance_cycle

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
        from workbench_atlas.db import upsert_scenario, get_scenario
        from workbench_atlas.phases.phase6_maintain import run_maintenance_cycle

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

    def test_refresh_does_not_publish_or_rewrite_status(self, empty_db, mock_omlox_client, monkeypatch):
        from workbench_atlas.db import get_scenario, upsert_scenario
        from workbench_atlas.phases import phase6_maintain

        def refuse_write(*args, **kwargs):
            raise AssertionError(f"refresh wrote scenario fields: {args[1:]} {kwargs}")

        monkeypatch.setattr(phase6_maintain, "update_scenario_fields", refuse_write)
        rows = [
            ("DRAFT-OLD", "draft", 0.91, 2020),
            ("DRAFT-NEW", "draft", 0.92, 2026),
            ("ACTIVE-1", "active", 0.93, 2026),
            ("RETIRED-1", "retired", 0.94, 2020),
        ]
        for sid, status, score, year in rows:
            upsert_scenario(empty_db, {
                "scenario_id": sid,
                "title": f"Unique title {sid}",
                "risk_category": "Digital resilience",
                "status": status,
                "judge_score": score,
                "created_year": year,
            })
        mock_omlox_client.chat_json = lambda *a, **k: {
            "recommendations": [
                {"scenario_id": sid, "action": "refresh", "reason": "Model asked for a refresh"}
                for sid, *_rest in rows
            ]
        }
        result = phase6_maintain.run_maintenance_cycle(
            empty_db, client=mock_omlox_client, apply=True, use_llm=True, apply_retire=False,
        )
        for sid, status, *_rest in rows:
            assert get_scenario(empty_db, sid)["status"] == status
        by_id: dict[str, list] = {}
        for action in result.actions:
            if action.action == "refresh":
                by_id.setdefault(action.scenario_id, []).append(action)
                assert action.applied is False
        assert by_id["DRAFT-OLD"]
        assert all("does not publish a draft" in action.reason for action in by_id["DRAFT-OLD"])
        assert by_id["DRAFT-NEW"]
        assert all("does not publish a draft" in action.reason for action in by_id["DRAFT-NEW"])
        assert by_id["ACTIVE-1"]
        assert all("does not publish" not in action.reason for action in by_id["ACTIVE-1"])
        assert by_id["RETIRED-1"]
        assert all("not reactivated" in action.reason for action in by_id["RETIRED-1"])


def test_demo_phrases_do_not_change_duplicate_pairs():
    """A shared demo phrase used to add 0.15 and could cross 0.50.

    These titles share three tokens out of seven (Jaccard about 0.43).
    The old boost would have reported a pair. Token overlap alone must not.
    """
    from pathlib import Path

    from workbench_atlas.phases.phase6_maintain import (
        _title_tokens,
        find_duplicate_candidates,
    )

    left = {
        "scenario_id": "LOW-A",
        "title": "alpha beta gamma extra",
        "status": "active",
        "description": "cloud identity stablecoin depeg deepfake kyc",
    }
    right = {
        "scenario_id": "LOW-B",
        "title": "alpha beta gamma other zebra quartz",
        "status": "active",
        "description": "cloud identity stablecoin depeg deepfake kyc",
    }
    left_tokens = _title_tokens(left["title"])
    right_tokens = _title_tokens(right["title"])
    overlap = len(left_tokens & right_tokens) / len(left_tokens | right_tokens)
    assert overlap < 0.50
    assert overlap + 0.15 >= 0.50
    assert find_duplicate_candidates([left, right]) == []

    close = [
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
        },
    ]
    plain = find_duplicate_candidates(close)
    phrased = find_duplicate_candidates([
        {**row, "description": "cloud identity stablecoin depeg deepfake kyc"}
        for row in close
    ])
    assert [(a, b) for a, b, _score in phrased] == [(a, b) for a, b, _score in plain]

    root = Path(__file__).resolve().parents[1] / "workbench_atlas"
    banned = ("cloud identity", "stablecoin depeg", "deepfake kyc")
    hits = [
        f"{path.relative_to(root)}:{phrase}"
        for path in root.rglob("*.py")
        for phrase in banned
        if phrase in path.read_text(encoding="utf-8").lower()
    ]
    assert hits == []
