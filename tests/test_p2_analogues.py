"""P2 analogue dossiers, display-only lane (wave 5).

Covers the append-only analogues store (intake discipline, dedup, the
pair_id-keyed query wave 6 rides on), the review decision functions
(approval validation: mechanism-mapped + cited + disanalogies; claim
fields immutable through the decision path), the MockClient-driven
elicitor with its citation clamp, the contamination canary, and the web
surfacing (queue section, decide routing, compound dossiers). No live
LLM, no network: clients and citation resolvers are injected mocks.
"""

from __future__ import annotations

import http.client
import json
import sqlite3
import threading
from contextlib import contextmanager
from http.server import ThreadingHTTPServer

import pytest

from contingency_atlas.compound_join import migrate_compound
from contingency_atlas.config import Settings
from contingency_atlas.proposals import (
    create_analogues,
    get_analogue,
    list_analogues,
    migrate_proposals,
)
from contingency_atlas.review import ReviewError, approve_analogue, reject_analogue
from contingency_atlas.scoring.elicitation import (
    contamination_canary,
    elicit_analogues,
)
from contingency_atlas.scoring.models import RiskAtom, pair_id_for
from contingency_atlas.web import server
from contingency_atlas.web.server import DashboardHandler


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "atlas.db"
    migrate_proposals(path)
    return path


def _candidate(**overrides):
    cand = {
        "pair_id": pair_id_for(["risk-a", "risk-b"]),
        "risk_ids": ["risk-a", "risk-b"],
        "horizon": "1y",
        "compound_def": "all_must_fire",
        "event": "Knight Capital deployment cascade",
        "period": "2012-08",
        "mechanism_map": {"channel": "model", "risk-a": "deployment pipeline",
                          "risk-b": "permissioning failure"},
        "co_fired": True,
        "outcome_class": "q95-band-3",
        "citations": ["https://example.com/sec/knight"],
        "disanalogies": ["single firm, not a shared utility"],
        "as_of_check": "citation dates precede cutoff",
        "elicitor": {"model_id": "mock-model", "prompt_hash": "ab" * 32},
        "elicited_state_hash": "cd" * 32,
        "graph_version": None,
    }
    cand.update(overrides)
    return cand


# ── store: intake discipline, dedup, pair_id query ────────────────────────


def test_create_and_list_by_pair_id(db_path):
    out = create_analogues(db_path, [_candidate()])
    assert out["created"] == 1
    assert out["analogue_ids"][0].startswith("ana-")
    rows = list_analogues(db_path, status="pending")
    assert len(rows) == 1
    row = rows[0]
    assert row["event"] == "Knight Capital deployment cascade"
    assert row["co_fired"] is True
    assert row["mechanism_map"]["channel"] == "model"
    assert row["elicitor"]["model_id"] == "mock-model"
    assert row["elicited_state_hash"] == "cd" * 32
    assert row["graph_version"] is None
    # the wave-6 query: approved analogues by stable pair_id
    by_pair = list_analogues(db_path, pair_id=row["pair_id"])
    assert [r["analogue_id"] for r in by_pair] == [row["analogue_id"]]
    assert list_analogues(db_path, pair_id="pair-doesnotexist") == []


def test_intake_requires_disanalogies_atomically(db_path):
    with pytest.raises(ValueError, match="disanalogies"):
        create_analogues(db_path, [_candidate(disanalogies=[])])
    with pytest.raises(ValueError, match="disanalogies"):
        create_analogues(db_path, [_candidate(disanalogies=["  "])])
    # batch is atomic: one invalid candidate sinks nothing valid with it
    with pytest.raises(ValueError, match="disanalogies"):
        create_analogues(db_path, [_candidate(), _candidate(disanalogies=[])])
    assert list_analogues(db_path) == []


def test_intake_requires_identity(db_path):
    with pytest.raises(ValueError, match="pair_id"):
        create_analogues(db_path, [_candidate(pair_id="")])
    with pytest.raises(ValueError, match="pair_id"):
        create_analogues(db_path, [_candidate(risk_ids=[])])


def test_dedup_against_pending_same_event(db_path):
    create_analogues(db_path, [_candidate()])
    again = create_analogues(db_path, [_candidate()])
    assert again["created"] == 0
    assert again["skipped_existing_pending"] == 1
    # a different event for the same pair is a different claim
    other = create_analogues(db_path, [_candidate(event="Flash Crash")])
    assert other["created"] == 1


# ── review decision functions ─────────────────────────────────────────────


@pytest.fixture
def pending_analogue(db_path):
    create_analogues(db_path, [_candidate()])
    return list_analogues(db_path, status="pending")[0]["analogue_id"]


