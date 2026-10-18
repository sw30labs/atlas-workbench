"""Deterministic post-approval scenario coverage recommendations."""

from __future__ import annotations

import json

from contingency_atlas.coverage import recommend_scenario_coverage


def _evidence(event_id: str, *, status: str = "approved", category: str = "Digital resilience"):
    return {
        "event_id": event_id,
        "source_url": f"https://public.example/{event_id}",
        "review_status": status,
        "risk_category": category,
    }


def _risk(
    risk_id: str,
    event_id: str,
    *,
    status: str = "approved",
    category: str = "Digital resilience",
    issue: str | None = None,
    description: str = "A distinct operational mechanism.",
):
    return {
        "risk_id": risk_id,
        "issue": issue or risk_id.replace("-", " ").title(),
        "description": description,
        "review_status": status,
        "status": "active",
        "risk_category": category,
        "evidence_refs": [event_id],
    }


def _scenario(
    scenario_id: str,
    core_risks,
    *,
    title: str | None = None,
    status: str = "active",
    category: str = "Digital resilience",
):
    return {
        "scenario_id": scenario_id,
        "title": title or f"Scenario {scenario_id}",
        "status": status,
        "risk_category": category,
        "core_risks": core_risks,
    }


def test_pending_intake_never_changes_recommendation():
    result = recommend_scenario_coverage(
        [
            _evidence("EVT-PENDING", status="pending"),
            _evidence("EVT-CHANGED", status="needs_update"),
        ],
        [
            _risk("novel-risk", "EVT-PENDING", status="pending"),
            _risk("changed-risk", "EVT-CHANGED", status="needs_update"),
        ],
        [],
    )

    assert result.recommended == 0
    assert result.eligible_risk_count == 0
    assert result.ignored_unapproved_evidence_count == 2
    assert result.ignored_unapproved_risk_count == 2
    assert "novel-risk" not in result.uncovered_risk_ids


def test_approved_risk_must_resolve_to_approved_evidence():
    result = recommend_scenario_coverage(
        [_evidence("EVT-1", status="pending")],
        [_risk("cloud-control", "EVT-1")],
        [],
    )

    assert result.recommended == 0
    assert result.approved_risk_count == 1
    assert result.eligible_risk_count == 0
    assert result.excluded_unresolved_risk_ids == ("cloud-control",)


def test_every_risk_reference_must_resolve_without_using_pending_evidence():
    risk = _risk("mixed-provenance", "EVT-APPROVED")
    risk["evidence_refs"].append("EVT-PENDING")

    result = recommend_scenario_coverage(
        [
            _evidence("EVT-APPROVED"),
            _evidence("EVT-PENDING", status="pending"),
        ],
        [risk],
        [],
    )

    assert result.recommended == 0
    assert result.eligible_risk_count == 0
    assert result.excluded_unresolved_risk_ids == ("mixed-provenance",)


def test_recommendation_range_is_explainable_and_capped():
    evidence = [_evidence(f"EVT-{index}") for index in range(1, 8)]
    risks = [
        _risk(f"risk-{index}", f"EVT-{index}", category=f"Category {index}")
        for index in range(1, 8)
    ]

    result = recommend_scenario_coverage(evidence, risks, [], max_scenarios=3)

    assert (result.range_min, result.recommended, result.range_max) == (3, 3, 3)
    assert result.cap == 3
    assert len(result.uncovered_risk_ids) == 7
    assert len(result.uncovered_categories) == 7
    assert "three, two, or one" in " ".join(result.rationale)


def test_active_scenario_covers_risk_by_json_core_risk_label():
    evidence = [_evidence("EVT-1")]
    risks = [
        _risk(
            "cloud-concentration",
            "EVT-1",
            issue="Coordinated Cloud Concentration Risk",
        )
    ]
    scenarios = [
        _scenario(
            "ATLAS-1",
            json.dumps(["Cloud concentration"]),
            category="Digital resilience",
        )
    ]

    result = recommend_scenario_coverage(evidence, risks, scenarios)

    assert result.recommended == 0
    assert result.covered_risk_ids == ("cloud-concentration",)
    assert result.uncovered_risk_ids == ()


def test_inactive_scenario_does_not_cover_an_approved_risk():
    result = recommend_scenario_coverage(
        [_evidence("EVT-1")],
        [_risk("cloud-control", "EVT-1")],
        [_scenario("ATLAS-DRAFT", ["Cloud Control"], status="draft")],
    )

    assert result.active_scenario_count == 0
    assert result.recommended == 1
    assert result.uncovered_risk_ids == ("cloud-control",)


