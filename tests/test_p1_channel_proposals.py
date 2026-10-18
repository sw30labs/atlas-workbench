"""P1 channel-graph enrichment, shadow-only lane (wave 5).

Covers the channel_proposals store (create/dedup/citation-clamp statuses),
the review decision functions (the only write path — approval changes
nothing but the row), the MockClient-driven channel audit, and the join's
shadow pass with its graduation-freeze invariant: shadow scores NEVER land
in compound_scenarios, score_realizations, or the artifact's compounds
list. No live LLM, no network anywhere: resolvers are injected mocks.
"""

from __future__ import annotations

import http.client
import json
import sqlite3
import threading
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from contingency_atlas.compound_join import run_compound_join
from contingency_atlas.config import Settings
from contingency_atlas.proposals import (
    create_channel_proposals,
    get_channel_proposal,
    list_proposals,
    migrate_proposals,
)
from contingency_atlas.review import (
    ReviewError,
    approve_channel_proposal,
    reject_channel_proposal,
)
from contingency_atlas.scoring.elicitation import channel_audit, default_resolver
from contingency_atlas.scoring.inventory import load_risk_atoms
from contingency_atlas.scoring.models import RiskAtom, pair_id_for
from contingency_atlas.scoring.scorer import ScorerConfig
from contingency_atlas.web import server
from contingency_atlas.web.server import DashboardHandler

FAST = ScorerConfig(mc_draws=300, interval_draws=300)


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "atlas.db"
    migrate_proposals(path)
    return path


def _candidate(**overrides):
    cand = {
        "kind": "tag_add",
        "risk_ids": ["cloud-region-failover-congestion"],
        "channel": "third-party",
        "mechanism": "failover capacity is outsourced to the same operators",
        "citation_url": "https://example.com/postmortem",
        "citation_status": "resolved",
    }
    cand.update(overrides)
    return cand


# ── store: create / dedup / list ──────────────────────────────────────────


def test_migrate_is_idempotent(db_path):
    migrate_proposals(db_path)  # second run must not raise
    conn = sqlite3.connect(db_path)
    tables = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    conn.close()
    assert {"merge_proposals", "channel_proposals", "analogues"} <= tables


def test_create_and_dedup_against_pending(db_path):
    first = create_channel_proposals(db_path, [_candidate()])
    assert first["created"] == 1
    again = create_channel_proposals(db_path, [_candidate()])
    assert again["created"] == 0
    assert again["skipped_existing_pending"] == 1
    rows = list_proposals(db_path, kind="channel", status="pending")
    assert len(rows) == 1
    row = rows[0]
    assert row["proposal_id"].startswith("cp-")
    assert row["kind"] == "tag_add"
    assert row["risk_ids"] == ["cloud-region-failover-congestion"]
    assert row["status"] == "pending"
    assert row["graph_version_applied"] is None


def test_create_validates_kind_and_risk_ids(db_path):
    with pytest.raises(ValueError, match="kind"):
        create_channel_proposals(db_path, [_candidate(kind="nonsense")])
    with pytest.raises(ValueError, match="risk_id"):
        create_channel_proposals(db_path, [_candidate(risk_ids=[])])
    with pytest.raises(ValueError, match="citation_status"):
        create_channel_proposals(db_path, [_candidate(citation_status="maybe")])


def test_unresolvable_citation_is_auto_rejected_before_review(db_path):
    out = create_channel_proposals(
        db_path, [_candidate(citation_status="unresolvable")]
    )
    assert out["created"] == 1
    # never reaches the review queue
    assert list_proposals(db_path, kind="channel", status="pending") == []
    auto = list_proposals(db_path, kind="channel", status="auto_rejected")
    assert len(auto) == 1
    assert auto[0]["citation_status"] == "unresolvable"
    # an auto-rejected row does not block re-proposal with a fixed citation
    retry = create_channel_proposals(db_path, [_candidate(citation_status="resolved")])
    assert retry["created"] == 1