def _insert_raw(db_path, **fields):
    """Direct INSERT (test-only) for claim shapes intake would refuse —
    approval validation must still gate them."""
    row = {
        "analogue_id": "ana-rawtest0001",
        "pair_id": pair_id_for(["risk-a", "risk-b"]),
        "risk_ids": json.dumps(["risk-a", "risk-b"]),
        "horizon": "1y",
        "compound_def": "all_must_fire",
        "event": "Raw event",
        "period": "2001",
        "mechanism_map": json.dumps({"channel": "model"}),
        "co_fired": 1,
        "outcome_class": "q95-band-1",
        "citations": json.dumps(["https://example.com/x"]),
        "disanalogies": json.dumps(["a difference"]),
        "as_of_check": "",
        "elicitor": None,
        "elicited_state_hash": None,
        "graph_version": None,
        "status": "pending",
        "created_at": "2026-08-03T00:00:00+00:00",
    }
    row.update(fields)
    conn = sqlite3.connect(db_path)
    cols = ", ".join(row)
    conn.execute(
        f"INSERT INTO analogues ({cols}) VALUES ({', '.join('?' * len(row))})",
        tuple(row.values()),
    )
    conn.commit()
    conn.close()
    return row["analogue_id"]


def test_approve_requires_reviewer_and_reason(db_path, pending_analogue):
    with pytest.raises(ReviewError, match="reviewer"):
        approve_analogue(db_path, pending_analogue, reviewer="", reason="ok")
    with pytest.raises(ReviewError, match="reason"):
        approve_analogue(db_path, pending_analogue, reviewer="alice", reason="")
    assert get_analogue(db_path, pending_analogue)["status"] == "pending"


def test_approve_sets_decision_fields(db_path, pending_analogue):
    out = approve_analogue(
        db_path, pending_analogue, reviewer="alice", reason="mechanism maps cleanly"
    )
    assert out["decision"] == "approved"
    row = get_analogue(db_path, pending_analogue)
    assert row["status"] == "approved"
    assert row["reviewer"] == "alice"
    assert row["reason"] == "mechanism maps cleanly"
    assert row["decided_at"]


def test_approval_validation_gates_claim_quality(db_path):
    no_mechanism = _insert_raw(db_path, analogue_id="ana-rawtest0002",
                               mechanism_map=json.dumps({}))
    no_citations = _insert_raw(db_path, analogue_id="ana-rawtest0003",
                               citations=json.dumps([]))
    no_disanalogies = _insert_raw(db_path, analogue_id="ana-rawtest0004",
                                  disanalogies=json.dumps([]))
    for aid, match in (
        (no_mechanism, "mechanism map"),
        (no_citations, "citation"),
        (no_disanalogies, "disanalogies"),
    ):
        with pytest.raises(ReviewError, match=match):
            approve_analogue(db_path, aid, reviewer="alice", reason="trying")
        assert get_analogue(db_path, aid)["status"] == "pending"
    # rejection needs no claim validation — only the written reason
    out = reject_analogue(db_path, no_mechanism, reviewer="alice",
                          reason="no mechanism, no dossier")
    assert out["decision"] == "rejected"


def test_decision_leaves_claim_fields_untouched(db_path, pending_analogue):
    before = get_analogue(db_path, pending_analogue)
    approve_analogue(db_path, pending_analogue, reviewer="alice", reason="ok")
    after = get_analogue(db_path, pending_analogue)
    claim_fields = (
        "pair_id", "risk_ids", "horizon", "compound_def", "event", "period",
        "mechanism_map", "co_fired", "outcome_class", "citations",
        "disanalogies", "as_of_check", "elicitor", "elicited_state_hash",
        "graph_version", "created_at",
    )
    for field in claim_fields:
        assert after[field] == before[field], field
    # exactly the decision fields changed, nothing else
    changed = {f for f in before if before[f] != after[f]}
    assert changed == {"status", "reviewer", "reason", "decided_at"}


def test_double_decide_raises(db_path, pending_analogue):
    approve_analogue(db_path, pending_analogue, reviewer="alice", reason="ok")
    with pytest.raises(ReviewError, match="no pending decision"):
        reject_analogue(db_path, pending_analogue, reviewer="bob", reason="again")
    with pytest.raises(ReviewError, match="not found"):
        approve_analogue(db_path, "ana-doesnotexist", reviewer="bob", reason="ghost")


# ── elicitation: mock-client analogue elicitor ────────────────────────────


