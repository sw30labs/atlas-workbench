"""P8 semantic atom-identity hygiene: dedup ranker, merge application,
proposal store, review decision functions, and the web routing.

Covers the wave-4 P8 deliverable end to end with no live LLM: the
candidate ranker is deterministic, the register merge runs under the
register lock with provenance stamps, proposal decisions go through the
review.py functions only (ADR-013/ADR-014), and the advisory verdict is
stored but never required.
"""

from __future__ import annotations

import http.client
import json
import threading
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from workbench_atlas.config import Settings
from workbench_atlas.dedup import MergeError, apply_merge, candidate_duplicates
from workbench_atlas.proposals import (
    acceptance_stats,
    advise_merge,
    create_merge_proposals,
    get_proposal,
    list_proposals,
    migrate_proposals,
    record_advisory,
)
from workbench_atlas.review import ReviewError, approve_merge, reject_merge
from workbench_atlas.schema import migrate
from workbench_atlas.web import server
from workbench_atlas.web.server import DashboardHandler


def _risk(issue, description, refs, first_seen="", status="approved", **extra):
    record = {
        "issue": issue,
        "description": description,
        "evidence_refs": refs,
        "first_seen": first_seen,
        "review_status": status,
        "source_type": "public_research",
        "provenance_note": "fixture",
    }
    record.update(extra)
    return record


@pytest.fixture
def risks():
    """A near-duplicate pair (A/B), a weakly overlapping record (C), and
    an unrelated record (D)."""
    return [
        _risk(
            "Cloud concentration across hyperscale regions",
            "Payments workloads depend on a small number of cloud regions.",
            ["EVT-1"], first_seen="2025-01-15",
        ),
        _risk(
            "Cloud concentration in hyperscale regions",
            "Payment processing depends on few cloud regions.",
            ["EVT-2"], first_seen="2025-02-01",
        ),
        _risk(
            "Cloud payment processing outage exposure",
            "Cloud region failures interrupt payment processing.",
            ["EVT-9"], first_seen="2025-04-01",
        ),
        _risk(
            "Sovereign export controls on accelerators",
            "Export-control regimes restrict chip supply.",
            ["EVT-3"], first_seen="2025-03-01",
        ),
    ]


# ── candidate_duplicates ─────────────────────────────────────────────────


def test_near_duplicate_ranks_above_distinct(risks):
    pairs = candidate_duplicates(risks, threshold=0.05)
    assert pairs, "expected at least the near-duplicate pair"
    top = pairs[0]
    assert {top["risk_id_a"], top["risk_id_b"]} == {
        "cloud-concentration-across-hyperscale-regions",
        "cloud-concentration-in-hyperscale-regions",
    }
    # ranked by descending similarity
    sims = [p["similarity"] for p in pairs]
    assert sims == sorted(sims, reverse=True)
    # components are exposed for the reviewer
    assert set(top["components"]) >= {
        "title_jaccard", "text_jaccard", "shared_tokens",
    }


def test_no_self_pairs_and_unique_pairs(risks):
    pairs = candidate_duplicates(risks, threshold=0.0)
    seen = set()
    for p in pairs:
        assert p["similarity"] > 0.0
        assert p["risk_id_a"] != p["risk_id_b"]
        key = tuple(sorted((p["risk_id_a"], p["risk_id_b"])))
        assert key not in seen
        seen.add(key)


def test_threshold_respected(risks):
    high = candidate_duplicates(risks, threshold=0.99)
    assert high == []
    for p in candidate_duplicates(risks, threshold=0.3):
        assert p["similarity"] >= 0.3


def test_candidate_duplicates_is_deterministic(risks):
    assert candidate_duplicates(risks, threshold=0.05) == candidate_duplicates(
        risks, threshold=0.05
    )


def test_retired_records_are_not_candidates(risks):
    retired = _risk(
        "Cloud concentration in hyperscale regions",
        "Payment processing depends on few cloud regions.",
        ["EVT-2"],
    )
    retired["merged_into"] = "Cloud concentration across hyperscale regions"
    pool = [risks[0], retired]
    assert candidate_duplicates(pool, threshold=0.0) == []


# ── apply_merge ──────────────────────────────────────────────────────────


