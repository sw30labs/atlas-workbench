"""Unified free-form intake graph: routing, provenance, QA, and duplicates."""

from __future__ import annotations

import json

from workbench_atlas.db import add_evidence_event, list_evidence_events
from workbench_atlas.intake import build_intake_graph, prepare_intake
from workbench_atlas.schema import migrate_evidence_events


URL = "https://public.example/report-1"


def _evidence(
    *,
    text: str = "A public report documents an operational disruption.",
    url: str = URL,
    unit_id: str = "unit-001",
) -> dict:
    return {
        "kind": "evidence",
        "title": "Operational disruption",
        "text": text,
        "source_urls": [url],
        "input_unit_ids": [unit_id],
        "classification_reason": "A realized operational event is described.",
        "record": {
            "source_publisher": "Public Authority",
            "source_title": "Operational disruption report",
            "source_date": "2026-08-20",
            "source_type": "regulatory_release",
            "instrument_status": "enforcement",
        },
    }


def _risk(
    *,
    title: str = "Shared Infrastructure Concentration",
    text: str = "Concentration in shared infrastructure may amplify future outages.",
    url: str = URL,
    unit_id: str = "unit-001",
) -> dict:
    return {
        "kind": "risk",
        "title": title,
        "text": text,
        "source_urls": [url],
        "input_unit_ids": [unit_id],
        "classification_reason": "The source describes a forward-looking mechanism.",
        "record": {
            "external_drivers": ["Technology"],
            "signal_maturity": "emerging",
            "momentum": "accelerating",
        },
    }


def _extract(*candidates, ignored=None):
    return lambda _request: {
        "candidates": list(candidates),
        "ignored": ignored or [],
    }


def _pass(_request):
    return {"verdict": "pass", "issues": [], "note": "coherent and attributed"}


def test_graph_has_conditional_qa_repair_cycle():
    mermaid = build_intake_graph().get_graph().draw_mermaid()
    assert "qa -.-> repair" in mermaid
    assert "qa -.-> finalize" in mermaid
    assert "repair --> qa" in mermaid


def test_empty_input_is_a_valid_zero_candidate_result():
    result = prepare_intake("")

    assert result.status == "ready"
    assert result.candidates == []
    assert result.holds == []
    assert result.accounting == []
    assert result.routes == ["normalize", "extract", "qa:1", "finalize"]


def test_fully_ignored_input_is_valid_and_every_unit_is_accounted_for():
    raw = "Greeting with no research content.\n\nAnother administrative note."
    result = prepare_intake(
        raw,
        extractor=_extract(
            ignored=[
                {"unit_id": "unit-001", "reason": "administrative"},
                {"unit_id": "unit-002", "reason": "administrative"},
            ]
        ),
        judge=_pass,
    )

    assert result.status == "ready"
    assert result.candidates == []
    assert [item.disposition for item in result.accounting] == ["ignored", "ignored"]
    assert {item.unit_id for item in result.accounting} == {"unit-001", "unit-002"}


def test_mixed_candidates_share_source_and_risk_links_to_evidence_payload():
    raw = f"Public authority report about an outage and a new dependency. {URL}"
    result = prepare_intake(
        raw,
        extractor=_extract(_evidence(), _risk()),
        judge=_pass,
    )

    assert result.status == "ready"
    assert {candidate.kind for candidate in result.candidates} == {"evidence", "risk"}
    evidence = next(c for c in result.candidates if c.kind == "evidence")
    risk = next(c for c in result.candidates if c.kind == "risk")
    assert evidence.eligible_for_review is True
    assert evidence.record["review_status"] == "pending"
    assert evidence.record["rights_review"] == "pending"
    assert evidence.record["event_id"].startswith("EVT-INTAKE-")
    assert risk.eligible_for_review is True
    assert risk.record["review_status"] == "pending"
    assert risk.linked_evidence_candidate_ids == [evidence.candidate_id]
    assert risk.record["evidence_refs"] == [evidence.record["event_id"]]
    assert risk.source_refs == [URL]
    assert result.accounting[0].candidate_ids == (
        evidence.candidate_id,
        risk.candidate_id,
    )
    assert all(candidate.draft_only for candidate in result.candidates)


def test_regulatory_release_clamps_instrument_status():
    result = prepare_intake(
        f"A public authority enforcement notice. {URL}",
        extractor=_extract(_evidence()),
        judge=_pass,
    )
    evidence = next(c for c in result.candidates if c.kind == "evidence")
    assert evidence.record["instrument_status"] == "enforcement"

    junk = _evidence()
    junk["record"]["instrument_status"] = "maybe-soon"
    result = prepare_intake(
        f"A public authority enforcement notice. {URL}",
        extractor=_extract(junk),
        judge=_pass,
    )
    evidence = next(c for c in result.candidates if c.kind == "evidence")
    assert evidence.record["instrument_status"] is None
    assert evidence.record["source_type"] == "regulatory_release"


