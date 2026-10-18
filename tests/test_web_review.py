"""Review card: validation reports, decide endpoint, advisory pre-analysis.

Covers the dashboard's human review boundary end to end: the non-raising
validation reports must agree with the raising validators, decisions must
enforce attestations and the register lock, and the LLM pre-analysis must
stay advisory (cache + agreement audit, never review state).
"""

from __future__ import annotations

import http.client
import json
import re
import sqlite3
import threading
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from contingency_atlas.config import Settings
from contingency_atlas.review import (
    ReviewError,
    evidence_validation_report,
    register_lock,
    risk_validation_report,
    validate_evidence_for_approval,
    validate_risk_for_approval,
)
from contingency_atlas.review_assist import (
    agreement_stats,
    content_hash,
    load_suggestions,
    run_review_analysis,
)
from contingency_atlas.schema import migrate, migrate_evidence_events
from contingency_atlas.web import runner, server
from contingency_atlas.web.server import DashboardHandler

ROOT = Path(__file__).resolve().parents[1]

FP = "a" * 64


def _event(event_id: str, **overrides) -> dict:
    record = {
        "event_id": event_id,
        "headline": f"Public incident {event_id}",
        "loss_amount_usd": 1_000_000.0,
        "source_date": "2026-03-15",
        "risk_category": "Digital resilience",
        "source_url": f"https://news.example.com/{event_id}",
        "source_publisher": "Example Wire",
        "source_title": "Example incident report",
        "retrieved_at": "2026-03-16T09:00:00+00:00",
        "source_type": "public_reporting",
        "record_type": "event",
        "provenance_class": "public_primary",
        "source_locator": "",
        "independently_authored_summary": "Independent summary of the incident.",
        "rights_review": None,
        "content_fingerprint": FP,
        "review_status": "pending",
        "reviewed_at": None,
        "taxonomy_version": "atlas-1.0",
        "provenance_note": "Fixture record for review-card tests.",
    }
    record.update(overrides)
    return record


def _insert_events(db_path, events) -> None:
    migrate_evidence_events(str(db_path))
    conn = sqlite3.connect(str(db_path))
    for record in events:
        columns = ", ".join(record)
        marks = ", ".join("?" for _ in record)
        conn.execute(
            f"INSERT INTO evidence_events ({columns}) VALUES ({marks})",
            tuple(record.values()),
        )
    conn.commit()
    conn.close()


APPROVED = {
    "review_status": "approved",
    "rights_review": "attested_for_public_use",
    "reviewed_at": "2026-03-17T10:00:00+00:00",
}


@pytest.fixture
def review_settings(tmp_path, monkeypatch):
    """Two pending events, one approvable pending risk, one blocked risk."""
    ev_db = tmp_path / "event_evidence.db"
    _insert_events(ev_db, [
        _event("EVT-A1", **APPROVED),
        _event("EVT-A2", **APPROVED),
        _event("EVT-R1", review_status="rejected"),
        _event("EVT-P1"),                                # pending, approvable
        _event("EVT-P2", source_url="not a url"),        # pending, blocked
    ])
    risks = [
        {"issue": "Approvable research risk", "description": "well grounded",
         "source_type": "public_research", "provenance_note": "fixture",
         "review_status": "pending", "evidence_refs": ["EVT-A1", "EVT-A2"]},
        {"issue": "Blocked risk on rejected evidence", "description": "poorly grounded",
         "source_type": "public_research", "provenance_note": "fixture",
         "review_status": "pending", "evidence_refs": ["EVT-R1", "EVT-MISSING"]},
    ]
    risks_path = tmp_path / "emerging_risks.json"
    risks_path.write_text(json.dumps(risks))
    atlas_db = tmp_path / "atlas.db"
    migrate(str(atlas_db))
    settings = Settings(
        scenario_db_path=atlas_db,
        evidence_db_path=ev_db,
        emerging_risks_path=risks_path,
    )
    monkeypatch.setattr(server, "get_settings", lambda: settings)
    return settings


class _FakeJudgeSettings:
    llm_model_judge = "fake-judge"


class FakeClient:
    """Deterministic advisory analyst standing in for OMLXClient."""

    settings = _FakeJudgeSettings()

    def __init__(self, recommendation: str = "approve"):
        self.recommendation = recommendation
        self.calls = 0

    def chat_json(self, messages, model=None, temperature=0.0):
        self.calls += 1
        assert "advisory" in messages[0]["content"].lower()
        return {
            "recommendation": self.recommendation,
            "confidence": 0.7,
            "summary": "fixture analysis",
            "reasons": ["reason one"],
            "concerns": [],
            "human_must_verify": [],
        }


