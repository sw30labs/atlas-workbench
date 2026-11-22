"""Focused tests for the manual evidence and emerging-risk review workflow."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from workbench_atlas.db import (
    add_evidence_event,
    get_latest_signpost_assessment,
    get_evidence_event,
    get_scenario,
    list_signpost_assessments,
    record_signpost_assessment,
    update_evidence_event_fields,
    update_scenario_fields,
    upsert_scenario,
)
from workbench_atlas.review import (
    ReviewError,
    approve_evidence,
    approve_risk,
    approve_signpost_assessment,
    list_evidence_for_review,
    list_risks_for_review,
    list_signpost_assessments_for_review,
    reject_evidence,
    reject_risk,
    reject_signpost_assessment,
)
from workbench_atlas.schema import migrate, migrate_evidence_events


ROOT = Path(__file__).resolve().parents[1]


def _event(event_id: str, **overrides: object) -> dict[str, object]:
    record: dict[str, object] = {
        "event_id": event_id,
        "headline": "Public enforcement notice",
        "organization": "Example Institution",
        "country": "United States",
        "region": "North America",
        "loss_amount_usd": 1_000_000,
        "source_date": "2026-01-02",
        "risk_category": "Processing integrity",
        "event_driver_class": "Process execution",
        "primary_cause_class": "Process design",
        "source_url": f"https://www.sec.gov/example/{event_id}",
        "source_publisher": "U.S. Securities and Exchange Commission",
        "source_title": "Example public enforcement notice",
        "retrieved_at": "2026-01-03T12:00:00+00:00",
        "source_type": "regulatory_release",
        "instrument_status": "enforcement",
        "record_type": "event",
        "provenance_class": "public_primary",
        "source_locator": "p. 4, §2",
        "independently_authored_summary": "An original factual summary.",
        "research_inference": None,
        "rights_review": "pending",
        "content_fingerprint": "a" * 64,
        "review_status": "pending",
        "reviewed_at": None,
        "taxonomy_version": "atlas-1.0",
        "provenance_note": "Compiled from the cited public source.",
    }
    record.update(overrides)
    return record


def _evidence_db(tmp_path: Path) -> Path:
    path = tmp_path / "event_evidence.db"
    migrate_evidence_events(str(path))
    return path


def _scenario_db(tmp_path: Path) -> Path:
    path = tmp_path / "atlas.db"
    migrate(str(path))
    return path


def _write_risks(path: Path, *records: dict[str, object]) -> None:
    path.write_text(json.dumps(list(records)), encoding="utf-8")


def _risk(issue: str, **overrides: object) -> dict[str, object]:
    record: dict[str, object] = {
        "issue": issue,
        "description": "A public-source-grounded research hypothesis.",
        "status": "monitoring",
        "trend": "rising",
        "evidence_refs": ["https://www.cisa.gov/example/advisory"],
        "source_type": "public_research",
        "provenance_note": "Derived from the cited public evidence.",
        "review_status": "pending",
    }
    record.update(overrides)
    return record


def test_get_and_update_evidence_event_fields_are_allowlisted(tmp_path):
    db_path = _evidence_db(tmp_path)
    assert add_evidence_event(str(db_path), _event("EVT-001"))

    assert get_evidence_event(str(db_path), "EVT-001")["headline"] == (
        "Public enforcement notice"
    )
    assert update_evidence_event_fields(
        str(db_path),
        "EVT-001",
        {"headline": "Reviewed public enforcement notice"},
    )
    assert get_evidence_event(str(db_path), "EVT-001")["headline"].startswith("Reviewed")
    assert not update_evidence_event_fields(str(db_path), "MISSING", {"headline": "No row"})
    with pytest.raises(ValueError, match="Unknown evidence-event field"):
        update_evidence_event_fields(str(db_path), "EVT-001", {"unknown": "value"})
    with pytest.raises(ValueError, match="immutable"):
        update_evidence_event_fields(str(db_path), "EVT-001", {"event_id": "EVT-002"})


def test_evidence_approval_requires_both_attestations(tmp_path):
    db_path = _evidence_db(tmp_path)
    add_evidence_event(str(db_path), _event("EVT-APPROVE"))

    with pytest.raises(ReviewError, match="public-source.*rights"):
        approve_evidence(str(db_path), "EVT-APPROVE")
    with pytest.raises(ReviewError, match="rights attestation"):
        approve_evidence(
            str(db_path),
            "EVT-APPROVE",
            public_source_attested=True,
        )

    approved = approve_evidence(
        str(db_path),
        "EVT-APPROVE",
        public_source_attested=True,
        rights_attested=True,
        note="Verified against the issuing authority.",
    )
    assert approved["review_status"] == "approved"
    assert approved["rights_review"] == "attested_for_public_use"
    assert approved["reviewed_at"].endswith("+00:00")
    assert "Verified against" in approved["provenance_note"]
    assert list_evidence_for_review(db_path, review_status="pending") == []
    assert [item["event_id"] for item in list_evidence_for_review(
        db_path, review_status="approved"
    )] == ["EVT-APPROVE"]


def test_evidence_locator_allows_document_text_but_rejects_private_url(tmp_path):
    db_path = _evidence_db(tmp_path)
    add_evidence_event(str(db_path), _event("EVT-PAGE", source_locator="p. 4, §2"))
    approved = approve_evidence(
        db_path,
        "EVT-PAGE",
        public_source_attested=True,
        rights_attested=True,
    )
    assert approved["review_status"] == "approved"

    add_evidence_event(
        str(db_path),
        _event("EVT-PRIVATE", source_locator="http://127.0.0.1/private"),
    )
    with pytest.raises(ReviewError, match="source_locator"):
        approve_evidence(
            db_path,
            "EVT-PRIVATE",
            public_source_attested=True,
            rights_attested=True,
        )
    assert get_evidence_event(str(db_path), "EVT-PRIVATE")["review_status"] == "pending"


def test_regulatory_release_requires_instrument_status(tmp_path):
    from workbench_atlas.review import (
        evidence_validation_report,
        validate_evidence_for_approval,
    )

    db_path = _evidence_db(tmp_path)
    add_evidence_event(
        str(db_path),
        _event("EVT-NO-STATUS", instrument_status=None),
    )
    with pytest.raises(ReviewError, match="instrument_status"):
        approve_evidence(
            db_path,
            "EVT-NO-STATUS",
            public_source_attested=True,
            rights_attested=True,
        )
    missing = _event("EVT-NO-STATUS", instrument_status=None)
    with pytest.raises(ReviewError, match="instrument_status"):
        validate_evidence_for_approval(missing)
    report = evidence_validation_report(missing)
    assert report["approvable"] is False
    assert "Instrument status on regulatory_release" in report["blockers"]

    news = _event(
        "EVT-NEWS",
        source_type="public_reporting",
        provenance_class="public_secondary",
        instrument_status=None,
    )
    validate_evidence_for_approval(news)
    assert evidence_validation_report(news)["approvable"] is True


def test_invalid_public_metadata_is_not_approved(tmp_path):
    db_path = _evidence_db(tmp_path)
    add_evidence_event(
        str(db_path),
        _event("EVT-BAD", source_url="http://localhost/private"),
    )
    with pytest.raises(ReviewError, match="source_url"):
        approve_evidence(
            db_path,
            "EVT-BAD",
            public_source_attested=True,
            rights_attested=True,
        )
    assert get_evidence_event(str(db_path), "EVT-BAD")["review_status"] == "pending"


def test_rejection_is_explicit_and_synthetic_approval_is_immutable(tmp_path):
    db_path = _evidence_db(tmp_path)
    add_evidence_event(str(db_path), _event("EVT-REJECT"))
    rejected = reject_evidence(
        db_path,
        "EVT-REJECT",
        reason="The source title does not match the cited page.",
    )
    assert rejected["review_status"] == "rejected"
    assert "does not match" in rejected["provenance_note"]

    synthetic = _event(
        "SYN-EVT-001",
        source_type="synthetic",
        provenance_class="synthetic",
        rights_review="not_applicable_synthetic",
        review_status="approved_synthetic",
    )
    add_evidence_event(str(db_path), synthetic)
    with pytest.raises(ReviewError, match="immutable"):
        reject_evidence(db_path, "SYN-EVT-001", reason="Do not change fixtures")
    with pytest.raises(ReviewError, match="immutable"):
        approve_evidence(
            db_path,
            "SYN-EVT-001",
            public_source_attested=True,
            rights_attested=True,
        )
    assert get_evidence_event(str(db_path), "SYN-EVT-001")["review_status"] == (
        "approved_synthetic"
    )


def test_risk_approval_resolves_urls_and_ids_to_approved_evidence(tmp_path):
    db_path = _evidence_db(tmp_path)
    approved_event = _event("EVT-APPROVED")
    add_evidence_event(str(db_path), approved_event)
    approve_evidence(
        db_path,
        "EVT-APPROVED",
        public_source_attested=True,
        rights_attested=True,
    )
    risks_path = tmp_path / "risks.json"
    _write_risks(
        risks_path,
        _risk(
            "URL grounded risk",
            evidence_refs=[approved_event["source_url"]],
        ),
        _risk("Event grounded risk", evidence_refs=["EVT-APPROVED"]),
    )

    url_risk = approve_risk(
        risks_path,
        "URL grounded risk",
        evidence_db=db_path,
        public_source_attested=True,
        rights_attested=True,
    )
    assert url_risk["review_status"] == "approved"
    assert url_risk["evidence_refs"] == ["EVT-APPROVED"]

    event_risk = approve_risk(
        risks_path,
        "Event grounded risk",
        evidence_db=db_path,
        public_source_attested=True,
        rights_attested=True,
    )
    assert event_risk["review_status"] == "approved"
    assert event_risk["evidence_refs"] == ["EVT-APPROVED"]
    assert len(list_risks_for_review(risks_path, review_status="approved")) == 2


def test_review_queue_includes_needs_update_risks(tmp_path):
    """needs_update means a decided record changed and is waiting on a human,
    and phases 3/5 exclude it until one acts. If the queue only listed pending
    the record would be stranded: invisible to review, yet excluded downstream.
    """
    risks_path = tmp_path / "risks.json"
    _write_risks(
        risks_path,
        _risk("Pending risk"),
        dict(_risk("Changed after review"), review_status="needs_update"),
        dict(_risk("Settled risk"), review_status="approved"),
    )

    queued = list_risks_for_review(
        risks_path, review_status=("pending", "needs_update")
    )
    assert sorted(r["issue"] for r in queued) == [
        "Changed after review",
        "Pending risk",
    ]
    # A single status still filters exactly, and the default is unchanged.
    assert [r["issue"] for r in list_risks_for_review(risks_path)] == ["Pending risk"]
    assert [
        r["issue"]
        for r in list_risks_for_review(risks_path, review_status="needs_update")
    ] == ["Changed after review"]


def test_risk_approval_rejects_unresolved_ids_and_preserves_synthetic(tmp_path):
    db_path = _evidence_db(tmp_path)
    add_evidence_event(
        str(db_path),
        _event(
            "SYN-EVT-001",
            source_type="synthetic",
            provenance_class="synthetic",
            rights_review="not_applicable_synthetic",
            review_status="approved_synthetic",
        ),
    )
    risks_path = tmp_path / "risks.json"
    _write_risks(
        risks_path,
        _risk("Unresolved risk", evidence_refs=["MISSING-EVENT"]),
        _risk(
            "Bare URL risk",
            evidence_refs=["https://www.cisa.gov/example/unreviewed"],
        ),
        _risk("Synthetic evidence risk", evidence_refs=["SYN-EVT-001"]),
        _risk(
            "Synthetic fixture risk",
            source_type="synthetic",
            evidence_refs=["SYN-EVT-001"],
            review_status="approved_synthetic",
        ),
    )

    with pytest.raises(ReviewError, match="approved evidence"):
        approve_risk(
            risks_path,
            "Unresolved risk",
            evidence_db=db_path,
            public_source_attested=True,
            rights_attested=True,
        )
    with pytest.raises(ReviewError, match="approved evidence"):
        approve_risk(
            risks_path,
            "Bare URL risk",
            evidence_db=db_path,
            public_source_attested=True,
            rights_attested=True,
        )
    with pytest.raises(ReviewError, match="approved evidence"):
        approve_risk(
            risks_path,
            "Synthetic evidence risk",
            evidence_db=db_path,
            public_source_attested=True,
            rights_attested=True,
        )
    with pytest.raises(ReviewError, match="immutable"):
        reject_risk(
            risks_path,
            "Synthetic fixture risk",
            reason="Do not change fixtures",
        )
    with pytest.raises(ReviewError, match="immutable"):
        approve_risk(
            risks_path,
            "Synthetic fixture risk",
            evidence_db=db_path,
            public_source_attested=True,
            rights_attested=True,
        )
    records = json.loads(risks_path.read_text(encoding="utf-8"))
    synthetic = next(r for r in records if r["issue"] == "Synthetic fixture risk")
    assert synthetic["review_status"] == "approved_synthetic"


def test_risk_rejection_and_documented_cli_commands(tmp_path):
    db_path = _evidence_db(tmp_path)
    scenario_db = _scenario_db(tmp_path)
    add_evidence_event(str(db_path), _event("EVT-CLI"))
    upsert_scenario(
        str(scenario_db),
        {
            "scenario_id": "ATLAS-REVIEW-001",
            "title": "Pending signpost interpretation",
            "risk_category": "Digital resilience",
            "status": "active",
        },
    )
    record_signpost_assessment(
        str(scenario_db),
        "ATLAS-REVIEW-001",
        classification="strengthened",
        rationale="Approved evidence matches a signpost.",
        evidence_refs=["EVT-CLI"],
        considered_refs=[
            "EVT-CLI",
            "https://www.sec.gov/example/EVT-CLI",
        ],
        evaluated_at="2026-01-04T00:00:00+00:00",
    )
    risks_path = tmp_path / "risks.json"
    _write_risks(
        risks_path,
        _risk(
            "CLI risk",
            evidence_refs=["https://www.sec.gov/example/EVT-CLI"],
        ),
        _risk("Rejected risk"),
    )

    rejected = reject_risk(
        risks_path,
        "Rejected risk",
        reason="Evidence does not support the stated scope.",
    )
    assert rejected["review_status"] == "rejected"

    base = [
        sys.executable,
        str(ROOT / "scripts" / "review_records.py"),
        "--evidence-db",
        str(db_path),
        "--risks-file",
        str(risks_path),
        "--scenario-db",
        str(scenario_db),
    ]
    help_result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "review_records.py"), "--help"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "approve-event" in help_result.stdout
    assert "reject-risk" in help_result.stdout
    assert "approve-signpost" in help_result.stdout

    listed = subprocess.run(
        [*base, "list"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(listed.stdout)
    assert [record["event_id"] for record in payload["events"]] == ["EVT-CLI"]
    assert [record["issue"] for record in payload["risks"]] == ["CLI risk"]
    assert [record["scenario_id"] for record in payload["signposts"]] == [
        "ATLAS-REVIEW-001"
    ]

    approved = subprocess.run(
        [
            *base,
            "approve-event",
            "EVT-CLI",
            "--confirm-public-source",
            "--confirm-rights",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(approved.stdout)["review_status"] == "approved"

    approved_risk = subprocess.run(
        [
            *base,
            "approve-risk",
            "CLI risk",
            "--confirm-public-source",
            "--confirm-rights",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    approved_risk_payload = json.loads(approved_risk.stdout)
    assert approved_risk_payload["review_status"] == "approved"
    assert approved_risk_payload["evidence_refs"] == ["EVT-CLI"]

    approved_signpost = subprocess.run(
        [
            *base,
            "approve-signpost",
            "ATLAS-REVIEW-001",
            "--note",
            "Interpretation checked against the cited notice.",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    approved_signpost_payload = json.loads(approved_signpost.stdout)
    assert approved_signpost_payload["signpost_review_status"] == "approved"


def test_signpost_review_is_explicit_and_does_not_change_lifecycle(tmp_path):
    db_path = _scenario_db(tmp_path)
    evidence_db = _evidence_db(tmp_path)
    for suffix in ("APPROVE", "REJECT"):
        event_id = f"EVT-SIGNPOST-{suffix}"
        add_evidence_event(str(evidence_db), _event(event_id))
        approve_evidence(
            evidence_db,
            event_id,
            public_source_attested=True,
            rights_attested=True,
        )
    for scenario_id in ("ATLAS-SIGNPOST-APPROVE", "ATLAS-SIGNPOST-REJECT"):
        upsert_scenario(
            str(db_path),
            {
                "scenario_id": scenario_id,
                "title": "Pending signpost interpretation",
                "risk_category": "Digital resilience",
                "status": "active",
            },
        )
        event_id = scenario_id.replace("ATLAS-", "EVT-")
        record_signpost_assessment(
            str(db_path),
            scenario_id,
            classification="weakened",
            rationale="New evidence matches a weakening signpost.",
            evidence_refs=[event_id],
            considered_refs=[
                event_id,
                f"https://www.sec.gov/example/{event_id}",
            ],
            evaluated_at="2026-01-04T00:00:00+00:00",
        )

    assert {
        record["scenario_id"]
        for record in list_signpost_assessments_for_review(db_path)
    } == {"ATLAS-SIGNPOST-APPROVE", "ATLAS-SIGNPOST-REJECT"}

    approved = approve_signpost_assessment(
        db_path,
        "ATLAS-SIGNPOST-APPROVE",
        evidence_db=evidence_db,
        note="Compared with the underlying evidence.",
    )
    assert approved["signpost_review_status"] == "approved"
    assert approved["signpost_status"] == "weakened"
    assert approved["status"] == "active"
    assert "underlying evidence" in approved["additional_information"]
    approved_history = get_latest_signpost_assessment(
        str(db_path),
        "ATLAS-SIGNPOST-APPROVE",
    )
    assert approved_history["review_status"] == "approved"
    assert approved_history["reviewed_at"]
    assert "underlying evidence" in approved_history["reviewer_note"]

    with pytest.raises(ReviewError, match="requires a reason"):
        reject_signpost_assessment(
            db_path,
            "ATLAS-SIGNPOST-REJECT",
            reason="",
        )
    rejected = reject_signpost_assessment(
        db_path,
        "ATLAS-SIGNPOST-REJECT",
        reason="The evidence does not match the named signpost.",
    )
    assert rejected["signpost_review_status"] == "rejected"
    assert rejected["signpost_status"] == "weakened"
    assert rejected["status"] == "active"
    assert "does not match" in rejected["additional_information"]
    rejected_history = get_latest_signpost_assessment(
        str(db_path),
        "ATLAS-SIGNPOST-REJECT",
    )
    assert rejected_history["review_status"] == "rejected"
    assert "does not match" in rejected_history["reviewer_note"]

    with pytest.raises(ReviewError, match="no pending"):
        approve_signpost_assessment(
            db_path,
            "ATLAS-SIGNPOST-APPROVE",
            evidence_db=evidence_db,
        )
    assert get_scenario(
        str(db_path),
        "ATLAS-SIGNPOST-APPROVE",
    )["status"] == "active"


def test_orphaned_history_is_retained_but_not_listed_for_review(tmp_path):
    db_path = _scenario_db(tmp_path)
    upsert_scenario(str(db_path), {
        "scenario_id": "ATLAS-ORPHANED-HISTORY",
        "title": "Orphan queue boundary",
        "status": "active",
    })
    record_signpost_assessment(
        str(db_path),
        "ATLAS-ORPHANED-HISTORY",
        classification="unchanged",
        rationale="A pending interpretation.",
        evidence_refs=[],
        considered_refs=["E1"],
        evaluated_at="2026-01-04T00:00:00+00:00",
    )
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "DELETE FROM scenarios WHERE scenario_id = ?",
            ("ATLAS-ORPHANED-HISTORY",),
        )

    assert list_signpost_assessments_for_review(db_path) == []
    retained = list_signpost_assessments(
        str(db_path),
        review_status=None,
    )
    assert [row["scenario_id"] for row in retained] == [
        "ATLAS-ORPHANED-HISTORY"
    ]


def test_pending_queue_normalizes_legacy_active_status(tmp_path):
    db_path = _scenario_db(tmp_path)
    upsert_scenario(str(db_path), {
        "scenario_id": "ATLAS-LEGACY-ACTIVE",
        "title": "Whitespace-normalized lifecycle",
        "status": " Active ",
    })
    record_signpost_assessment(
        str(db_path),
        "ATLAS-LEGACY-ACTIVE",
        classification="unchanged",
        rationale="A pending interpretation.",
        evidence_refs=[],
        considered_refs=["E1"],
        evaluated_at="2026-01-04T00:00:00+00:00",
    )

    assert [
        row["scenario_id"]
        for row in list_signpost_assessments_for_review(db_path)
    ] == ["ATLAS-LEGACY-ACTIVE"]
    migrate(str(db_path))
    assert get_latest_signpost_assessment(
        str(db_path),
        "ATLAS-LEGACY-ACTIVE",
    )["review_status"] == "pending"


def test_signpost_approval_rejects_orphan_and_unapproved_evidence(tmp_path):
    db_path = _scenario_db(tmp_path)
    evidence_db = _evidence_db(tmp_path)
    add_evidence_event(str(evidence_db), _event("EVT-UNAPPROVED"))

    for scenario_id, event_id in (
        ("ATLAS-ORPHAN", "EVT-MISSING"),
        ("ATLAS-UNAPPROVED", "EVT-UNAPPROVED"),
    ):
        upsert_scenario(str(db_path), {
            "scenario_id": scenario_id,
            "title": "Evidence validation test",
            "status": "active",
        })
        record_signpost_assessment(
            str(db_path),
            scenario_id,
            classification="strengthened",
            rationale="A directional assessment.",
            evidence_refs=[event_id],
            considered_refs=[event_id],
            evaluated_at="2026-01-04T00:00:00+00:00",
        )
        with pytest.raises(ReviewError, match="still resolve to approved"):
            approve_signpost_assessment(
                db_path,
                scenario_id,
                evidence_db=evidence_db,
            )


def test_signpost_decision_rechecks_evidence_if_preflight_is_bypassed(
    tmp_path,
    monkeypatch,
):
    import workbench_atlas.review as review_module

    db_path = _scenario_db(tmp_path)
    evidence_db = _evidence_db(tmp_path)
    add_evidence_event(str(evidence_db), _event("EVT-TOCTOU"))
    upsert_scenario(str(db_path), {
        "scenario_id": "ATLAS-TOCTOU",
        "title": "Transactional evidence validation",
        "status": "active",
    })
    record_signpost_assessment(
        str(db_path),
        "ATLAS-TOCTOU",
        classification="strengthened",
        rationale="Pending evidence recheck.",
        evidence_refs=["EVT-TOCTOU"],
        considered_refs=["EVT-TOCTOU"],
        evaluated_at="2026-01-04T00:00:00+00:00",
    )
    # Simulate evidence changing after the user-facing preflight check. The
    # deciding transaction must independently fail closed.
    monkeypatch.setattr(
        review_module,
        "_validate_signpost_evidence_for_approval",
        lambda *args, **kwargs: None,
    )

    with pytest.raises(ReviewError, match="still resolve to approved"):
        approve_signpost_assessment(
            db_path,
            "ATLAS-TOCTOU",
            evidence_db=evidence_db,
        )
    assert get_latest_signpost_assessment(
        str(db_path),
        "ATLAS-TOCTOU",
    )["review_status"] == "pending"


def test_signpost_approval_rejects_invalid_snapshot_and_projection(tmp_path):
    db_path = _scenario_db(tmp_path)
    evidence_db = _evidence_db(tmp_path)
    add_evidence_event(str(evidence_db), _event("EVT-VALID"))
    approve_evidence(
        evidence_db,
        "EVT-VALID",
        public_source_attested=True,
        rights_attested=True,
    )

    upsert_scenario(str(db_path), {
        "scenario_id": "ATLAS-MISMATCHED-REFS",
        "title": "Invalid snapshot",
        "status": "active",
        "signpost_status": "strengthened",
        "signpost_assessment": "Invalid subset.",
        "signpost_evidence_refs": json.dumps(["EVT-VALID", "EVT-OTHER"]),
        "signpost_checked_evidence_refs": json.dumps(["EVT-VALID"]),
        "signposts_last_evaluated": "2026-01-04T00:00:00+00:00",
        "signpost_review_status": "pending",
    })
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO signpost_assessments (
                scenario_id,
                classification,
                rationale,
                evidence_refs,
                considered_refs,
                evaluated_at,
                review_status
            )
            VALUES (?, ?, ?, ?, ?, ?, 'pending')
            """,
            (
                "ATLAS-MISMATCHED-REFS",
                "strengthened",
                "Invalid subset.",
                json.dumps(["EVT-VALID", "EVT-OTHER"]),
                json.dumps(["EVT-VALID"]),
                "2026-01-04T00:00:00+00:00",
            ),
        )
    with pytest.raises(ReviewError, match="subset"):
        approve_signpost_assessment(
            db_path,
            "ATLAS-MISMATCHED-REFS",
            evidence_db=evidence_db,
        )

    upsert_scenario(str(db_path), {
        "scenario_id": "ATLAS-PROJECTION-MISMATCH",
        "title": "Projection mismatch",
        "status": "active",
    })
    record_signpost_assessment(
        str(db_path),
        "ATLAS-PROJECTION-MISMATCH",
        classification="strengthened",
        rationale="Snapshot rationale.",
        evidence_refs=["EVT-VALID"],
        considered_refs=["EVT-VALID"],
        evaluated_at="2026-01-04T00:00:00+00:00",
    )
    update_scenario_fields(
        str(db_path),
        "ATLAS-PROJECTION-MISMATCH",
        {"signpost_assessment": "Tampered projection."},
    )
    with pytest.raises(ReviewError, match="rationale no longer matches"):
        approve_signpost_assessment(
            db_path,
            "ATLAS-PROJECTION-MISMATCH",
            evidence_db=evidence_db,
        )