class _ElicitClient:
    """OMLX-shaped mock returning a canned elicitation batch."""

    def __init__(self, payload=None, error=None):
        self.settings = Settings()
        self._payload = payload
        self._error = error

    def chat_json(self, messages, **kwargs):
        if self._error:
            raise RuntimeError(self._error)
        return self._payload if self._payload is not None else {
            "analogues": [
                {"event": "Knight Capital deployment cascade", "period": "2012-08",
                 "mechanism_map": {"channel": "model", "risk-a": "pipeline"},
                 "co_fired": True, "outcome_class": "q95-band-3",
                 "citations": ["https://example.com/knight"],
                 "disanalogies": ["single firm"],
                 "as_of_check": "dates precede cutoff"},
                {"event": "No disanalogies event", "period": "2001",
                 "mechanism_map": {"channel": "model"}, "co_fired": True,
                 "outcome_class": "x", "citations": ["https://example.com/a"],
                 "disanalogies": []},
                {"event": "Dead citation event", "period": "2002",
                 "mechanism_map": {"channel": "model"}, "co_fired": True,
                 "outcome_class": "y", "citations": ["https://dead.example/404"],
                 "disanalogies": ["d"]},
                {"event": "Non co-fired episode", "period": "2010",
                 "mechanism_map": {"channel": "cloud"}, "co_fired": False,
                 "outcome_class": "near-miss",
                 "citations": ["https://example.com/b"],
                 "disanalogies": ["different trigger"]},
            ]
        }


@pytest.fixture
def compound_row():
    return {
        "pair_id": pair_id_for(["risk-a", "risk-b"]),
        "risk_ids": ["risk-a", "risk-b"],
        "state_hash": "ab" * 32,
        "atom_titles": {"risk-a": "Alpha cloud concentration",
                        "risk-b": "Beta identity sprawl"},
        "p_hat": 0.12,
        "uncertainty_class": "mixed",
    }


def _resolver(url):
    if "dead.example" in url:
        return False
    return True


def test_elicit_analogues_well_formed_rows(compound_row):
    rows = elicit_analogues([compound_row], _ElicitClient(), resolver=_resolver)
    events = [r["event"] for r in rows]
    # mandatory disanalogies and the citation clamp each killed a candidate
    assert events == ["Knight Capital deployment cascade", "Non co-fired episode"]
    for row in rows:
        assert row["status"] == "pending"
        assert row["pair_id"] == compound_row["pair_id"]
        assert row["risk_ids"] == ["risk-a", "risk-b"]
        assert row["disanalogies"]
        assert row["elicitor"]["prompt_version"] == "analogue-elicitation-v1"
        assert row["elicitor"]["prompt_hash"]
        assert row["elicitor"]["model_id"]
        assert row["elicited_state_hash"] == "ab" * 32  # metadata only
        assert row["graph_version"] is None
        assert row["error"] is None


def test_elicit_analogues_respects_k(compound_row):
    rows = elicit_analogues([compound_row], _ElicitClient(), k=1, resolver=_resolver)
    assert len(rows) == 1
    assert rows[0]["event"] == "Knight Capital deployment cascade"


def test_elicit_analogues_unresolvable_citation_drops_candidate(compound_row):
    rows = elicit_analogues(
        [compound_row], _ElicitClient(), resolver=lambda url: False
    )
    assert rows == []  # every citation dead -> no rows at all


def test_elicit_analogues_error_recorded_not_raised(compound_row):
    rows = elicit_analogues([compound_row], _ElicitClient(error="endpoint down"))
    assert len(rows) == 1
    assert "endpoint down" in rows[0]["error"]
    rows = elicit_analogues([compound_row], _ElicitClient(payload=["not", "a", "dict"]))
    assert "non-object" in rows[0]["error"]


def test_contamination_canary_measures_confabulation_without_writes(db_path):
    fictional = [
        RiskAtom(risk_id="fic-alpha", title="Fictional Alpha", channels=["cloud"]),
        RiskAtom(risk_id="fic-beta", title="Fictional Beta", channels=["model"]),
    ]
    out = contamination_canary(fictional, _ElicitClient(), resolver=_resolver)
    assert out["batch_id"].startswith("canary-")
    # one fictional pair; the mock confabulates two well-formed analogues,
    # one of them confidently (co-fired + mechanism + citation)
    assert out["n_elicited"] == 2
    assert out["n_confident"] == 1
    assert out["canary_rate"] == 0.5
    assert len(out["rows"]) == 2
    # report artifact only — NOTHING is stored
    assert list_analogues(db_path) == []


# ── web: queue section, decide routing, compound dossiers ────────────────


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
def web_setup(tmp_path, monkeypatch, db_path):
    risks_path = tmp_path / "emerging_risks.json"
    risks_path.write_text("[]")
    create_analogues(db_path, [_candidate()])
    settings = Settings(
        scenario_db_path=db_path,
        evidence_db_path=tmp_path / "event_evidence.db",
        emerging_risks_path=risks_path,
    )
    monkeypatch.setattr(server, "get_settings", lambda: settings)
    return settings


