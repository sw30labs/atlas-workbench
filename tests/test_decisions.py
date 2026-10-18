"""Human decisions survive competing requests and remain attributable."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from contingency_atlas.db import add_evidence_event, get_evidence_event
from contingency_atlas.decisions import list_decisions, record_revision
from contingency_atlas.phases.phase2_risks import load_emerging_risks
from contingency_atlas.review import ReviewError, reject_evidence, reject_risk
from contingency_atlas.schema import migrate_evidence_events


@pytest.fixture
def pending_evidence(tmp_path):
    path = tmp_path / "evidence.db"
    migrate_evidence_events(str(path))
    add_evidence_event(str(path), {
        "event_id": "decision-fixture", "headline": "Fictional decision fixture",
        "source_type": "public_reporting", "review_status": "pending",
        "provenance_note": "Synthetic test of a pending public-review record.",
    })
    return path


def test_concurrent_review_of_same_revision_has_one_winner(pending_evidence):
    revision = record_revision(get_evidence_event(str(pending_evidence), "decision-fixture"))

    def decide(actor):
        try:
            reject_evidence(pending_evidence, "decision-fixture", reason=f"Reason from {actor}",
                            reviewer=actor, expected_revision=revision)
            return actor
        except ReviewError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(decide, ("Reviewer A", "Reviewer B")))
    winners = [actor for actor in results if actor]
    assert len(winners) == 1
    decisions = list_decisions(str(pending_evidence), record_id="decision-fixture")
    assert len(decisions) == 1
    assert decisions[0]["actor"] == winners[0]
    assert decisions[0]["before_revision"] == revision
    record = get_evidence_event(str(pending_evidence), "decision-fixture")
    assert decisions[0]["after_revision"] == record_revision(record)
    assert f"Reason from {winners[0]}" in record["provenance_note"]


def test_evidence_decision_log_cannot_be_updated_or_deleted(pending_evidence):
    reject_evidence(pending_evidence, "decision-fixture", reason="Source cannot support claim",
                    reviewer="Independent reviewer")
    with sqlite3.connect(pending_evidence) as conn:
        for statement in ("DELETE FROM review_decisions",
                          "UPDATE review_decisions SET actor='someone else'"):
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                conn.execute(statement)


def test_invalid_actor_rolls_back_record_and_history(pending_evidence):
    before = get_evidence_event(str(pending_evidence), "decision-fixture")
    with pytest.raises(ValueError, match="reviewer"):
        reject_evidence(pending_evidence, "decision-fixture", reason="Insufficient evidence",
                        reviewer="")
    assert get_evidence_event(str(pending_evidence), "decision-fixture") == before
    assert list_decisions(str(pending_evidence)) == []


def test_risk_history_and_revision_are_atomic_with_register(tmp_path):
    path = tmp_path / "risks.json"
    path.write_text(json.dumps([{"risk_id": "risk-a", "issue": "Fictional hypothesis",
                                 "source_type": "public_research", "review_status": "pending"}]))
    before = load_emerging_risks(path)[0]
    revision = record_revision(before)
    rejected = reject_risk(path, "Fictional hypothesis", reason="Needs direct source",
                           reviewer="Reviewer A", expected_revision=revision)
    with pytest.raises(ReviewError, match="changed after"):
        reject_risk(path, "Fictional hypothesis", reason="Stale request",
                    reviewer="Reviewer B", expected_revision=revision)
    stored = load_emerging_risks(path)[0]
    assert stored == rejected
    assert stored["reviewed_by"] == "Reviewer A"
    assert len(stored["review_history"]) == 1
    assert stored["review_history"][0]["before_revision"] == revision
    assert stored["review_history"][0]["after_revision"] == record_revision(stored)