def test_apply_merge_unions_and_retires(tmp_path, risks):
    path = tmp_path / "emerging_risks.json"
    path.write_text(json.dumps(risks[:2]))
    diff = apply_merge(
        path,
        "Cloud concentration across hyperscale regions",
        "Cloud concentration in hyperscale regions",
        reviewer="alice",
        reason="same underlying dependency",
    )
    assert diff["status"] == "merged"
    assert diff["before"]["keep"]["evidence_refs"] == ["EVT-1"]
    assert diff["before"]["drop"]["evidence_refs"] == ["EVT-2"]

    after_keep = diff["after"]["keep"]
    assert after_keep["evidence_refs"] == ["EVT-1", "EVT-2"]  # union
    assert after_keep["first_seen"] == "2025-01-15"           # earliest
    assert after_keep["last_reviewed"] >= "2026"              # merge stamp
    assert "absorbed duplicate" in after_keep["provenance_note"]
    assert "alice" in after_keep["provenance_note"]

    after_drop = diff["after"]["drop"]
    assert after_drop["review_status"] == "superseded"
    assert after_drop["merged_into"] == "Cloud concentration across hyperscale regions"
    assert after_drop["merged_by"] == "alice"
    assert after_drop["merged_at"]
    assert "same underlying dependency" in after_drop["merge_reason"]
    assert "retired as a duplicate" in after_drop["provenance_note"]

    saved = json.loads(path.read_text())
    assert len(saved) == 2  # retired record kept, never deleted
    by_issue = {r["issue"]: r for r in saved}
    assert by_issue[after_keep["issue"]]["evidence_refs"] == ["EVT-1", "EVT-2"]
    assert by_issue[after_drop["issue"]]["review_status"] == "superseded"


def test_apply_merge_is_idempotent_noop(tmp_path, risks):
    path = tmp_path / "emerging_risks.json"
    path.write_text(json.dumps(risks[:2]))
    first = apply_merge(
        path, risks[0]["issue"], risks[1]["issue"],
        reviewer="alice", reason="dup",
    )
    assert first["status"] == "merged"
    after_first = path.read_text()
    second = apply_merge(
        path, risks[0]["issue"], risks[1]["issue"],
        reviewer="bob", reason="trying again",
    )
    assert second["status"] == "already_merged"
    assert path.read_text() == after_first  # recorded no-op, no re-mutation


def test_apply_merge_requires_reviewer_and_reason(tmp_path, risks):
    path = tmp_path / "emerging_risks.json"
    path.write_text(json.dumps(risks[:2]))
    with pytest.raises(MergeError, match="reviewer"):
        apply_merge(path, risks[0]["issue"], risks[1]["issue"],
                    reviewer=" ", reason="dup")
    with pytest.raises(MergeError, match="reason"):
        apply_merge(path, risks[0]["issue"], risks[1]["issue"],
                    reviewer="alice", reason="")
    with pytest.raises(MergeError, match="itself"):
        apply_merge(path, risks[0]["issue"], risks[0]["issue"],
                    reviewer="alice", reason="dup")


# ── proposal store ───────────────────────────────────────────────────────


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "atlas.db"
    migrate(str(path))
    return path


@pytest.fixture
def proposals_db(db_path, risks):
    candidates = candidate_duplicates(risks, threshold=0.3)
    assert len(candidates) == 1
    create_merge_proposals(db_path, candidates)
    return candidates


def test_migrate_proposals_idempotent(db_path):
    migrate_proposals(db_path)
    migrate_proposals(db_path)  # second call must not raise
    assert list_proposals(db_path) == []


def test_create_dedupes_against_pending(db_path, risks):
    candidates = candidate_duplicates(risks, threshold=0.3)
    first = create_merge_proposals(db_path, candidates)
    assert first["created"] == 1
    second = create_merge_proposals(db_path, candidates)
    assert second["created"] == 0
    assert second["skipped_existing_pending"] == 1
    # flipped orientation is the same proposal
    flipped = [dict(c, risk_id_a=c["risk_id_b"], risk_id_b=c["risk_id_a"])
               for c in candidates]
    assert create_merge_proposals(db_path, flipped)["created"] == 0


def test_list_proposals_filters_status(db_path, proposals_db):
    pending = list_proposals(db_path, status="pending")
    assert len(pending) == 1
    row = pending[0]
    assert row["status"] == "pending"
    assert row["components"]["title_jaccard"] is not None
    assert row["merge_diff"] is None
    assert list_proposals(db_path, status="approved") == []


# ── advisory verdict (mock client; stored, never required) ───────────────


class _JudgeSettings:
    llm_model_judge = "fake-judge"


class FakeMergeAdvisor:
    settings = _JudgeSettings()

    def __init__(self, verdict="same_risk"):
        self.verdict = verdict
        self.calls = 0

    def chat_json(self, messages, model=None, temperature=0.0):
        self.calls += 1
        system = " ".join(messages[0]["content"].lower().split())
        assert "you do not decide anything" in system
        return {
            "verdict": self.verdict,
            "confidence": 0.8,
            "rationale": "same underlying dependency",
            "evidence_quotes": ["cloud regions"],
            "human_must_verify": ["scope judgment"],
        }