def test_literal_url_admission_rejects_private_and_invented_sources():
    private = "http://127.0.0.1/internal"
    raw = f"One public link {URL} and one private link {private}."
    invented = "https://invented.example/not-in-input"
    result = prepare_intake(
        raw,
        extractor=_extract(_evidence(url=invented)),
        judge=_pass,
    )

    by_raw = {source.raw_url: source for source in result.sources}
    assert by_raw[URL].public_shape_valid is True
    assert by_raw[private].public_shape_valid is False
    candidate = result.candidates[0]
    assert candidate.qa_status == "hold"
    rendered = " ".join(candidate.validation_issues)
    assert "not present in raw_input" in rendered
    assert invented not in candidate.source_urls


def test_unit_urls_follow_canonical_identity_across_encodings():
    raw = (
        "First mention https://public.example/report{1}\n\n"
        "Second mention https://public.example/report%7B1%7D"
    )
    encoded = "https://public.example/report%7B1%7D"
    result = prepare_intake(
        raw,
        extractor=_extract(_evidence(url=encoded, unit_id="unit-002")),
        judge=_pass,
    )

    candidate = result.candidates[0]
    assert candidate.qa_status == "pass"
    assert candidate.source_urls == [encoded]
    assert result.input_units[1].source_urls == (encoded,)


def test_source_url_must_belong_to_the_candidates_cited_input_units():
    raw = (
        "A claim without a source in this unit.\n\n"
        f"A different sourced claim appears here. {URL}"
    )
    result = prepare_intake(
        raw,
        extractor=_extract(_evidence(unit_id="unit-001")),
        judge=_pass,
    )

    candidate = result.candidates[0]
    assert candidate.qa_status == "hold"
    assert any(
        "not present in its cited input unit" in issue
        for issue in candidate.validation_issues
    )


def test_uncovered_input_becomes_a_hold_instead_of_disappearing():
    raw = f"First source {URL}\n\nSecond source https://public.example/report-2"
    result = prepare_intake(
        raw,
        extractor=_extract(_evidence()),
        judge=_pass,
    )

    assert result.status == "ready_with_holds"
    assert [(hold.unit_id, hold.reason) for hold in result.holds] == [
        ("unit-002", "Extractor did not account for this input unit")
    ]
    assert [item.disposition for item in result.accounting] == ["candidate", "hold"]


def test_conditional_edge_repairs_then_rejudges_with_bounded_history():
    raw = f"A detailed public report. {URL}"

    def judge(request):
        if "too vague" in request["candidate"]["text"]:
            return {"verdict": "revise", "issues": ["make the mechanism explicit"]}
        return {"verdict": "pass", "issues": []}

    def repair(request):
        candidate = request["candidate"]
        assert request["input_units"][0]["unit_id"] == "unit-001"
        assert URL in request["input_units"][0]["text"]
        return {
            "kind": candidate["kind"],
            "title": candidate["title"],
            "text": "The report documents a payment outage caused by a shared dependency.",
            "source_urls": candidate["source_urls"],
            "input_unit_ids": candidate["input_unit_ids"],
        }

    result = prepare_intake(
        raw,
        extractor=_extract(_evidence(text="too vague")),
        judge=judge,
        repairer=repair,
        max_iterations=3,
    )

    candidate = result.candidates[0]
    assert candidate.qa_status == "pass"
    assert [round_.verdict for round_ in candidate.qa_history] == ["revise", "pass"]
    assert candidate.record["independently_authored_summary"].startswith(
        "The report documents"
    )
    assert result.routes == [
        "normalize", "extract", "qa:1", "repair", "qa:2", "finalize"
    ]


def test_repair_cannot_move_a_candidate_onto_a_different_input_unit():
    other = "https://public.example/report-2"
    raw = f"First public report. {URL}\n\nSecond public report. {other}"

    result = prepare_intake(
        raw,
        extractor=_extract(_evidence()),
        judge=lambda _request: {
            "verdict": "revise",
            "issues": ["tighten the wording"],
        },
        repairer=lambda request: {
            "kind": request["candidate"]["kind"],
            "title": request["candidate"]["title"],
            "text": "Repaired wording taken from the other unit.",
            "source_urls": [other],
            "input_unit_ids": ["unit-002"],
        },
        max_iterations=3,
    )

    candidate = result.candidates[0]
    assert "unit-002" not in candidate.input_unit_ids
    assert candidate.qa_status == "hold"