def test_list_proposals_unknown_kind_raises(db_path):
    with pytest.raises(ValueError, match="unknown proposal kind"):
        list_proposals(db_path, kind="nonsense")
    # merge kind keeps working on the same store
    assert list_proposals(db_path, kind="merge") == []


# ── review decision functions (the only write path) ──────────────────────


@pytest.fixture
def pending_proposal(db_path):
    create_channel_proposals(db_path, [_candidate()])
    return list_proposals(db_path, kind="channel", status="pending")[0][
        "proposal_id"
    ]


def test_approve_requires_reviewer_and_reason(db_path, pending_proposal):
    with pytest.raises(ReviewError, match="reviewer"):
        approve_channel_proposal(db_path, pending_proposal, reviewer="", reason="ok")
    with pytest.raises(ReviewError, match="reason"):
        approve_channel_proposal(
            db_path, pending_proposal, reviewer="alice", reason=" "
        )
    assert get_channel_proposal(db_path, pending_proposal)["status"] == "pending"


def test_approve_flips_only_the_row(db_path, pending_proposal):
    out = approve_channel_proposal(
        db_path, pending_proposal, reviewer="alice", reason="mechanism is documented"
    )
    assert out["decision"] == "approved"
    row = get_channel_proposal(db_path, pending_proposal)
    assert row["status"] == "approved"
    assert row["reviewer"] == "alice"
    assert row["reason"] == "mechanism is documented"
    assert row["decided_at"]
    # ADR-014(c): approval applies NOTHING — the graph-version stamp stays
    # NULL on this branch, forever.
    assert row["graph_version_applied"] is None


def test_reject_and_double_decide_raises(db_path, pending_proposal):
    out = reject_channel_proposal(
        db_path, pending_proposal, reviewer="alice", reason="mechanism is speculative"
    )
    assert out["decision"] == "rejected"
    assert get_channel_proposal(db_path, pending_proposal)["status"] == "rejected"
    with pytest.raises(ReviewError, match="no pending decision"):
        approve_channel_proposal(
            db_path, pending_proposal, reviewer="bob", reason="again"
        )
    with pytest.raises(ReviewError, match="not found"):
        reject_channel_proposal(
            db_path, "cp-doesnotexist", reviewer="bob", reason="ghost"
        )


# ── elicitation: channel audit with a mock client ─────────────────────────


class _AuditClient:
    """OMLX-shaped mock returning a canned per-atom audit."""

    def __init__(self, payload=None, error=None):
        self.settings = Settings()
        self._payload = payload
        self._error = error
        self.calls = 0

    def chat_json(self, messages, **kwargs):
        self.calls += 1
        if self._error:
            raise RuntimeError(self._error)
        return self._payload if self._payload is not None else {
            "missing_channels": [
                {"channel": "third-party", "mechanism": "shared failover operators",
                 "citation_url": "https://example.com/ok"},
                {"channel": "not-a-channel", "mechanism": "invented",
                 "citation_url": "https://example.com/invented"},
            ],
            "new_channels": [
                {"channel": "compute-supply", "mechanism": "GPU allocation coupling",
                 "citation_url": "https://example.com/gpu"},
            ],
            "expert_edges": [
                {"other_risk_id": "risk-b", "channel": "cloud",
                 "mechanism": "region failover congestion",
                 "citation_url": "https://example.com/edge"},
                {"other_risk_id": "risk-ghost", "channel": "",
                 "mechanism": "unknown atom", "citation_url": "https://example.com/g"},
            ],
        }


@pytest.fixture
def atoms():
    return [
        RiskAtom(risk_id="risk-a", title="Alpha cloud concentration", channels=["cloud"]),
        RiskAtom(risk_id="risk-b", title="Beta identity sprawl", channels=["identity"]),
    ]