def test_advise_merge_stores_advisory(db_path, proposals_db):
    proposal = list_proposals(db_path, status="pending")[0]
    proposal["description_a"] = "desc a"
    proposal["description_b"] = "desc b"
    client = FakeMergeAdvisor()
    advisory = advise_merge(client, proposal)
    assert client.calls == 1
    assert advisory["verdict"] == "same_risk"
    assert advisory["model_id"] == "fake-judge"
    assert advisory["prompt_hash"]
    assert advisory["error"] is None
    record_advisory(db_path, proposal["proposal_id"], advisory)
    stored = get_proposal(db_path, proposal["proposal_id"])
    assert stored["advisory"]["verdict"] == "same_risk"
    assert stored["status"] == "pending"  # advisory never decides


def test_advise_merge_failure_is_recorded_not_raised(db_path, proposals_db):
    class BrokenClient:
        settings = _JudgeSettings()

        def chat_json(self, messages, model=None, temperature=0.0):
            raise RuntimeError("endpoint down")

    proposal = list_proposals(db_path, status="pending")[0]
    advisory = advise_merge(BrokenClient(), proposal)
    assert advisory["verdict"] is None
    assert "endpoint down" in advisory["error"]


# ── review decision functions ────────────────────────────────────────────


@pytest.fixture
def merge_setup(tmp_path, risks, db_path, proposals_db):
    risks_path = tmp_path / "emerging_risks.json"
    risks_path.write_text(json.dumps(risks))
    return risks_path, db_path


def test_approve_merge_applies_under_lock(merge_setup):
    risks_path, db_path = merge_setup
    proposal = list_proposals(db_path, status="pending")[0]
    out = approve_merge(
        db_path, risks_path, proposal["proposal_id"],
        reviewer="alice", reason="same underlying dependency",
    )
    assert out["decision"] == "approved"
    diff = out["merge"]
    assert diff["status"] == "merged"

    saved = {r["issue"]: r for r in json.loads(risks_path.read_text())}
    keep = saved[diff["keep_title"]]
    drop = saved[diff["drop_title"]]
    assert sorted(keep["evidence_refs"]) == ["EVT-1", "EVT-2"]
    assert drop["review_status"] == "superseded"
    assert drop["merged_into"] == diff["keep_title"]
    assert "alice" in drop["provenance_note"]
    assert "alice" in keep["provenance_note"]

    row = get_proposal(db_path, proposal["proposal_id"])
    assert row["status"] == "approved"
    assert row["reviewer"] == "alice"
    assert row["reason"] == "same underlying dependency"
    assert row["decided_at"]
    assert row["merge_diff"]["status"] == "merged"
    assert row["merge_diff"]["before"]["drop"]["review_status"] == "approved"
    assert acceptance_stats(db_path) == {
        "approved": 1, "rejected": 0, "pending": 0,
        "acceptance_rate": 1.0, "kill_rule_threshold": 0.2,
    }


def test_approve_merge_requires_reviewer_and_reason(merge_setup):
    risks_path, db_path = merge_setup
    pid = list_proposals(db_path, status="pending")[0]["proposal_id"]
    with pytest.raises(ReviewError, match="reviewer"):
        approve_merge(db_path, risks_path, pid, reviewer="", reason="dup")
    with pytest.raises(ReviewError, match="reason"):
        approve_merge(db_path, risks_path, pid, reviewer="alice", reason=" ")
    # nothing was decided and the register is untouched
    assert get_proposal(db_path, pid)["status"] == "pending"


def test_second_approve_is_clean_error_not_corruption(merge_setup):
    risks_path, db_path = merge_setup
    pid = list_proposals(db_path, status="pending")[0]["proposal_id"]
    approve_merge(db_path, risks_path, pid, reviewer="alice", reason="dup")
    after = risks_path.read_text()
    with pytest.raises(ReviewError, match="no pending decision"):
        approve_merge(db_path, risks_path, pid, reviewer="bob", reason="again")
    assert risks_path.read_text() == after
    assert get_proposal(db_path, pid)["reviewer"] == "alice"


def test_reject_merge_records_without_touching_register(merge_setup):
    risks_path, db_path = merge_setup
    before = risks_path.read_text()
    pid = list_proposals(db_path, status="pending")[0]["proposal_id"]
    with pytest.raises(ReviewError, match="reason"):
        reject_merge(db_path, pid, reviewer="alice", reason="")
    out = reject_merge(
        db_path, pid, reviewer="alice", reason="distinct scopes",
    )
    assert out["decision"] == "rejected"
    assert risks_path.read_text() == before
    row = get_proposal(db_path, pid)
    assert row["status"] == "rejected"
    assert row["merge_diff"] is None
    stats = acceptance_stats(db_path)
    assert stats["rejected"] == 1 and stats["acceptance_rate"] == 0.0
    with pytest.raises(ReviewError, match="no pending decision"):
        reject_merge(db_path, pid, reviewer="alice", reason="again")


