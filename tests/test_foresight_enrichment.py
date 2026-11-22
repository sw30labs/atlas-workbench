"""Tests for the external-driver, transmission-path, and signpost layers."""

import json
from datetime import datetime, timezone

import pytest


def test_project_native_radar_normalization():
    from workbench_atlas.categories import (
        derive_evidence_strength,
        normalize_external_drivers,
        normalize_momentum,
        normalize_signal_maturity,
    )

    assert normalize_external_drivers(
        ["Technological change", "Demographics", "untrusted label"]
    ) == ["Technology", "Demographic change"]
    assert normalize_external_drivers(
        [],
        fallback_text="Climate-driven flood and regulatory change",
    ) == ["Institutional change", "Environment"]
    assert normalize_external_drivers(
        [],
        fallback_text="An unclassified legacy signal",
    ) == []
    assert normalize_signal_maturity("ACCELERATING") == "accelerating"
    assert normalize_signal_maturity("invented") == "emerging"
    assert normalize_momentum("", legacy_trend="rising") == "accelerating"
    assert derive_evidence_strength([]) == "unrated"
    assert derive_evidence_strength(["one"]) == "limited"
    assert derive_evidence_strength(["one", "two"]) == "moderate"
    assert derive_evidence_strength(["one", "two", "three"]) == "strong"
    assert derive_evidence_strength(
        ["EVENT-1", "https://example.com/event-1"]
    ) == "limited"


def test_legacy_risk_records_gain_compatible_radar_fields(tmp_path):
    from workbench_atlas.phases.phase2_risks import load_emerging_risks

    path = tmp_path / "risks.json"
    path.write_text(json.dumps([{
        "issue": "Climate workforce pressure",
        "description": "Climate disruption coincides with a skills shortage.",
        "trend": "rising",
        "first_seen": "2026-01-02",
        "evidence_refs": ["https://example.com/risk"],
        "review_status": "approved",
    }]))

    risk = load_emerging_risks(path)[0]
    assert risk["trend"] == "rising"
    assert risk["momentum"] == "accelerating"
    assert risk["signal_maturity"] == "emerging"
    assert risk["first_observed"] == "2026-01-02"
    assert risk["external_drivers"] == [
        "Demographic change",
        "Environment",
    ]
    assert risk["evidence_strength"] == "limited"


def test_identified_risk_has_derived_evidence_strength(mock_omlox_client):
    from workbench_atlas.phases.phase2_risks import identify_new_risks

    mock_omlox_client.chat_json = lambda *args, **kwargs: {
        "new_risks": [{
            "issue": "Synthetic quantum transition",
            "description": "Quantum technology changes a control dependency.",
            "trend": "stable",
            "external_drivers": ["Technology", "not-a-driver"],
            "signal_maturity": "accelerating",
            "momentum": "accelerating",
            "evidence_strength": "strong",
        }]
    }
    risks = identify_new_risks(
        "Cited public research packet.",
        [],
        mock_omlox_client,
        [
            "https://example.com/research/one",
            "https://example.com/research/two",
        ],
    )

    assert len(risks) == 1
    risk = risks[0]
    assert risk.external_drivers == ["Technology"]
    assert risk.signal_maturity == "accelerating"
    assert risk.momentum == "accelerating"
    assert risk.trend == "rising"
    # The model-provided value is ignored; coverage comes from cited refs.
    assert risk.evidence_strength == "moderate"
    assert risk.first_observed
    assert risk.last_confirmed
    assert risk.review_status == "pending"