def test_channel_audit_produces_well_formed_rows(atoms):
    rows = channel_audit(atoms, _AuditClient(), resolver=lambda url: True)
    proposals = [r for r in rows if not r.get("error")]
    kinds = {(r["kind"], r["channel"]) for r in proposals}
    # taxonomy-missing tag kept, invented channel demoted to new_channel,
    # edge to known atom kept, edge to unknown atom dropped
    assert ("tag_add", "third-party") in kinds
    assert ("tag_add", "not-a-channel") not in kinds
    assert ("new_channel", "compute-supply") in kinds
    edge = [r for r in proposals if r["kind"] == "edge_add"]
    # risk-a's edge to risk-b is kept; risk-b's canned edge points at
    # itself and is dropped (self-edges are not proposals)
    assert len(edge) == 1
    assert {tuple(r["risk_ids"]) for r in edge} == {("risk-a", "risk-b")}
    assert all(r["citation_status"] == "resolved" for r in proposals)
    assert all(r["status"] == "pending" for r in proposals)
    assert all(r["prompt_version"] == "channel-audit-v1" for r in proposals)
    assert all(r["prompt_hash"] for r in proposals)


def test_channel_audit_resolver_controls_citation_fate(atoms):
    resolved = channel_audit(atoms, _AuditClient(), resolver=lambda url: True)
    assert all(r["citation_status"] == "resolved" for r in resolved)

    dead = channel_audit(atoms, _AuditClient(), resolver=lambda url: False)
    assert dead, "expected proposal rows"
    assert all(r["citation_status"] == "unresolvable" for r in dead)
    assert all(r["status"] == "auto_rejected" for r in dead)

    unknown = channel_audit(atoms, _AuditClient(), resolver=lambda url: None)
    assert all(r["citation_status"] == "unverified" for r in unknown)
    assert all(r["status"] == "pending" for r in unknown)


def test_channel_audit_error_is_recorded_not_raised(atoms):
    rows = channel_audit(atoms, _AuditClient(error="endpoint down"))
    assert len(rows) == 2  # one error row per atom
    assert all("endpoint down" in r["error"] for r in rows)
    assert all(r["prompt_version"] == "channel-audit-v1" for r in rows)


def test_default_resolver_rejects_non_urls_without_network():
    assert default_resolver("not-a-url") is False
    assert default_resolver("ftp://example.com/x") is False
    assert default_resolver("") is False
    # private targets are not fetched — recorded as unverified, never rejected
    assert default_resolver("http://127.0.0.1:9/private") is None
    assert default_resolver("http://localhost/internal") is None


# ── shadow pass in the join ───────────────────────────────────────────────


def _dense_join_fixture(tmp_path):
    """6 risks in two channel groups (identity 0,1,3 / cloud 2,4,5), one
    linked evidence event per quarter across 8 quarters."""
    channels = {0: "identity", 1: "identity", 2: "cloud",
                3: "identity", 4: "cloud", 5: "cloud"}
    risks = [
        {
            "issue": f"compound test risk {i} ({channels[i]})",
            "review_status": "approved",
            "description": f"synthetic risk touching {channels[i]} infrastructure",
            "status": "monitoring",
            "signal_maturity": "established",
            "momentum": "stable",
            "evidence_refs": [f"E{i}q{q}" for q in range(8)],
            "source_type": "synthetic",
        }
        for i in range(6)
    ]
    rows = []
    for i in range(6):
        for q in range(8):
            if q == (i % 8):
                continue
            month = q * 3 + 2
            year = 2024 + (month - 1) // 12
            month = (month - 1) % 12 + 1
            rows.append((f"E{i}q{q}", f"{year}-{month:02d}-15", 10_000.0 * (i + 1)))
    risks_path = tmp_path / "emerging_risks.json"
    risks_path.write_text(json.dumps(risks))
    ev_db = tmp_path / "event_evidence.db"
    conn = sqlite3.connect(ev_db)
    conn.execute(
        "CREATE TABLE evidence_events (event_id TEXT, source_date TEXT, "
        "loss_amount_usd REAL, review_status TEXT DEFAULT 'approved')"
    )
    conn.executemany(
        "INSERT INTO evidence_events (event_id, source_date, loss_amount_usd) VALUES (?,?,?)",
        rows,
    )
    conn.commit()
    conn.close()
    return Settings(
        scenario_db_path=tmp_path / "atlas.db",
        evidence_db_path=ev_db,
        emerging_risks_path=risks_path,
    )