def test_approve_on_already_merged_pair_is_recorded_noop(merge_setup):
    """The register was merged by hand first; approving the proposal must
    be a recorded no-op, not an error and not a second mutation."""
    risks_path, db_path = merge_setup
    proposal = list_proposals(db_path, status="pending")[0]
    apply_merge(
        risks_path, proposal["title_a"], proposal["title_b"],
        reviewer="manual", reason="merged by hand",
    )
    after_manual = risks_path.read_text()
    out = approve_merge(
        db_path, risks_path, proposal["proposal_id"],
        reviewer="alice", reason="confirming the manual merge",
    )
    assert out["merge"]["status"] == "already_merged"
    assert risks_path.read_text() == after_manual
    assert get_proposal(db_path, proposal["proposal_id"])["status"] == "approved"


# ── web: review queue + decide routing ───────────────────────────────────


@contextmanager
def _dashboard():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), DashboardHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd.server_address[1]
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def _request(port, method, path, body=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        payload = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if payload else {}
        connection.request(method, path, body=payload, headers=headers)
        response = connection.getresponse()
        return response.status, json.loads(response.read() or b"{}")
    finally:
        connection.close()


@pytest.fixture
def web_setup(merge_setup, monkeypatch):
    risks_path, db_path = merge_setup
    settings = Settings(
        scenario_db_path=db_path,
        evidence_db_path=db_path.parent / "event_evidence.db",
        emerging_risks_path=risks_path,
    )
    monkeypatch.setattr(server, "get_settings", lambda: settings)
    return settings


def test_review_queue_includes_merge_section(web_setup):
    queue = server.build_review_queue()
    section = queue["merge_proposals"]
    assert len(section["pending"]) == 1
    entry = section["pending"][0]
    assert entry["kind"] == "merge"
    assert entry["record_id"].startswith("mp-")
    assert entry["report"]["approvable"] is True
    assert "title_a" in entry["record"] and "title_b" in entry["record"]
    acceptance = section["acceptance"]
    assert acceptance["approved"] == 0 and acceptance["rejected"] == 0
    assert acceptance["acceptance_rate"] is None


def test_decide_endpoint_routes_merge_approve(web_setup):
    pid = list_proposals(web_setup.scenario_db_path, status="pending")[0][
        "proposal_id"
    ]
    with _dashboard() as port:
        status, payload = _request(port, "POST", "/api/review/decide", {
            "kind": "merge", "record_id": pid, "action": "approve",
            "reviewer": "alice", "reason": "same dependency",
        })
    assert status == 200 and payload["ok"] is True
    assert payload["record"]["decision"] == "approved"
    saved = {r["issue"]: r for r in json.loads(
        web_setup.emerging_risks_path.read_text()
    )}
    merged = [r for r in saved.values() if r.get("review_status") == "superseded"]
    assert len(merged) == 1
    assert merged[0]["merged_by"] == "alice"
    queue = server.build_review_queue()
    assert queue["merge_proposals"]["pending"] == []
    assert queue["merge_proposals"]["acceptance"]["acceptance_rate"] == 1.0


def test_decide_endpoint_merge_requires_reviewer_and_reason(web_setup):
    pid = list_proposals(web_setup.scenario_db_path, status="pending")[0][
        "proposal_id"
    ]
    with _dashboard() as port:
        status, payload = _request(port, "POST", "/api/review/decide", {
            "kind": "merge", "record_id": pid, "action": "approve",
            "reviewer": "", "reason": "same dependency",
        })
        assert status == 400 and "reviewer" in payload["error"].lower()
        status, payload = _request(port, "POST", "/api/review/decide", {
            "kind": "merge", "record_id": pid, "action": "approve",
            "reviewer": "alice", "reason": "",
        })
        assert status == 400 and "reason" in payload["error"].lower()


def test_decide_endpoint_routes_merge_reject(web_setup):
    pid = list_proposals(web_setup.scenario_db_path, status="pending")[0][
        "proposal_id"
    ]
    before = web_setup.emerging_risks_path.read_text()
    with _dashboard() as port:
        status, payload = _request(port, "POST", "/api/review/decide", {
            "kind": "merge", "record_id": pid, "action": "reject",
            "reviewer": "alice", "reason": "distinct scopes",
        })
    assert status == 200 and payload["record"]["decision"] == "rejected"
    assert web_setup.emerging_risks_path.read_text() == before
    stats = acceptance_stats(web_setup.scenario_db_path)
    assert stats["rejected"] == 1 and stats["acceptance_rate"] == 0.0