@contextmanager
def _dashboard(port_holder=None):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), DashboardHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd.server_address[1]
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def _request(port, method, path, body=None, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        payload = json.dumps(body).encode() if body is not None else None
        base = {"Content-Type": "application/json"} if payload else {}
        connection.request(method, path, body=payload, headers={**base, **(headers or {})})
        response = connection.getresponse()
        return response.status, json.loads(response.read() or b"{}")
    finally:
        connection.close()


# ── validation reports agree with the raising validators ─────────────────


def test_reports_agree_with_raising_validators(review_settings):
    events = [
        _event("EVT-OK"),
        _event("EVT-BAD-URL", source_url="not a url"),
        _event("EVT-BAD-DATE", source_date="15/03/2026"),
        _event("EVT-SYN", source_type="synthetic"),
        _event("EVT-BAD-FP", content_fingerprint="nope"),
    ]
    for record in events:
        try:
            validate_evidence_for_approval(record)
            raised = False
        except ReviewError:
            raised = True
        report = evidence_validation_report(record)
        assert report["approvable"] == (not raised), record["event_id"]

    risks = [
        {"issue": "ok", "description": "d", "source_type": "public_research",
         "provenance_note": "n", "evidence_refs": ["EVT-A1"]},
        {"issue": "bad refs", "description": "d", "source_type": "public_research",
         "provenance_note": "n", "evidence_refs": ["EVT-R1"]},
        {"issue": "no refs", "description": "d", "source_type": "public_research",
         "provenance_note": "n", "evidence_refs": []},
        {"issue": "synthetic", "description": "d", "source_type": "synthetic",
         "provenance_note": "n", "evidence_refs": ["EVT-A1"]},
    ]
    for record in risks:
        try:
            validate_risk_for_approval(record, evidence_db=review_settings.evidence_db_path)
            raised = False
        except ReviewError:
            raised = True
        report = risk_validation_report(record, evidence_db=review_settings.evidence_db_path)
        assert report["approvable"] == (not raised), record["issue"]


def test_risk_report_classifies_refs(review_settings):
    record = {
        "issue": "classify", "description": "d", "source_type": "public_research",
        "provenance_note": "n",
        "evidence_refs": ["EVT-A1", "EVT-R1", "EVT-P1", "EVT-MISSING"],
    }
    report = risk_validation_report(record, evidence_db=review_settings.evidence_db_path)
    statuses = {ref["ref"]: ref["status"] for ref in report["refs"]}
    assert statuses == {
        "EVT-A1": "approved",
        "EVT-R1": "rejected",
        "EVT-P1": "pending",
        "EVT-MISSING": "missing",
    }
    assert not report["approvable"]


# ── /api/review/pending ──────────────────────────────────────────────────


def test_review_pending_endpoint_shape(review_settings):
    queue = server.build_review_queue()
    assert len(queue["events"]) == 2      # EVT-P1, EVT-P2 (pending only)
    assert len(queue["risks"]) == 2
    entry = queue["risks"][0]
    assert set(entry) >= {"kind", "record_id", "record", "report", "suggestion",
                          "suggestion_stale"}
    approvable = {e["record_id"]: e["report"]["approvable"] for e in queue["risks"]}
    assert approvable == {"Approvable research risk": True,
                          "Blocked risk on rejected evidence": False}
    assert queue["agreement"] == {"decided": 0, "agreed": 0}


# ── /api/review/decide ───────────────────────────────────────────────────


def test_decide_approve_risk_requires_attestations(review_settings):
    with _dashboard() as port:
        status, payload = _request(port, "POST", "/api/review/decide", {
            "kind": "risk", "record_id": "Approvable research risk",
            "action": "approve",
        })
    assert status == 400
    assert "attestation" in payload["error"].lower()


@pytest.mark.parametrize(
    ("path", "target"),
    [
        ("/api/review/decide", {
            "kind": "risk", "record_id": "Approvable research risk",
        }),
        ("/api/review/decide-batch", {
            "items": [{
                "kind": "risk", "record_id": "Approvable research risk",
            }],
        }),
    ],
)
def test_decide_attestations_require_literal_json_true(
    review_settings, path, target,
):
    with _dashboard() as port:
        status, payload = _request(port, "POST", path, {
            **target,
            "action": "approve",
            "confirm_public_source": "true",
            "confirm_rights": 1,
        })
    assert status == 400
    assert "attestation" in payload["error"].lower()
    risks = {
        risk["issue"]: risk
        for risk in json.loads(review_settings.emerging_risks_path.read_text())
    }
    assert risks["Approvable research risk"]["review_status"] == "pending"


def test_decide_approve_risk_happy_path(review_settings):
    with _dashboard() as port:
        status, payload = _request(port, "POST", "/api/review/decide", {
            "kind": "risk", "record_id": "Approvable research risk",
            "action": "approve", "confirm_public_source": True,
            "confirm_rights": True, "note": "looks solid",
        })
    assert status == 200 and payload["ok"] is True
    assert payload["record"]["review_status"] == "approved"
    assert payload["record"]["evidence_refs"] == ["EVT-A1", "EVT-A2"]
    saved = json.loads(review_settings.emerging_risks_path.read_text())
    by_issue = {r["issue"]: r for r in saved}
    assert by_issue["Approvable research risk"]["review_status"] == "approved"


def test_decide_blocked_risk_is_refused(review_settings):
    with _dashboard() as port:
        status, payload = _request(port, "POST", "/api/review/decide", {
            "kind": "risk", "record_id": "Blocked risk on rejected evidence",
            "action": "approve", "confirm_public_source": True,
            "confirm_rights": True,
        })
    assert status == 400
    assert "resolve" in payload["error"].lower()


def test_decide_reject_requires_reason(review_settings):
    with _dashboard() as port:
        status, payload = _request(port, "POST", "/api/review/decide", {
            "kind": "risk", "record_id": "Blocked risk on rejected evidence",
            "action": "reject",
        })
        assert status == 400 and "reason" in payload["error"].lower()
        status, payload = _request(port, "POST", "/api/review/decide", {
            "kind": "risk", "record_id": "Blocked risk on rejected evidence",
            "action": "reject", "reason": "evidence was rejected",
        })
    assert status == 200
    assert payload["record"]["review_status"] == "rejected"


def test_decide_approve_event(review_settings):
    with _dashboard() as port:
        status, payload = _request(port, "POST", "/api/review/decide", {
            "kind": "event", "record_id": "EVT-P1", "action": "approve",
            "confirm_public_source": True, "confirm_rights": True,
        })
    assert status == 200
    assert payload["record"]["review_status"] == "approved"
    assert payload["record"]["rights_review"] == "attested_for_public_use"


# ── /api/review/decide-batch ────────────────────────────────────────────────────


def test_decide_batch_approve_applies_common_note_across_stores(review_settings):
    note = "Verified together against the public-source review checklist."
    run_review_analysis(review_settings, FakeClient("approve"))
    with _dashboard() as port:
        status, payload = _request(port, "POST", "/api/review/decide-batch", {
            "items": [
                {"kind": "event", "record_id": "EVT-P1"},
                {"kind": "risk", "record_id": "Approvable research risk"},
            ],
            "action": "approve",
            "note": note,
            "confirm_public_source": True,
            "confirm_rights": True,
        })

    assert status == 200
    assert payload["ok"] is True
    assert payload["action"] == "approve"
    assert (payload["total"], payload["succeeded"], payload["failed"]) == (2, 2, 0)
    assert [result["record_id"] for result in payload["results"]] == [
        "EVT-P1", "Approvable research risk",
    ]
    assert all(result["ok"] for result in payload["results"])
    assert all(
        result["record"]["review_status"] == "approved"
        for result in payload["results"]
    )

    conn = sqlite3.connect(str(review_settings.evidence_db_path))
    event_status, event_note = conn.execute(
        "SELECT review_status, provenance_note FROM evidence_events WHERE event_id = ?",
        ("EVT-P1",),
    ).fetchone()
    conn.close()
    assert event_status == "approved"
    assert note in event_note
    risks = {
        risk["issue"]: risk
        for risk in json.loads(review_settings.emerging_risks_path.read_text())
    }
    assert risks["Approvable research risk"]["review_status"] == "approved"
    assert note in risks["Approvable research risk"]["provenance_note"]
    assert agreement_stats(review_settings.scenario_db_path) == {
        "decided": 2,
        "agreed": 2,
    }


def test_decide_batch_reject_applies_one_reason_to_every_selected_item(
    review_settings,
):
    reason = "The cited material does not support the submitted claim."
    with _dashboard() as port:
        status, payload = _request(port, "POST", "/api/review/decide-batch", {
            "items": [
                {"kind": "event", "record_id": "EVT-P2"},
                {"kind": "risk", "record_id": "Blocked risk on rejected evidence"},
            ],
            "action": "reject",
            "note": reason,
            "reason": reason,
        })

    assert status == 200
    assert payload["ok"] is True
    assert (payload["succeeded"], payload["failed"]) == (2, 0)
    conn = sqlite3.connect(str(review_settings.evidence_db_path))
    event_status, event_note = conn.execute(
        "SELECT review_status, provenance_note FROM evidence_events WHERE event_id = ?",
        ("EVT-P2",),
    ).fetchone()
    conn.close()
    assert event_status == "rejected"
    assert reason in event_note
    risks = {
        risk["issue"]: risk
        for risk in json.loads(review_settings.emerging_risks_path.read_text())
    }
    assert risks["Blocked risk on rejected evidence"]["review_status"] == "rejected"
    assert reason in risks["Blocked risk on rejected evidence"]["provenance_note"]


def test_decide_batch_reports_partial_success_and_preserves_failed_item(
    review_settings,
):
    with _dashboard() as port:
        status, payload = _request(port, "POST", "/api/review/decide-batch", {
            "items": [
                {"kind": "event", "record_id": "EVT-P1"},
                {"kind": "event", "record_id": "EVT-P2"},
            ],
            "action": "approve",
            "note": "Batch with one record that still has a validation blocker.",
            "confirm_public_source": True,
            "confirm_rights": True,
        })

    assert status == 207
    assert payload["ok"] is False
    assert (payload["total"], payload["succeeded"], payload["failed"]) == (2, 1, 1)
    results = {result["record_id"]: result for result in payload["results"]}
    assert results["EVT-P1"]["ok"] is True
    assert results["EVT-P1"]["record"]["review_status"] == "approved"
    assert results["EVT-P2"]["ok"] is False
    assert "source_url" in results["EVT-P2"]["error"]

    conn = sqlite3.connect(str(review_settings.evidence_db_path))
    states = dict(conn.execute(
        "SELECT event_id, review_status FROM evidence_events WHERE event_id IN (?, ?)",
        ("EVT-P1", "EVT-P2"),
    ))
    conn.close()
    assert states == {"EVT-P1": "approved", "EVT-P2": "pending"}


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ({"items": [], "action": "approve"}, "item"),
        ({
            "items": [
                {"kind": "event", "record_id": "EVT-P1"},
                {"kind": "event", "record_id": "EVT-P1"},
            ],
            "action": "approve",
            "confirm_public_source": True,
            "confirm_rights": True,
        }, "duplicate"),
        ({
            "items": ["event:EVT-P1"],
            "action": "reject",
            "note": "Malformed selection.",
        }, "object"),
        ({
            "items": [{"kind": "scenario", "record_id": "SCN-1"}],
            "action": "reject",
            "note": "Wrong queue kind.",
        }, "kind"),
        ({
            "items": [{"kind": "event", "record_id": "   "}],
            "action": "reject",
            "note": "Blank identifier.",
        }, "record_id"),
        ({
            "items": [{"kind": "event", "record_id": "EVT-P1"}],
            "action": "reject",
        }, "note"),
        ({
            "items": [{"kind": "event", "record_id": "EVT-P1"}],
            "action": "approve",
        }, "attestation"),
        ({
            "items": [{"kind": "merge", "record_id": "missing-proposal"}],
            "action": "reject",
            "note": "Not a duplicate.",
            "reason": "Not a duplicate.",
        }, "reviewer"),
    ],
)
def test_decide_batch_preflight_rejects_invalid_request_without_mutation(
    review_settings, body, message,
):
    with _dashboard() as port:
        status, payload = _request(
            port, "POST", "/api/review/decide-batch", body,
        )
    assert status == 400
    assert message in payload["error"].lower()
    conn = sqlite3.connect(str(review_settings.evidence_db_path))
    event_status = conn.execute(
        "SELECT review_status FROM evidence_events WHERE event_id = ?", ("EVT-P1",),
    ).fetchone()[0]
    conn.close()
    assert event_status == "pending"