def test_legacy_scenario_without_status_is_treated_as_active():
    scenario = _scenario("ATLAS-LEGACY", ["Cloud Control"])
    scenario.pop("status")

    result = recommend_scenario_coverage(
        [_evidence("EVT-1")],
        [_risk("cloud-control", "EVT-1")],
        [scenario],
    )

    assert result.active_scenario_count == 1
    assert result.recommended == 0
    assert result.covered_risk_ids == ("cloud-control",)


def test_related_active_scenario_is_an_explicit_coverage_link():
    risk = _risk("reference-data", "EVT-1")
    risk["related_scenarios"] = ["ATLAS-LINKED"]

    result = recommend_scenario_coverage(
        [_evidence("EVT-1")],
        [risk],
        [_scenario("ATLAS-LINKED", ["Different wording entirely"])],
    )

    assert result.recommended == 0
    assert result.covered_risk_ids == ("reference-data",)


def test_duplicate_candidates_narrow_only_the_upper_bound():
    evidence = [_evidence(f"EVT-{index}") for index in range(1, 5)]
    risks = [
        _risk(
            "cloud-identity-a",
            "EVT-1",
            issue="Cloud identity concentration",
            description="Shared identity control plane concentration.",
        ),
        _risk(
            "cloud-identity-b",
            "EVT-2",
            issue="Cloud identity concentration risk",
            description="Shared identity control plane concentration risk.",
        ),
        _risk("payment-chain", "EVT-3"),
        _risk("model-drift", "EVT-4"),
    ]

    result = recommend_scenario_coverage(evidence, risks, [])

    assert result.recommended == 2
    assert result.range_min == 2
    assert result.range_max == 3  # four singleton slots minus duplicate pressure
    assert result.duplicate_risk_candidates == (
        ("cloud-identity-a", "cloud-identity-b"),
    )
    assert "human review decisions" in " ".join(result.rationale)


def test_duplicate_active_scenarios_are_reported_without_mutation():
    scenarios = [
        _scenario(
            "ATLAS-1",
            ["Already Covered"],
            title="Shared cloud outage",
        ),
        _scenario(
            "ATLAS-2",
            ["Already Covered"],
            title="Shared cloud outage",
        ),
    ]
    result = recommend_scenario_coverage(
        [_evidence("EVT-1"), _evidence("EVT-2"), _evidence("EVT-3")],
        [
            _risk("new-one", "EVT-1"),
            _risk("new-two", "EVT-2"),
            _risk("new-three", "EVT-3"),
        ],
        scenarios,
    )

    assert result.recommended == 2
    assert result.range_max == 2
    assert result.duplicate_scenario_candidates == (("ATLAS-1", "ATLAS-2"),)


def test_repeat_api_rows_with_the_same_persisted_id_do_not_inflate_counts():
    evidence = _evidence("EVT-1")
    risk = _risk("one-risk", "EVT-1")
    scenario = _scenario("ATLAS-1", ["Unrelated"])

    result = recommend_scenario_coverage(
        [evidence, dict(evidence)],
        [risk, dict(risk)],
        [scenario, dict(scenario)],
    )

    assert result.approved_evidence_count == 1
    assert result.approved_risk_count == 1
    assert result.active_scenario_count == 1
    assert result.recommended == 1


def test_output_is_stable_and_json_serializable():
    evidence = [_evidence("EVT-B"), _evidence("EVT-A")]
    risks = [_risk("risk-b", "EVT-B"), _risk("risk-a", "EVT-A")]

    first = recommend_scenario_coverage(evidence, risks, []).to_dict()
    second = recommend_scenario_coverage(
        list(reversed(evidence)), list(reversed(risks)), []
    ).to_dict()

    assert first == second
    assert json.loads(json.dumps(first))["range"] == {"min": 1, "max": 2}
    assert first["recommended"] == 1


def test_zero_cap_is_allowed_and_never_exceeds_public_limit():
    evidence = [_evidence("EVT-1")]
    risks = [_risk("new-risk", "EVT-1")]

    blocked = recommend_scenario_coverage(
        evidence, risks, [], max_scenarios=0
    )
    clamped = recommend_scenario_coverage(
        evidence * 12,
        [
            _risk(f"risk-{index}", "EVT-1", issue=f"Distinct mechanism {index}")
            for index in range(24)
        ],
        [],
        max_scenarios=99,
    )

    assert (blocked.recommended, blocked.range_min, blocked.range_max) == (0, 0, 0)
    assert blocked.cap == 0
    assert clamped.cap == 10
    assert clamped.recommended == 10
    assert clamped.range_max == 10