def test_scenario_conversion_persists_full_transmission_path():
    from workbench_atlas.phases.phase3_generate import convert_to_db_row

    scenario = {
        "core_risks": ["Cloud concentration", "Workforce skills shortage"],
        "external_drivers": ["Technology", "Demographic change"],
        "signal_maturity": "accelerating",
        "momentum": "accelerating",
        "scenario_event": {
            "name": "Recovery expertise bottleneck",
            "description": "A shared technology disruption outpaces recovery expertise.",
            "scenario_horizon": "2027-2029",
            "detailed_rationale": ["Two approved signals could converge."],
            "affected_capabilities": ["Identity recovery", "Payment operations"],
            "transmission_mechanism": "A shared outage exhausts scarce specialist capacity.",
            "second_order_effects": ["Manual workarounds increase reconciliation pressure."],
            "cross_industry_analogues": ["Specialist recovery capacity in public infrastructure."],
            "impacts": {
                "likelihood": "Moderate",
                "financial_impact": "$500M",
            },
        },
        "signposts": {
            "strengthening": ["Support windows shorten across several suppliers."],
            "weakening": ["Comparable migrations reduce specialist dependence."],
        },
        "conclusion": {
            "preparation_strategy": ["Exercise recovery without the specialist team."],
            "defensive_options": ["Cross-train recovery operators."],
            "resilience_opportunities": ["Replace a fragile manual recovery step."],
            "strategic_options": ["Reserve external recovery capacity."],
            "option_expiry_signals": ["Supplier support falls below twelve months."],
            "challenged_assumptions": ["Specialists will be available during a sector event."],
            "discussion_prompts": ["Which recovery step has no substitute operator?"],
        },
    }
    row = convert_to_db_row(
        scenario,
        "ATLAS-RADAR-001",
        evidence_refs=["E1", "E2", "E3"],
    )

    assert json.loads(row["external_drivers"]) == [
        "Technology",
        "Demographic change",
    ]
    assert row["signal_maturity"] == "accelerating"
    assert row["momentum"] == "accelerating"
    assert row["evidence_strength"] == "strong"
    assert json.loads(row["affected_capabilities"]) == [
        "Identity recovery",
        "Payment operations",
    ]
    assert row["transmission_mechanism"].startswith("A shared outage")
    assert json.loads(row["second_order_effects"])
    assert json.loads(row["cross_industry_analogues"])
    assert json.loads(row["strengthening_signposts"])
    assert json.loads(row["weakening_signposts"])
    assert json.loads(row["defensive_options"])
    assert json.loads(row["resilience_opportunities"])
    assert json.loads(row["strategic_options"])
    assert json.loads(row["option_expiry_signals"])
    assert json.loads(row["challenged_assumptions"])
    assert json.loads(row["discussion_prompts"])
    assert row["signpost_status"] == "not_evaluated"
    assert row["signpost_review_status"] == "not_reviewed"


def test_structured_signposts_increase_foresight_value():
    from workbench_atlas.phases.phase4_prioritize import (
        _infer_foresight_value,
    )

    baseline = {
        "detailed_rationale": json.dumps(["One rationale"]),
        "element_of_surprise": "Uncertain timing",
    }
    structured = {
        **baseline,
        "external_drivers": json.dumps(["Technology", "Institutional change"]),
        "transmission_mechanism": "A policy deadline concentrates migrations.",
        "second_order_effects": json.dumps(["Recovery queues lengthen."]),
        "strengthening_signposts": json.dumps(["Migration failures increase."]),
        "weakening_signposts": json.dumps(["Comparable migrations succeed."]),
    }

    assert _infer_foresight_value(structured) > _infer_foresight_value(baseline)