def test_decide_batch_caps_selection_at_500_items(review_settings):
    with _dashboard() as port:
        status, payload = _request(port, "POST", "/api/review/decide-batch", {
            "items": [
                {"kind": "event", "record_id": f"missing-{index}"}
                for index in range(501)
            ],
            "action": "reject",
            "note": "Not in the review queue.",
            "reason": "Not in the review queue.",
        })
    assert status == 400
    assert "500" in payload["error"]


def test_decide_cross_origin_is_blocked(review_settings):
    with _dashboard() as port:
        status, payload = _request(
            port, "POST", "/api/review/decide",
            {"kind": "risk", "record_id": "x", "action": "reject", "reason": "r"},
            headers={"Origin": "http://evil.example"},
        )
    assert status == 403


def test_decide_batch_cross_origin_is_blocked(review_settings):
    with _dashboard() as port:
        status, _payload = _request(
            port, "POST", "/api/review/decide-batch",
            {
                "items": [{"kind": "event", "record_id": "EVT-P1"}],
                "action": "reject",
                "note": "reason",
                "reason": "reason",
            },
            headers={"Origin": "http://evil.example"},
        )
    assert status == 403


def test_decide_locked_register_returns_409(review_settings, monkeypatch):
    import contingency_atlas.review as review_module

    original = review_module.register_lock
    # keep the suite fast: the handler's lock attempt gives up in 0.3 s
    monkeypatch.setattr(
        review_module, "register_lock",
        lambda path, **kwargs: original(path, timeout=0.3),
    )
    with original(review_settings.emerging_risks_path):
        with _dashboard() as port:
            status, payload = _request(port, "POST", "/api/review/decide", {
                "kind": "risk", "record_id": "Blocked risk on rejected evidence",
                "action": "reject", "reason": "r",
            })
    assert status == 409
    assert "locked" in payload["error"].lower()