def _cross_group_atom_ids(settings):
    atoms = load_risk_atoms(settings.emerging_risks_path)
    identity = next(a.risk_id for a in atoms if "identity" in a.channels)
    cloud = next(a.risk_id for a in atoms if "cloud" in a.channels)
    return sorted([identity, cloud])


def _table_counts(db_path):
    conn = sqlite3.connect(db_path)
    n_cs = conn.execute("SELECT COUNT(*) FROM compound_scenarios").fetchone()[0]
    n_sr = conn.execute("SELECT COUNT(*) FROM score_realizations").fetchone()[0]
    conn.close()
    return n_cs, n_sr


def test_no_shadow_key_without_approved_proposals(tmp_path):
    settings = _dense_join_fixture(tmp_path)
    result = run_compound_join(settings, force=True, scorer_config=FAST)
    assert result.status == "ok"
    artifact = json.loads(Path(result.artifact).read_text())
    assert "shadow" not in artifact
    # shadow=False is byte-identical (modulo run identity/timestamps)
    again = run_compound_join(settings, force=True, scorer_config=FAST, shadow=False)
    artifact_off = json.loads(Path(again.artifact).read_text())
    assert "shadow" not in artifact_off
    assert artifact["compounds"] == artifact_off["compounds"]


def test_shadow_scores_delta_pairs_and_freezes_graduation(tmp_path, monkeypatch):
    settings = _dense_join_fixture(tmp_path)
    telemetry_path = tmp_path / "telemetry.jsonl"
    monkeypatch.setattr(
        "contingency_atlas.compound_join.TELEMETRY_PATH", telemetry_path
    )

    base = run_compound_join(settings, force=True, scorer_config=FAST)
    assert base.status == "ok"
    base_artifact = json.loads(Path(base.artifact).read_text())
    assert "shadow" not in base_artifact
    base_counts = _table_counts(settings.scenario_db_path)

    # an approved expert edge connecting two otherwise-unconnected atoms
    pair = _cross_group_atom_ids(settings)
    create_channel_proposals(
        settings.scenario_db_path,
        [_candidate(
            kind="edge_add", risk_ids=pair, channel="",
            mechanism="shared failover operators couple the outages",
        )],
    )
    pid = list_proposals(
        settings.scenario_db_path, kind="channel", status="pending"
    )[0]["proposal_id"]
    approve_channel_proposal(
        settings.scenario_db_path, pid,
        reviewer="alice", reason="documented coupling",
    )

    rerun = run_compound_join(settings, force=True, scorer_config=FAST)
    assert rerun.status == "ok"
    artifact = json.loads(Path(rerun.artifact).read_text())

    shadow = artifact.get("shadow")
    assert shadow is not None
    assert shadow["graduation_freeze"] is True
    assert shadow["n_proposals_applied"] == 1
    assert shadow["n_delta_pairs"] == 1
    delta = shadow["coverage_delta"][0]
    assert delta["pair"] == pair
    assert delta["pair_id"] == pair_id_for(pair)
    assert 0.0 <= delta["p_hat"] <= 1.0

    # GRADUATION FREEZE, by construction: the delta pair appears nowhere
    # in governed stores, whose rows remain exactly the base run's.
    assert artifact["compounds"] == base_artifact["compounds"]
    assert _table_counts(settings.scenario_db_path) == base_counts
    conn = sqlite3.connect(settings.scenario_db_path)
    in_cs = conn.execute(
        "SELECT COUNT(*) FROM compound_scenarios WHERE pair_id = ?",
        (delta["pair_id"],),
    ).fetchone()[0]
    in_sr = conn.execute(
        "SELECT COUNT(*) FROM score_realizations WHERE pair_id = ?",
        (delta["pair_id"],),
    ).fetchone()[0]
    conn.close()
    assert in_cs == 0 and in_sr == 0

    # telemetry event for the shadow pass
    events = [json.loads(ln) for ln in telemetry_path.read_text().splitlines()]
    shadow_events = [e for e in events if e["node"] == "compound_join_shadow"]
    assert len(shadow_events) == 1
    assert shadow_events[0]["tags"]["n_delta_pairs"] == 1
    assert shadow_events[0]["tags"]["graduation_freeze"] is True