def test_signpost_cycle_uses_only_approved_evidence(
    empty_db,
    mock_omlox_client,
    tmp_path,
):
    from workbench_atlas.db import add_evidence_event, get_scenario, upsert_scenario
    from workbench_atlas.phases.phase6_maintain import run_maintenance_cycle
    from workbench_atlas.schema import migrate_evidence_events

    evidence_db = str(tmp_path / "evidence.db")
    migrate_evidence_events(evidence_db)
    common = {
        "source_date": "2026-07-28",
        "source_publisher": "Synthetic Public Register",
        "retrieved_at": "2026-07-29T10:00:00+00:00",
        "record_type": "event",
        "provenance_class": "synthetic",
        "rights_review": "not_applicable_synthetic",
        "reviewed_at": "2026-07-29",
        "source_type": "synthetic",
    }
    add_evidence_event(evidence_db, {
        **common,
        "event_id": "APPROVED-1",
        "headline": "Approved specialist support notice",
        "source_title": "Approved specialist support notice",
        "source_url": "https://example.com/approved",
        "independently_authored_summary": "A support window was shortened.",
        "review_status": "approved_synthetic",
    })
    add_evidence_event(evidence_db, {
        **common,
        "event_id": "PENDING-1",
        "headline": "PENDING RECORD MUST NOT REACH THE MODEL",
        "source_title": "PENDING RECORD MUST NOT REACH THE MODEL",
        "source_url": "https://example.com/pending",
        "independently_authored_summary": "Unreviewed text.",
        "review_status": "pending",
    })
    upsert_scenario(empty_db, {
        "scenario_id": "SIGNPOST-1",
        "title": "Specialist recovery bottleneck",
        "description": "A scenario hypothesis.",
        "created_year": datetime.now(timezone.utc).year,
        "last_scored": "2026-07-29T00:00:00+00:00",
        "status": "active",
        "core_risks": json.dumps(["Skills shortage"]),
        "transmission_mechanism": "Support contraction strains recovery.",
        "strengthening_signposts": json.dumps(["Support windows shorten."]),
        "weakening_signposts": json.dumps(["Support windows expand."]),
    })

    prompts = []

    def answer(messages, **kwargs):
        prompt = messages[0]["content"]
        prompts.append(prompt)
        if "New approved evidence:" in prompt:
            return {
                "classification": "strengthened",
                "rationale": "The approved notice matches a strengthening signpost.",
                "evidence_refs": [
                    "APPROVED-1",
                    "PENDING-1",
                    "https://attacker.invalid/not-in-packet",
                ],
            }
        return {"recommendations": []}

    mock_omlox_client.chat_json = answer
    result = run_maintenance_cycle(
        empty_db,
        mock_omlox_client,
        apply=True,
        use_llm=True,
        evidence_db_path=evidence_db,
        apply_retire=False,
    )

    assert result.signposts_evaluated == 1
    assert result.signpost_evaluations[0].review_status == "pending"
    stored = get_scenario(empty_db, "SIGNPOST-1")
    assert stored["status"] == "active"
    assert stored["signpost_status"] == "strengthened"
    assert stored["signpost_review_status"] == "pending"
    assert json.loads(stored["signpost_evidence_refs"]) == ["APPROVED-1"]
    assert set(json.loads(stored["signpost_checked_evidence_refs"])) == {
        "APPROVED-1",
        "https://example.com/approved",
    }
    signpost_prompt = next(p for p in prompts if "New approved evidence:" in p)
    assert "Approved specialist support notice" in signpost_prompt
    assert "PENDING RECORD MUST NOT REACH THE MODEL" not in signpost_prompt

    repeated = run_maintenance_cycle(
        empty_db,
        mock_omlox_client,
        apply=True,
        use_llm=True,
        evidence_db_path=evidence_db,
        apply_retire=False,
    )
    assert repeated.signposts_evaluated == 0