def test_register_lock_is_exclusive(review_settings):
    path = review_settings.emerging_risks_path
    with register_lock(path):
        with pytest.raises(ReviewError, match="locked"):
            with register_lock(path, timeout=0.2):
                pass  # pragma: no cover


# ── advisory pre-analysis ────────────────────────────────────────────────


def test_run_review_analysis_cache_and_force(review_settings):
    client = FakeClient()
    first = run_review_analysis(review_settings, client)
    assert first["candidates"] == 4 and first["analyzed"] == 4
    second = run_review_analysis(review_settings, FakeClient())
    assert second["analyzed"] == 0 and second["cached"] == 4
    third = run_review_analysis(review_settings, FakeClient(), force=True)
    assert third["analyzed"] == 4


def test_analysis_never_mutates_review_state(review_settings):
    before = json.loads(review_settings.emerging_risks_path.read_text())
    run_review_analysis(review_settings, FakeClient())
    after = json.loads(review_settings.emerging_risks_path.read_text())
    assert before == after


def test_suggestion_attaches_and_goes_stale(review_settings):
    run_review_analysis(review_settings, FakeClient())
    queue = server.build_review_queue()
    entry = next(e for e in queue["risks"]
                 if e["record_id"] == "Approvable research risk")
    assert entry["suggestion"]["recommendation"] == "approve"
    assert entry["suggestion_stale"] is False
    # The two attestations are always listed for the human, whatever the model said.
    verify = " ".join(entry["suggestion"]["human_must_verify"]).lower()
    assert "publicly accessible" in verify and "rights" in verify

    # phase-2-style rewrite changes the content hash -> suggestion is stale
    risks = json.loads(review_settings.emerging_risks_path.read_text())
    for risk in risks:
        if risk["issue"] == "Approvable research risk":
            risk["description"] = "rewritten by monitoring"
    review_settings.emerging_risks_path.write_text(json.dumps(risks))
    queue = server.build_review_queue()
    entry = next(e for e in queue["risks"]
                 if e["record_id"] == "Approvable research risk")
    assert entry["suggestion_stale"] is True