def test_shadow_flag_off_ignores_approved_proposals(tmp_path):
    settings = _dense_join_fixture(tmp_path)
    pair = _cross_group_atom_ids(settings)
    create_channel_proposals(
        settings.scenario_db_path,
        [_candidate(kind="edge_add", risk_ids=pair, channel="")],
    )
    pid = list_proposals(
        settings.scenario_db_path, kind="channel", status="pending"
    )[0]["proposal_id"]
    approve_channel_proposal(
        settings.scenario_db_path, pid, reviewer="alice", reason="documented"
    )
    result = run_compound_join(
        settings, force=True, scorer_config=FAST, shadow=False
    )
    artifact = json.loads(Path(result.artifact).read_text())
    assert "shadow" not in artifact


# ── web: review queue section + decide routing ───────────────────────────


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
    create_channel_proposals(db_path, [_candidate()])
    settings = Settings(
        scenario_db_path=db_path,
        evidence_db_path=tmp_path / "event_evidence.db",
        emerging_risks_path=risks_path,
    )
    monkeypatch.setattr(server, "get_settings", lambda: settings)
    return settings


def test_review_queue_includes_channel_section(web_setup):
    queue = server.build_review_queue()
    pending = queue["channel_proposals"]["pending"]
    assert len(pending) == 1
    entry = pending[0]
    assert entry["kind"] == "channel"
    assert entry["record_id"].startswith("cp-")
    assert entry["report"]["approvable"] is True
    assert entry["record"]["mechanism"]
    labels = {c["label"] for c in entry["report"]["checks"]}
    assert "Transmission mechanism" in labels
    assert any("Citation" in label for label in labels)


def test_decide_endpoint_routes_channel_approve(web_setup):
    pid = list_proposals(
        web_setup.scenario_db_path, kind="channel", status="pending"
    )[0]["proposal_id"]
    with _dashboard() as port:
        status, payload = _request(port, "POST", "/api/review/decide", {
            "kind": "channel", "record_id": pid, "action": "approve",
            "reviewer": "alice", "reason": "mechanism documented",
        })
    assert status == 200 and payload["ok"] is True
    assert payload["record"]["decision"] == "approved"
    row = get_channel_proposal(web_setup.scenario_db_path, pid)
    assert row["status"] == "approved"
    assert row["graph_version_applied"] is None  # deferred, always
    queue = server.build_review_queue()
    assert queue["channel_proposals"]["pending"] == []


def test_decide_endpoint_channel_requires_reviewer_and_reason(web_setup):
    pid = list_proposals(
        web_setup.scenario_db_path, kind="channel", status="pending"
    )[0]["proposal_id"]
    with _dashboard() as port:
        status, payload = _request(port, "POST", "/api/review/decide", {
            "kind": "channel", "record_id": pid, "action": "approve",
            "reviewer": "", "reason": "x",
        })
        assert status == 400 and "reviewer" in payload["error"].lower()
        status, payload = _request(port, "POST", "/api/review/decide", {
            "kind": "channel", "record_id": pid, "action": "approve",
            "reviewer": "alice", "reason": "",
        })
        assert status == 400 and "reason" in payload["error"].lower()


def test_decide_endpoint_routes_channel_reject(web_setup):
    pid = list_proposals(
        web_setup.scenario_db_path, kind="channel", status="pending"
    )[0]["proposal_id"]
    with _dashboard() as port:
        status, payload = _request(port, "POST", "/api/review/decide", {
            "kind": "channel", "record_id": pid, "action": "reject",
            "reviewer": "alice", "reason": "speculative coupling",
        })
    assert status == 200
    assert payload["record"]["decision"] == "rejected"
    assert get_channel_proposal(web_setup.scenario_db_path, pid)["status"] == "rejected"