def test_signpost_cli_names_the_row_and_accepts_reason(tmp_path, capsys):
    from workbench_atlas.review_cli import main as review_main

    db_path = _scenario_db(tmp_path)
    evidence_db = _evidence_db(tmp_path)
    add_evidence_event(str(evidence_db), _event("EVT-SIGNPOST-REASON"))
    approve_evidence(
        evidence_db,
        "EVT-SIGNPOST-REASON",
        public_source_attested=True,
        rights_attested=True,
    )
    upsert_scenario(str(db_path), {
        "scenario_id": "ATLAS-SIGNPOST-REASON",
        "title": "Pending signpost interpretation",
        "risk_category": "Digital resilience",
        "status": "active",
    })
    record_signpost_assessment(
        str(db_path),
        "ATLAS-SIGNPOST-REASON",
        classification="weakened",
        rationale="New evidence matches a weakening signpost.",
        evidence_refs=["EVT-SIGNPOST-REASON"],
        considered_refs=[
            "EVT-SIGNPOST-REASON",
            "https://www.sec.gov/example/EVT-SIGNPOST-REASON",
        ],
        evaluated_at="2026-01-04T00:00:00+00:00",
    )

    def invoke(*argv: str) -> tuple[int, str, str]:
        code = review_main([
            "--scenario-db", str(db_path),
            "--evidence-db", str(evidence_db),
            *argv,
        ])
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    code, out, err = invoke("list", "--kind", "signpost")
    assert code == 0, err
    rows = json.loads(out)
    assert rows[0]["scenario_id"] == "ATLAS-SIGNPOST-REASON"
    assert rows[0]["assessment_id"] == rows[0]["id"]
    row_id = str(rows[0]["id"])

    code, _out, err = invoke("approve-signpost", row_id, "--reason", "must not apply")
    assert code == 2
    assert "looks like an assessment id" in err
    assert "ATLAS-SIGNPOST-REASON" in err
    assert get_latest_signpost_assessment(
        str(db_path), "ATLAS-SIGNPOST-REASON"
    )["review_status"] == "pending"

    code, _out, err = invoke("approve-signpost", "ATLAS-NO-SUCH")
    assert code == 2
    assert "has no signpost assessment history" in err

    code, _out, err = invoke("approve-signpost", "999999")
    assert code == 2
    assert "looks like an assessment id" in err
    assert "999999" in err
    assert "(" not in err

    code, out, err = invoke(
        "approve-signpost",
        "ATLAS-SIGNPOST-REASON",
        "--reason",
        "Checked against the cited notice.",
    )
    assert code == 0, err
    assert json.loads(out)["signpost_review_status"] == "approved"
    history = get_latest_signpost_assessment(str(db_path), "ATLAS-SIGNPOST-REASON")
    assert history["review_status"] == "approved"
    assert "cited notice" in history["reviewer_note"]