def test_decision_records_agreement(review_settings):
    run_review_analysis(review_settings, FakeClient("approve"))
    with _dashboard() as port:
        _request(port, "POST", "/api/review/decide", {
            "kind": "risk", "record_id": "Approvable research risk",
            "action": "approve", "confirm_public_source": True,
            "confirm_rights": True,
        })
    stats = agreement_stats(review_settings.scenario_db_path)
    assert stats == {"decided": 1, "agreed": 1}


def test_content_hash_is_stable_and_field_sensitive(review_settings):
    record = {"issue": "x", "description": "y", "evidence_refs": ["a"]}
    assert content_hash("risk", record) == content_hash("risk", dict(record))
    changed = dict(record, description="z")
    assert content_hash("risk", record) != content_hash("risk", changed)


# ── channel proposal explanations (P1) ───────────────────────────────────


def _channel_candidate(**overrides):
    cand = {
        "kind": "edge_add",
        "risk_ids": ["atom-a", "atom-b"],
        "channel": "cloud",
        "mechanism": "fixture transmission mechanism",
        "citation_url": "https://example.com/report.pdf",
        "citation_status": "resolved",
    }
    cand.update(overrides)
    return cand


def _add_channel_proposal(settings, **overrides) -> str:
    from contingency_atlas.proposals import create_channel_proposals
    result = create_channel_proposals(
        settings.scenario_db_path, [_channel_candidate(**overrides)]
    )
    assert result["created"] == 1
    return result["proposal_ids"][0]