def test_review_queue_includes_analogue_section(web_setup):
    queue = server.build_review_queue()
    pending = queue["analogue_proposals"]["pending"]
    assert len(pending) == 1
    entry = pending[0]
    assert entry["kind"] == "analogue"
    assert entry["record_id"].startswith("ana-")
    assert entry["report"]["approvable"] is True
    record = entry["record"]
    assert record["event"] == "Knight Capital deployment cascade"
    assert record["disanalogies"] == ["single firm, not a shared utility"]
    assert record["mechanism_map"]["channel"] == "model"
    labels = {c["label"] for c in entry["report"]["checks"]}
    assert {"Mechanism map", "Citations", "Disanalogies (mandatory)"} <= labels


def test_decide_endpoint_routes_analogue_approve(web_setup):
    aid = list_analogues(web_setup.scenario_db_path, status="pending")[0][
        "analogue_id"
    ]
    with _dashboard() as port:
        status, payload = _request(port, "POST", "/api/review/decide", {
            "kind": "analogue", "record_id": aid, "action": "approve",
            "reviewer": "alice", "reason": "mechanism maps cleanly",
        })
    assert status == 200 and payload["ok"] is True
    assert payload["record"]["decision"] == "approved"
    assert get_analogue(web_setup.scenario_db_path, aid)["status"] == "approved"
    queue = server.build_review_queue()
    assert queue["analogue_proposals"]["pending"] == []


def test_decide_endpoint_analogue_requires_reviewer_reason_and_valid_claim(web_setup):
    aid = list_analogues(web_setup.scenario_db_path, status="pending")[0][
        "analogue_id"
    ]
    with _dashboard() as port:
        status, payload = _request(port, "POST", "/api/review/decide", {
            "kind": "analogue", "record_id": aid, "action": "approve",
            "reviewer": "", "reason": "x",
        })
        assert status == 400 and "reviewer" in payload["error"].lower()
        status, payload = _request(port, "POST", "/api/review/decide", {
            "kind": "analogue", "record_id": aid, "action": "approve",
            "reviewer": "alice", "reason": "",
        })
        assert status == 400 and "reason" in payload["error"].lower()
        status, payload = _request(port, "POST", "/api/review/decide", {
            "kind": "analogue", "record_id": aid, "action": "reject",
            "reviewer": "alice", "reason": "over-claimed analogy",
        })
        assert status == 200
        assert payload["record"]["decision"] == "rejected"


@pytest.fixture
def compound_web_setup(tmp_path, monkeypatch, db_path):
    """A compound_scenarios row plus one approved and one pending analogue."""
    migrate_compound(str(db_path))
    pair = ["risk-a", "risk-b"]
    pair_id = pair_id_for(pair)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO compound_scenarios (compound_id, risk_ids, state_hash,"
        " p_hat, p_lo, p_hi, uncertainty_class, dependence_support,"
        " evidence_support, data_regime, status, run_id, scored_at,"
        " algorithm_version, provenance_note, pair_id, loss_units)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("scn-test", json.dumps(pair), "hash1", 0.4, 0.2, 0.6, "mixed", 4,
         0.7, "R1", "candidate", "run-1", "2026-08-01T00:00:00+00:00",
         "0.2.0", "test", pair_id, "dollars"),
    )
    conn.commit()
    conn.close()
    create_analogues(db_path, [
        _candidate(pair_id=pair_id, event="Approved analogue event"),
        _candidate(pair_id=pair_id, event="Pending analogue event"),
        _candidate(pair_id=pair_id_for(["risk-x", "risk-y"]),
                   risk_ids=["risk-x", "risk-y"], event="Other pair event"),
    ])
    rows = {r["event"]: r for r in list_analogues(db_path, status="pending")}
    approve_analogue(
        db_path, rows["Approved analogue event"]["analogue_id"],
        reviewer="alice", reason="solid mechanism map",
    )
    approve_analogue(
        db_path, rows["Other pair event"]["analogue_id"],
        reviewer="alice", reason="also solid",
    )
    risks_path = tmp_path / "emerging_risks.json"
    risks_path.write_text("[]")
    settings = Settings(
        scenario_db_path=db_path,
        evidence_db_path=tmp_path / "event_evidence.db",
        emerging_risks_path=risks_path,
    )
    monkeypatch.setattr(server, "get_settings", lambda: settings)
    return settings


def test_compound_rows_carry_approved_dossiers_only(compound_web_setup):
    payload = server.build_payload()
    compounds = payload["compounds"]
    assert len(compounds) == 1
    dossiers = compounds[0]["dossiers"]
    # exactly the approved analogue for THIS pair_id: the pending one and
    # the other pair's approved one are excluded
    assert len(dossiers) == 1
    dossier = dossiers[0]
    assert dossier["event"] == "Approved analogue event"
    assert dossier["period"] == "2012-08"
    assert dossier["outcome_class"] == "q95-band-3"
    assert dossier["co_fired"] is True
    assert dossier["n_disanalogies"] == 1
    assert dossier["reviewer"] == "alice"