def test_signpost_history_preserves_multiple_review_cycles(
    empty_db,
    mock_omlox_client,
    tmp_path,
):
    from workbench_atlas.db import (
        add_evidence_event,
        get_scenario,
        list_signpost_assessments,
        update_evidence_event_fields,
        upsert_scenario,
    )
    from workbench_atlas.phases.phase6_maintain import run_maintenance_cycle
    from workbench_atlas.review import approve_signpost_assessment
    from workbench_atlas.schema import migrate_evidence_events

    evidence_db = str(tmp_path / "history-evidence.db")
    migrate_evidence_events(evidence_db)
    for event_id, review_status in (
        ("HISTORY-1", "approved_synthetic"),
        ("HISTORY-2", "pending"),
    ):
        add_evidence_event(evidence_db, {
            "event_id": event_id,
            "headline": event_id,
            "source_title": event_id,
            "source_url": f"https://example.com/{event_id.lower()}",
            "source_date": "2026-07-28",
            "retrieved_at": "2026-07-28T00:00:00+00:00",
            "source_type": "synthetic",
            "record_type": "event",
            "provenance_class": "synthetic",
            "independently_authored_summary": f"Summary for {event_id}.",
            "rights_review": "not_applicable_synthetic",
            "review_status": review_status,
            "reviewed_at": "2026-07-28" if review_status != "pending" else None,
        })
    upsert_scenario(empty_db, {
        "scenario_id": "SIGNPOST-HISTORY",
        "title": "History-preserving scenario",
        "description": "A scenario hypothesis.",
        "created_year": datetime.now(timezone.utc).year,
        "last_scored": "2026-07-28T00:00:00+00:00",
        "status": "active",
        "strengthening_signposts": json.dumps(["Signal rises."]),
        "weakening_signposts": json.dumps(["Signal falls."]),
    })

    response = {
        "classification": "strengthened",
        "rationale": "First rationale.",
        "evidence_refs": ["HISTORY-1"],
    }

    def answer(messages, **kwargs):
        if "New approved evidence:" in messages[0]["content"]:
            return dict(response)
        return {"recommendations": []}

    mock_omlox_client.chat_json = answer
    first = run_maintenance_cycle(
        empty_db,
        mock_omlox_client,
        evidence_db_path=evidence_db,
        use_llm=True,
        apply=True,
        apply_retire=False,
    )
    assert first.signposts_evaluated == 1
    approve_signpost_assessment(
        empty_db,
        "SIGNPOST-HISTORY",
        evidence_db=evidence_db,
        note="First assessment reviewed.",
    )

    update_evidence_event_fields(evidence_db, "HISTORY-2", {
        "review_status": "approved_synthetic",
        "reviewed_at": "2026-07-29T12:00:00+00:00",
    })
    response.update({
        "classification": "weakened",
        "rationale": "Second rationale.",
        "evidence_refs": ["HISTORY-2"],
    })
    second = run_maintenance_cycle(
        empty_db,
        mock_omlox_client,
        evidence_db_path=evidence_db,
        use_llm=True,
        apply=True,
        apply_retire=False,
    )
    assert second.signposts_evaluated == 1

    history = list_signpost_assessments(
        empty_db,
        review_status=None,
    )
    assert [item["classification"] for item in history] == [
        "weakened",
        "strengthened",
    ]
    assert [item["rationale"] for item in history] == [
        "Second rationale.",
        "First rationale.",
    ]
    assert history[0]["review_status"] == "pending"
    assert history[1]["review_status"] == "approved"
    assert json.loads(history[0]["evidence_refs"]) == ["HISTORY-2"]
    assert json.loads(history[1]["evidence_refs"]) == ["HISTORY-1"]

    projection = get_scenario(empty_db, "SIGNPOST-HISTORY")
    assert projection["signpost_assessment"] == "Second rationale."
    assert json.loads(projection["signpost_evidence_refs"]) == ["HISTORY-2"]
    assert set(json.loads(projection["signpost_checked_evidence_refs"])) == {
        "HISTORY-1",
        "https://example.com/history-1",
        "HISTORY-2",
        "https://example.com/history-2",
    }


def test_signpost_evaluation_excludes_draft_and_retired_scenarios(
    empty_db,
    mock_omlox_client,
    tmp_path,
):
    from workbench_atlas.db import (
        add_evidence_event,
        get_scenario,
        list_signpost_assessments,
        upsert_scenario,
    )
    from workbench_atlas.phases.phase6_maintain import run_maintenance_cycle
    from workbench_atlas.schema import migrate_evidence_events

    evidence_db = str(tmp_path / "active-only-evidence.db")
    migrate_evidence_events(evidence_db)
    add_evidence_event(evidence_db, {
        "event_id": "ACTIVE-EVIDENCE",
        "headline": "Approved active-scenario evidence",
        "source_title": "Approved active-scenario evidence",
        "source_url": "https://example.com/active-evidence",
        "source_date": "2026-07-28",
        "retrieved_at": "2026-07-28T00:00:00+00:00",
        "source_type": "synthetic",
        "record_type": "event",
        "provenance_class": "synthetic",
        "independently_authored_summary": "An approved synthetic summary.",
        "rights_review": "not_applicable_synthetic",
        "review_status": "approved_synthetic",
        "reviewed_at": "2026-07-28",
    })
    for scenario_id, status in (
        ("SIGNPOST-ACTIVE", "active"),
        ("SIGNPOST-LEGACY", None),
        ("SIGNPOST-DRAFT", "draft"),
        ("SIGNPOST-RETIRED", "retired"),
    ):
        row = {
            "scenario_id": scenario_id,
            "title": scenario_id,
            "description": "A scenario hypothesis.",
            "created_year": datetime.now(timezone.utc).year,
            "last_scored": "2026-07-28T00:00:00+00:00",
            "strengthening_signposts": json.dumps(["Signal rises."]),
            "weakening_signposts": json.dumps(["Signal falls."]),
        }
        if status is not None:
            row["status"] = status
        upsert_scenario(empty_db, row)

    def answer(messages, **kwargs):
        if "New approved evidence:" in messages[0]["content"]:
            return {
                "classification": "strengthened",
                "rationale": "Approved evidence matches.",
                "evidence_refs": ["ACTIVE-EVIDENCE"],
            }
        return {"recommendations": []}

    mock_omlox_client.chat_json = answer
    result = run_maintenance_cycle(
        empty_db,
        mock_omlox_client,
        evidence_db_path=evidence_db,
        use_llm=True,
        apply=True,
        apply_retire=False,
    )
    assert {
        item.scenario_id for item in result.signpost_evaluations
    } == {"SIGNPOST-ACTIVE", "SIGNPOST-LEGACY"}
    assert {
        item["scenario_id"]
        for item in list_signpost_assessments(
            empty_db,
            review_status=None,
        )
    } == {"SIGNPOST-ACTIVE", "SIGNPOST-LEGACY"}
    assert get_scenario(
        empty_db,
        "SIGNPOST-DRAFT",
    )["signpost_review_status"] is None
    assert get_scenario(
        empty_db,
        "SIGNPOST-RETIRED",
    )["signpost_review_status"] is None