def test_ralph_repair_loop_stops_at_the_configured_budget():
    raw = f"A detailed public report. {URL}"
    result = prepare_intake(
        raw,
        extractor=_extract(_evidence()),
        judge=lambda _request: {"verdict": "revise", "issues": ["still weak"]},
        repairer=lambda request: request["candidate"],
        max_iterations=2,
    )

    candidate = result.candidates[0]
    assert candidate.qa_status == "hold"
    assert [round_.verdict for round_ in candidate.qa_history] == [
        "revise", "revise", "hold"
    ]
    assert "budget exhausted" in candidate.qa_history[-1].issues[0]
    assert result.routes.count("repair") == 1
    assert result.routes.count("qa:2") == 1


def test_no_judge_fails_closed_for_nonempty_candidates():
    result = prepare_intake(
        f"A detailed public report. {URL}",
        extractor=_extract(_evidence()),
    )

    assert result.candidates[0].qa_status == "hold"
    assert "not configured" in result.candidates[0].qa_history[-1].issues[0]


def test_exact_and_near_duplicates_are_preflighted_without_mutating_corpus(tmp_path):
    evidence_db = tmp_path / "evidence.db"
    risks_path = tmp_path / "risks.json"
    migrate_evidence_events(str(evidence_db))
    add_evidence_event(str(evidence_db), {
        "event_id": "EVT-EXISTING",
        "headline": "Existing disruption",
        "source_url": URL,
        "independently_authored_summary": "An existing disruption report.",
        "review_status": "pending",
    })
    risks_path.write_text(json.dumps([{
        "issue": "Shared Cloud Infrastructure Concentration",
        "description": "Shared cloud infrastructure concentration can amplify outages.",
        "source_type": "public_research",
        "provenance_note": "Existing reviewed hypothesis.",
        "evidence_refs": ["EVT-EXISTING"],
        "review_status": "approved",
    }]))
    before_risks = risks_path.read_text()
    before_events = list_evidence_events(str(evidence_db))

    result = prepare_intake(
        f"A detailed public report. {URL}",
        extractor=_extract(
            _evidence(),
            _risk(
                title="Shared Cloud Infrastructure Concentration Exposure",
                text="Shared cloud infrastructure concentration may amplify outages.",
            ),
        ),
        judge=_pass,
        evidence_db_path=evidence_db,
        emerging_risks_path=risks_path,
        near_duplicate_threshold=0.55,
    )

    evidence = next(c for c in result.candidates if c.kind == "evidence")
    risk = next(c for c in result.candidates if c.kind == "risk")
    assert evidence.qa_status == "hold"
    assert evidence.duplicate_matches[0].match_type == "exact"
    assert evidence.duplicate_matches[0].record_id == "EVT-EXISTING"
    assert risk.qa_status == "pass"
    assert any(match.match_type == "near" for match in risk.duplicate_matches)
    assert "near duplicate" in risk.warnings[0]
    assert risks_path.read_text() == before_risks
    assert list_evidence_events(str(evidence_db)) == before_events


def test_risk_can_cite_existing_corpus_evidence_without_a_new_event(tmp_path):
    evidence_db = tmp_path / "evidence.db"
    migrate_evidence_events(str(evidence_db))
    add_evidence_event(str(evidence_db), {
        "event_id": "EVT-EXISTING",
        "headline": "Existing disruption",
        "source_url": URL,
        "independently_authored_summary": "An existing disruption report.",
        "review_status": "pending",
    })

    result = prepare_intake(
        f"A new concentration hypothesis. {URL}",
        extractor=_extract(_risk()),
        judge=_pass,
        evidence_db_path=evidence_db,
    )

    candidate = result.candidates[0]
    assert candidate.kind == "risk"
    assert candidate.qa_status == "pass"
    assert candidate.eligible_for_review is True


def test_rejected_records_are_not_part_of_duplicate_corpus(tmp_path):
    evidence_db = tmp_path / "evidence.db"
    migrate_evidence_events(str(evidence_db))
    add_evidence_event(str(evidence_db), {
        "event_id": "EVT-REJECTED",
        "headline": "Rejected disruption",
        "source_url": URL,
        "review_status": "rejected",
    })

    result = prepare_intake(
        f"A detailed public report. {URL}",
        extractor=_extract(_evidence()),
        judge=_pass,
        evidence_db_path=evidence_db,
    )

    assert result.candidates[0].qa_status == "pass"
    assert result.candidates[0].duplicate_matches == []