class ExplainClient(FakeClient):
    """FakeClient that also answers the channel-explanation prompt shape."""

    def chat_json(self, messages, model=None, temperature=0.0):
        out = super().chat_json(messages, model=model, temperature=temperature)
        out["explanation"] = "fixture channel explanation"
        out["weaknesses"] = ["fixture weakness"]
        return out


class BoomClient:
    """Every call fails — analysis errors must be collected, not raised."""

    settings = _FakeJudgeSettings()

    def chat_json(self, messages, model=None, temperature=0.0):
        from contingency_atlas.llm import LLMError
        raise LLMError("model offline")


def test_channel_explanation_cache_and_force(review_settings):
    pid = _add_channel_proposal(review_settings)
    first = run_review_analysis(review_settings, ExplainClient())
    assert first["candidates"] == 5 and first["analyzed"] == 5
    second = run_review_analysis(review_settings, ExplainClient())
    assert second["analyzed"] == 0 and second["cached"] == 5
    third = run_review_analysis(review_settings, ExplainClient(), force=True)
    assert third["analyzed"] == 5

    single = run_review_analysis(
        review_settings, ExplainClient(), kind="channel", record_id=pid
    )
    assert single["candidates"] == 1 and single["cached"] == 1


def test_channel_explanation_never_recommends(review_settings):
    pid = _add_channel_proposal(review_settings)
    run_review_analysis(review_settings, ExplainClient())
    suggestion = load_suggestions(review_settings.scenario_db_path)[("channel", pid)]
    assert suggestion["recommendation"] is None
    assert suggestion["explanation"] == "fixture channel explanation"
    assert suggestion["weaknesses"] == ["fixture weakness"]


def test_channel_explanation_error_is_collected(review_settings):
    pid = _add_channel_proposal(review_settings)
    result = run_review_analysis(
        review_settings, BoomClient(), kind="channel", record_id=pid
    )
    assert result["analyzed"] == 0 and result["failed"] == 1
    assert pid in result["errors"][0]


def test_channel_suggestion_attaches_and_goes_stale(review_settings):
    pid = _add_channel_proposal(review_settings)
    run_review_analysis(review_settings, ExplainClient())
    queue = server.build_review_queue()
    entry = next(e for e in queue["channel_proposals"]["pending"]
                 if e["record_id"] == pid)
    assert entry["suggestion"]["explanation"] == "fixture channel explanation"
    assert entry["suggestion_stale"] is False

    # a rewrite of the mechanism invalidates the cached explanation
    conn = sqlite3.connect(str(review_settings.scenario_db_path))
    conn.execute(
        "UPDATE channel_proposals SET mechanism = 'rewritten' WHERE proposal_id = ?",
        (pid,),
    )
    conn.commit()
    conn.close()
    queue = server.build_review_queue()
    entry = next(e for e in queue["channel_proposals"]["pending"]
                 if e["record_id"] == pid)
    assert entry["suggestion_stale"] is True


def test_agreement_stats_ignores_explanations(review_settings):
    from contingency_atlas.review_assist import record_decision
    pid = _add_channel_proposal(review_settings)
    run_review_analysis(review_settings, ExplainClient())
    record_decision(review_settings.scenario_db_path, "channel", pid, "approve")
    # explanation-only rows carry no recommendation, so there is nothing
    # for the human to agree or disagree with
    assert agreement_stats(review_settings.scenario_db_path) == {"decided": 0, "agreed": 0}