def test_unchanged_signpost_approval_requires_considered_evidence(tmp_path):
    db_path = _scenario_db(tmp_path)
    evidence_db = _evidence_db(tmp_path)
    upsert_scenario(str(db_path), {
        "scenario_id": "ATLAS-UNCHANGED-ORPHAN",
        "title": "Unchanged without a packet",
        "status": "active",
        "signpost_status": "unchanged",
        "signpost_assessment": "No directional change.",
        "signpost_evidence_refs": "[]",
        "signpost_checked_evidence_refs": "[]",
        "signposts_last_evaluated": "2026-01-04T00:00:00+00:00",
        "signpost_review_status": "pending",
    })
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO signpost_assessments (
                scenario_id,
                classification,
                rationale,
                evidence_refs,
                considered_refs,
                evaluated_at,
                review_status
            )
            VALUES (?, 'unchanged', ?, '[]', '[]', ?, 'pending')
            """,
            (
                "ATLAS-UNCHANGED-ORPHAN",
                "No directional change.",
                "2026-01-04T00:00:00+00:00",
            ),
        )
    with pytest.raises(ReviewError, match="considered evidence"):
        approve_signpost_assessment(
            db_path,
            "ATLAS-UNCHANGED-ORPHAN",
            evidence_db=evidence_db,
        )


def test_signpost_snapshot_columns_and_rows_are_immutable(tmp_path):
    db_path = _scenario_db(tmp_path)
    upsert_scenario(str(db_path), {
        "scenario_id": "ATLAS-IMMUTABLE",
        "title": "Immutable history",
        "status": "active",
    })
    snapshot = record_signpost_assessment(
        str(db_path),
        "ATLAS-IMMUTABLE",
        classification="strengthened",
        rationale="Immutable rationale.",
        evidence_refs=["EVT-1"],
        considered_refs=["EVT-1"],
        evaluated_at="2026-01-04T00:00:00+00:00",
    )
    with sqlite3.connect(db_path) as conn:
        with pytest.raises(sqlite3.DatabaseError, match="immutable"):
            conn.execute(
                "UPDATE signpost_assessments SET rationale = ? WHERE id = ?",
                ("Changed", snapshot["id"]),
            )
        with pytest.raises(sqlite3.DatabaseError, match="immutable"):
            conn.execute(
                "UPDATE signpost_assessments SET origin = ? WHERE id = ?",
                ("legacy_projection_backfill", snapshot["id"]),
            )
        with pytest.raises(sqlite3.DatabaseError, match="cannot be deleted"):
            conn.execute(
                "DELETE FROM signpost_assessments WHERE id = ?",
                (snapshot["id"],),
            )


def test_new_signpost_snapshot_supersedes_only_the_prior_pending_review(tmp_path):
    db_path = _scenario_db(tmp_path)
    upsert_scenario(str(db_path), {
        "scenario_id": "ATLAS-SUPERSEDE-PENDING",
        "title": "Pending snapshot replacement",
        "status": "active",
    })
    first = record_signpost_assessment(
        str(db_path),
        "ATLAS-SUPERSEDE-PENDING",
        classification="strengthened",
        rationale="First pending rationale.",
        evidence_refs=["EVT-1"],
        considered_refs=["EVT-1"],
        evaluated_at="2026-01-04T00:00:00+00:00",
    )
    second = record_signpost_assessment(
        str(db_path),
        "ATLAS-SUPERSEDE-PENDING",
        classification="weakened",
        rationale="Second pending rationale.",
        evidence_refs=["EVT-2"],
        considered_refs=["EVT-2"],
        evaluated_at="2026-01-05T00:00:00+00:00",
    )

    with sqlite3.connect(db_path) as conn:
        statuses = dict(conn.execute(
            "SELECT id, review_status FROM signpost_assessments"
        ).fetchall())
    assert statuses[first["id"]] == "superseded"
    assert statuses[second["id"]] == "pending"


def test_cli_missing_evidence_db_is_actionable_and_not_created(tmp_path):
    missing_db = tmp_path / "missing.db"
    risks_path = tmp_path / "risks.json"
    _write_risks(risks_path, _risk("Pending risk"))
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "review_records.py"),
            "--evidence-db",
            str(missing_db),
            "--risks-file",
            str(risks_path),
            "list",
            "--kind",
            "event",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "Evidence database does not exist" in result.stderr
    assert not missing_db.exists()