def test_same_cycle_retirement_supersedes_pending_signpost_review(
    empty_db,
    mock_omlox_client,
    tmp_path,
):
    from workbench_atlas.db import (
        add_evidence_event,
        get_scenario,
        list_signpost_assessments,
        upsert_scenario,
    )
    from workbench_atlas.phases.phase6_maintain import run_maintenance_cycle
    from workbench_atlas.review import (
        ReviewError,
        approve_signpost_assessment,
        list_signpost_assessments_for_review,
    )
    from workbench_atlas.schema import migrate_evidence_events

    evidence_db = str(tmp_path / "retirement-evidence.db")
    migrate_evidence_events(evidence_db)
    add_evidence_event(evidence_db, {
        "event_id": "RETIRE-EVIDENCE",
        "source_url": "https://example.com/retire-evidence",
        "review_status": "approved_synthetic",
    })
    upsert_scenario(empty_db, {
        "scenario_id": "SIGNPOST-RETIRE-SAME-CYCLE",
        "title": "Scenario retired after assessment",
        "description": "A scenario hypothesis.",
        "created_year": datetime.now(timezone.utc).year,
        "last_scored": "2026-07-28T00:00:00+00:00",
        "status": "active",
        "strengthening_signposts": json.dumps(["Signal rises."]),
        "weakening_signposts": json.dumps(["Signal falls."]),
    })

    def answer(messages, **kwargs):
        if "New approved evidence:" in messages[0]["content"]:
            return {
                "classification": "strengthened",
                "rationale": "Approved evidence matches.",
                "evidence_refs": ["RETIRE-EVIDENCE"],
            }
        return {
            "recommendations": [{
                "scenario_id": "SIGNPOST-RETIRE-SAME-CYCLE",
                "action": "retire",
                "reason": "The premise has ended.",
            }]
        }

    mock_omlox_client.chat_json = answer
    result = run_maintenance_cycle(
        empty_db,
        mock_omlox_client,
        evidence_db_path=evidence_db,
        use_llm=True,
        apply=True,
        apply_retire=True,
    )

    assert result.signposts_evaluated == 1
    assert result.retired == 1
    scenario = get_scenario(empty_db, "SIGNPOST-RETIRE-SAME-CYCLE")
    assert scenario["status"] == "retired"
    assert scenario["signpost_review_status"] == "superseded"
    history = list_signpost_assessments(
        empty_db,
        review_status=None,
    )
    assert history[0]["review_status"] == "superseded"
    assert list_signpost_assessments_for_review(empty_db) == []
    with pytest.raises(ReviewError, match="no pending"):
        approve_signpost_assessment(
            empty_db,
            "SIGNPOST-RETIRE-SAME-CYCLE",
            evidence_db=evidence_db,
        )