def test_channel_content_hash_is_field_sensitive():
    record = _channel_candidate(proposal_id="cp-x")
    assert content_hash("channel", record) == content_hash("channel", dict(record))
    changed = dict(record, mechanism="different mechanism")
    assert content_hash("channel", record) != content_hash("channel", changed)


def test_nav_badge_counts_every_reviewable_kind(review_settings):
    from contingency_atlas.frameworks.mappings import migrate_mapping_proposals
    from contingency_atlas.proposals import (
        create_analogues,
        create_merge_proposals,
    )

    risks = json.loads(review_settings.emerging_risks_path.read_text())
    risks.append({
        **risks[0],
        "issue": "Changed risk awaiting another decision",
        "review_status": "needs_update",
    })
    review_settings.emerging_risks_path.write_text(json.dumps(risks))

    merge = create_merge_proposals(review_settings.scenario_db_path, [{
        "risk_id_a": "risk-a",
        "risk_id_b": "risk-b",
        "title_a": "Risk A",
        "title_b": "Risk B",
        "similarity": 0.91,
        "components": {},
    }])
    assert merge["created"] == 1
    _add_channel_proposal(review_settings)

    analogue = create_analogues(review_settings.scenario_db_path, [{
        "pair_id": "pair-fixture",
        "risk_ids": ["risk-a", "risk-b"],
        "event": "Fixture historical analogue",
        "disanalogies": ["Fixture disanalogy"],
    }])
    assert analogue["created"] == 1

    migrate_mapping_proposals(review_settings.scenario_db_path)
    with sqlite3.connect(review_settings.scenario_db_path) as conn:
        conn.execute(
            """
            INSERT INTO framework_mapping_proposals (
                proposal_id,
                scenario_id,
                scenario_hash,
                scenario_revision,
                framework,
                content_version,
                technique_id,
                applicability,
                rationale,
                evidence_refs_json,
                origin,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "map-fixture",
                "scenario-fixture",
                "b" * 64,
                1,
                "mitre-atlas",
                "2026.07",
                "AML.T0001",
                "hypothesized",
                "Fixture mapping rationale.",
                "[]",
                "manual",
                "2026-08-30T00:00:00Z",
            ),
        )

    payload = server.build_payload()
    # 2 events + 2 pending risks + 1 needs-update risk + 4 proposal kinds.
    assert payload["meta"]["pending_review"] == 9


def test_channel_explanation_uses_research_digest(review_settings):
    from contingency_atlas.research import store_research_memo
    pid = _add_channel_proposal(review_settings)
    store_research_memo(
        review_settings.scenario_db_path, "channel", pid, "fake-judge",
        {"verdict": "enough", "note": "found corroboration", "coverage": [],
         "findings": [{"url": "https://news.stanford.edu/talk", "relevant": True,
                       "supports": ["mechanism"], "refutes": [],
                       "note": "on-topic"}],
         "candidates": [], "searches": [], "pages_fetched": 1, "iterations": 1,
         "seed_domains": [], "errors": []},
    )

    class CapturingClient(ExplainClient):
        def __init__(self):
            super().__init__()
            self.prompts = []

        def chat_json(self, messages, model=None, temperature=0.0):
            self.prompts.append(messages[-1]["content"])
            return super().chat_json(messages, model=model, temperature=temperature)

    client = CapturingClient()
    result = run_review_analysis(
        review_settings, client, kind="channel", record_id=pid
    )
    assert result["analyzed"] == 1
    # the memo's findings ground the explanation prompt
    assert "https://news.stanford.edu/talk" in client.prompts[-1]
    suggestion = load_suggestions(review_settings.scenario_db_path)[("channel", pid)]
    assert suggestion["recommendation"] is None


# ── wiring ───────────────────────────────────────────────────────────────


def test_review_analyze_is_a_valid_runner_phase():
    assert "review_analyze" in runner.VALID_PHASES


def test_index_has_review_view():
    html = (ROOT / "contingency_atlas/web/static/index.html").read_text()
    assert 'id="view-review"' in html
    assert 'data-view="review"' in html
    assert "/api/review/pending" in html
    assert "/api/review/decide" in html


def test_index_has_batch_review_selection_controls_and_partial_result_wiring():
    html = (ROOT / "contingency_atlas/web/static/index.html").read_text()
    controls = (
        "review-select-all",
        "review-batch-panel",
        "review-batch-count",
        "review-batch-reviewer",
        "review-batch-note",
        "review-batch-att-public",
        "review-batch-att-rights",
        "review-batch-approve",
        "review-batch-reject",
        "review-batch-status",
    )
    for control in controls:
        assert f'id="{control}"' in html

    # Keep the checkbox beside the card button: nesting one interactive
    # control inside another makes the long queue inaccessible by keyboard.
    assert re.search(
        r'<label class="rv-select"[^>]*>.*?data-rv-select=.*?</label>\s*'
        r'<button class="rv-item',
        html,
        re.DOTALL,
    )
    assert "reviewState.checked" in html
    assert "reviewState.fetchPromise" in html
    assert "mutateReviewBatchSelection" in html
    assert "clearReviewBatchAttestations" in html
    assert "selectAll.indeterminate" in html
    assert "checkbox.disabled = busy" in html
    assert "if (reviewState.batchRunning) return;" in html
    assert "!it.report || !it.report.approvable" in html
    # Batch decisions confirm in-page: a native dialog freezes the page and
    # any automation driving it. Any edit to the batch must disarm it.
    assert "window.confirm" not in html
    assert "reviewState.batchArmed = { action, signature }" in html
    assert "armed.signature !== rvBatchSignature(armed.action, selected)" in html
    assert 'fetch("/api/review/decide-batch"' in html
    assert "items: selected.map" in html
    assert "action, note, reason: note, reviewer" in html

    # Fetch treats HTTP 207 as `ok`; item results, rather than only the
    # top-level flag, must drive selection cleanup after a partial batch.
    assert "resp.status !== 207" in html
    assert "results.filter(row => row.ok)" in html
    assert "results.filter(row => !row.ok)" in html
    assert "renderReview(true, true)" in html
    assert 'id="review-jobline" role="status" aria-live="polite"' in html
    assert re.search(
        r"for \(const row of succeeded\)\s+reviewState\.checked\.delete",
        html,
    )


def test_index_has_findings_panel():
    html = (ROOT / "contingency_atlas/web/static/index.html").read_text()
    assert 'id="findings-list"' in html
    assert "DATA.patterns" in html and "DATA.causal" in html
    # long mechanism prose renders as collapsed per-sentence blocks
    assert "findingBody(" in html
    assert "splitSentences" in html
    assert "collapsible-extra" in html
    assert "finding-toggle" in html


def test_eval_reports_reads_artifacts_and_tolerates_absence(tmp_path, monkeypatch):
    from contingency_atlas.web import server as srv
    art = tmp_path / "eval_report.json"
    art.write_text(json.dumps({"recommendation": "go", "task_b": {"pass": True}}))
    monkeypatch.setattr(srv, "EVAL_ARTIFACTS", {
        "crash_test": art,
        "parallel_worlds": tmp_path / "missing.json",
        "ablations": tmp_path / "missing2.json",
        "judgment": tmp_path / "missing3.json",
    })
    reports = srv.build_eval_reports()
    assert reports["crash_test"]["report"]["recommendation"] == "go"
    assert reports["crash_test"]["modified_at"]
    assert reports["parallel_worlds"] is None
    assert reports["judgment"] is None


def test_index_has_eval_reports_panel():
    html = (ROOT / "contingency_atlas/web/static/index.html").read_text()
    assert 'id="eval-reports-list"' in html
    assert "/api/eval/reports" in html
    assert "ADVISORY" in html.upper()


def test_index_inline_scripts_parse_with_node(tmp_path):
    """Syntax-check every inline <script> body with `node --check`.

    A bad insert once split renderPipeline and left an orphaned fragment +
    stray brace (bb5f8f9): the browser discarded the entire script and the
    GUI hung at the boot overlay ("initializing…") with no server-side
    symptom. Marker tests above cannot catch that class; a parser can.
    Skips when node is not installed.
    """
    import re
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available for JS syntax check")
    html = (ROOT / "contingency_atlas/web/static/index.html").read_text()
    scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
    assert scripts, "expected inline <script> blocks in index.html"
    for i, body in enumerate(scripts):
        js = tmp_path / f"inline-{i}.js"
        js.write_text(body)
        proc = subprocess.run(
            [node, "--check", str(js)], capture_output=True, text=True
        )
        assert proc.returncode == 0, (
            f"inline script {i} failed node --check:\n{proc.stderr[:2000]}"
        )
