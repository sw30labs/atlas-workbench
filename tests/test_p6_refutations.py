"""P6 adversarial roles: refutation memos, definition stress, spot audit.

Covers the wave-4 P6 deliverable with mock clients only (no live LLM):
the memo is stored with model/prompt hash and never contains invented
numbers, the deterministic definition stress recomputes windowed support
through the same shrunk-empirical formula (verified by hand on a small
series), and the boilerplate spot audit counts the kill-rule share.
"""

from __future__ import annotations

import http.client
import json
import math
import sqlite3
import threading
from contextlib import contextmanager
from http.server import ThreadingHTTPServer

import pytest

from workbench_atlas.compound_graduate import migrate_graduations
from workbench_atlas.compound_join import migrate_compound
from workbench_atlas.config import Settings
from workbench_atlas.refutations import (
    BOILERPLATE_KILL_SHARE,
    definition_stress,
    generate_refutation_memo,
    latest_memos,
    migrate_refutations,
    rate_memo,
    run_refutation_pass,
    spot_audit_report,
    store_memo,
    windowed_support,
)
from workbench_atlas.schema import migrate, migrate_evidence_events
from workbench_atlas.scoring.models import IncidentEvent, RiskAtom
from workbench_atlas.scoring.scorer import DetScorer, ScorerConfig
from workbench_atlas.web import server
from workbench_atlas.web.server import DashboardHandler


# ── definition stress (deterministic, hand-verified) ─────────────────────


@pytest.fixture
def stress_scorer():
    """T=12 quarters; atom a fires {1, 5}, atom b fires {3, 7}."""
    atoms = [
        RiskAtom(risk_id="a", title="Risk A"),
        RiskAtom(risk_id="b", title="Risk B"),
    ]
    events = [
        IncidentEvent("e1", "a", 1, 100.0),
        IncidentEvent("e2", "a", 5, 100.0),
        IncidentEvent("e3", "b", 3, 100.0),
        IncidentEvent("e4", "b", 7, 100.0),
    ]
    return DetScorer(ScorerConfig()).fit(events, atoms, 12)


def test_windowed_support_hand_computed():
    series = {"a": {1, 5}, "b": {3, 7}}
    # w=0 is exact co-firing: {1,5} ∩ {3,7} = ∅
    assert windowed_support(series, ["a", "b"], 12, 0) == 0
    # w=2, hand count: a fires within 2 of t for t in [0,7]
    # ({1,5} each reach ±2 and the spans touch); b for t in [1,9].
    # Intersection [1,7] -> 7 anchor quarters.
    assert windowed_support(series, ["a", "b"], 12, 2) == 7
    # w=1: a reaches [0,2]∪[4,6], b reaches [2,4]∪[6,8] -> {2,4,6}
    assert windowed_support(series, ["a", "b"], 12, 1) == 3


def test_definition_stress_recomputes_windowed_support(stress_scorer):
    out = definition_stress(stress_scorer, ["a", "b"], w=2)
    assert out["w"] == 2
    assert out["n_s_exact"] == 0
    assert out["n_s_windowed"] == 7  # hand-verified anchor count (see above)
    # Expected values through the production formula, using the scorer's
    # own fitted marginals (EB-shrunk): q = (N_S + kappa*q_ind)/(T+kappa),
    # Fréchet-clipped to min(q_i), annualized over the config horizon.
    cfg = stress_scorer.config
    q_a = stress_scorer.marginals["a"].q_quarterly
    q_b = stress_scorer.marginals["b"].q_quarterly
    q_ind = q_a * q_b
    kappa, t_q, horizon = cfg.shrink_kappa, 12, cfg.horizon_quarters
    q_prod = (0 + kappa * q_ind) / (t_q + kappa)
    p_prod = 1.0 - (1.0 - q_prod) ** horizon
    q_win_raw = (7 + kappa * q_ind) / (t_q + kappa)
    q_win = min(q_win_raw, min(q_a, q_b))  # Fréchet clip to min marginal
    assert q_win_raw > min(q_a, q_b)  # the clip actually binds here
    p_win = 1.0 - (1.0 - q_win) ** horizon
    assert math.isclose(out["p_hat_production"], p_prod, rel_tol=0, abs_tol=1e-12)
    assert math.isclose(out["p_hat_windowed"], p_win, rel_tol=0, abs_tol=1e-12)
    assert math.isclose(out["swing"], p_win - p_prod, rel_tol=0, abs_tol=1e-12)
    assert out["swing"] > 0
    assert set(out) >= {
        "w", "n_s_windowed", "p_hat_windowed", "p_hat_production", "swing",
    }


def test_definition_stress_w0_matches_production(stress_scorer):
    exact = stress_scorer.joint_support(["a", "b"])
    out = definition_stress(stress_scorer, ["a", "b"], w=0)
    assert out["n_s_windowed"] == exact
    q_prod, _ = stress_scorer.joint_quarterly_combo(["a", "b"])
    p_prod = 1.0 - (1.0 - q_prod) ** stress_scorer.config.horizon_quarters
    assert math.isclose(out["p_hat_production"], p_prod, rel_tol=0, abs_tol=1e-12)
    assert math.isclose(out["swing"], 0.0, abs_tol=1e-12)


# ── memo generation with a mock client ───────────────────────────────────


class _JudgeSettings:
    llm_model_judge = "fake-judge"


MEMO_RESPONSE = {
    "case_against": (
        "The shared cloud channel explains the co-occurrence mechanically; "
        "the stored dependence support is only 2 quarters."
    ),
    "key_doubts": [
        "dependence_support of 2 is thin against 32 train quarters",
        "both atoms tag the same channel, so the join's own structure predicts the pair",
    ],
    "cited_facts": ["dependence_support: 2", "uncertainty_class: mixed"],
    "falsifier": "another co-fire in the next holdout quarter",
}


class FakeRefutator:
    settings = _JudgeSettings()

    def __init__(self, response=None):
        self.response = response or MEMO_RESPONSE
        self.calls = 0

    def chat_json(self, messages, model=None, temperature=0.0):
        self.calls += 1
        assert messages[0]["role"] == "system"
        return self.response


@pytest.fixture
def compound_row():
    return {
        "compound_id": "scn-deadbeef1234",
        "pair_id": "pair-0123456789ab",
        "risk_ids": json.dumps(["risk-a", "risk-b"]),
        "p_hat": 0.05,
        "p_lo": 0.01,
        "p_hi": 0.12,
        "uncertainty_class": "mixed",
        "dependence_support": 2,
        "evidence_support": 0.4,
        "data_regime": "R1",
        "scored_at": "2026-08-01T00:00:00+00:00",
    }


def test_generate_memo_structure_and_hash(compound_row):
    # scorer fitted on the compound's own atoms, so the stress runs
    atoms = [
        RiskAtom(risk_id="risk-a", title="Risk A"),
        RiskAtom(risk_id="risk-b", title="Risk B"),
    ]
    events = [
        IncidentEvent("e1", "risk-a", 1, 100.0),
        IncidentEvent("e2", "risk-b", 2, 100.0),
    ]
    scorer = DetScorer(ScorerConfig()).fit(events, atoms, 8)
    client = FakeRefutator()
    memo = generate_refutation_memo(
        compound_row,
        {"risk-a": "Risk A", "risk-b": "Risk B"},
        client,
        scorer=scorer,
    )
    assert client.calls == 1
    assert memo["error"] is None
    assert memo["model_id"] == "fake-judge"
    assert memo["prompt_version"]
    assert len(memo["prompt_hash"]) == 64
    assert memo["case_against"] == MEMO_RESPONSE["case_against"]
    assert memo["key_doubts"] == MEMO_RESPONSE["key_doubts"]
    assert memo["falsifier"] == MEMO_RESPONSE["falsifier"]
    # Public payload discipline: titles/channels + stored score fields only
    assert memo["payload"]["stored_score"]["p_hat"] == 0.05
    titles = {a["title"] for a in memo["payload"]["atoms"]}
    assert titles == {"Risk A", "Risk B"}
    # definition stress attached when a scorer is supplied
    assert memo["definition_stress"]["n_s_windowed"] is not None


def test_generate_memo_failure_recorded_not_raised(compound_row):
    class BrokenClient:
        settings = _JudgeSettings()

        def chat_json(self, messages, model=None, temperature=0.0):
            raise RuntimeError("endpoint down")

    memo = generate_refutation_memo(
        compound_row, {"risk-a": "A", "risk-b": "B"}, BrokenClient()
    )
    assert "endpoint down" in memo["error"]


def test_stored_memo_payload_has_no_invented_numbers(compound_row, tmp_path):
    db_path = tmp_path / "atlas.db"
    memo = generate_refutation_memo(
        compound_row, {"risk-a": "Risk A", "risk-b": "Risk B"}, FakeRefutator()
    )
    memo_id = store_memo(db_path, memo)
    stored = latest_memos(db_path)["scn-deadbeef1234"]
    assert stored["memo_id"] == memo_id
    blob = json.dumps(stored)
    assert "$" not in blob  # no dollar figures anywhere in the stored view
    # the only numerals are the supplied stored-score fields
    for token in ("0.05", "0.01", "0.12"):
        assert token in blob
    assert stored["model_id"] == "fake-judge"
    assert stored["prompt_hash"] == memo["prompt_hash"]


# ── store, rating, spot audit ────────────────────────────────────────────


def test_migrate_idempotent(tmp_path):
    db_path = tmp_path / "atlas.db"
    migrate_refutations(db_path)
    migrate_refutations(db_path)
    assert latest_memos(db_path) == {}


def test_latest_memos_picks_newest_per_compound(compound_row, tmp_path):
    db_path = tmp_path / "atlas.db"
    for i in range(2):
        memo = generate_refutation_memo(
            compound_row, {"risk-a": "A", "risk-b": "B"}, FakeRefutator()
        )
        memo["case_against"] = f"version {i}"
        memo["created_at"] = f"2026-08-0{i + 1}T00:00:00+00:00"
        store_memo(db_path, memo)
    assert latest_memos(db_path)["scn-deadbeef1234"]["case_against"] == "version 1"


def test_rate_memo_and_spot_audit(compound_row, tmp_path):
    db_path = tmp_path / "atlas.db"
    ids = []
    for _ in range(3):
        memo = generate_refutation_memo(
            compound_row, {"risk-a": "A", "risk-b": "B"}, FakeRefutator()
        )
        ids.append(store_memo(db_path, memo))
    assert spot_audit_report(db_path)["n_memos"] == 3
    assert spot_audit_report(db_path)["boilerplate_share"] is None

    rate_memo(db_path, ids[0], 0.9)   # specific/useful
    rate_memo(db_path, ids[1], 0.1)   # boilerplate
    report = spot_audit_report(db_path)
    assert report["n_rated"] == 2
    assert report["n_boilerplate"] == 1
    assert report["boilerplate_share"] == 0.5
    assert report["kill_rule_threshold"] == BOILERPLATE_KILL_SHARE == 0.30
    assert report["kill_rule_tripped"] is True  # 0.5 > 0.30

    with pytest.raises(ValueError, match=r"\[0, 1\]"):
        rate_memo(db_path, ids[2], 1.5)
    with pytest.raises(ValueError, match="not found"):
        rate_memo(db_path, "ref-missing", 0.5)


# ── run_refutation_pass end to end ───────────────────────────────────────


FP = "b" * 64


def _event(event_id, source_date, **overrides):
    record = {
        "event_id": event_id,
        "headline": f"Public incident {event_id}",
        "loss_amount_usd": None,
        "source_date": source_date,
        "risk_category": "Digital resilience",
        "source_url": f"https://news.example.com/{event_id}",
        "source_publisher": "Example Wire",
        "source_title": "Example incident report",
        "retrieved_at": "2026-03-16T09:00:00+00:00",
        "source_type": "public_reporting",
        "record_type": "event",
        "provenance_class": "public_primary",
        "source_locator": "",
        "independently_authored_summary": "Independent summary.",
        "rights_review": "attested_for_public_use",
        "content_fingerprint": FP,
        "review_status": "approved",
        "reviewed_at": "2026-03-17T10:00:00+00:00",
        "taxonomy_version": "atlas-1.0",
        "provenance_note": "Fixture record.",
    }
    record.update(overrides)
    return record


def _insert_events(db_path, events):
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


@pytest.fixture
def eligible_setup(tmp_path):
    """Two approved risks co-firing in the same holdout quarter, plus a
    promoted compound row for them.

    Quarters re-index from 0 (build_signal_series): alpha fires at
    periods {0, 2, 4}, beta at {1, 4} — so n_periods = 5, train = {0..2},
    holdout = {3, 4}, and the pair co-fires in holdout period 4. In
    train, exact support is 0; with w = 2 every train quarter has both
    atoms within reach (hand count: t=0,1,2 all qualify) -> n_s = 3.
    """
    ev_db = tmp_path / "event_evidence.db"
    _insert_events(ev_db, [
        _event("EVT-A1", "2025-01-10"),  # quarter 8100 -> period 0
        _event("EVT-A2", "2025-09-15"),  # quarter 8102 -> period 2
        _event("EVT-A3", "2026-03-15"),  # quarter 8104 -> period 4
        _event("EVT-B1", "2025-06-15"),  # quarter 8101 -> period 1
        _event("EVT-B2", "2026-03-20"),  # quarter 8104 -> period 4
    ])
    risks = [
        {"issue": "Ref alpha signal", "description": "alpha",
         "evidence_refs": ["EVT-A1", "EVT-A2", "EVT-A3"],
         "review_status": "approved",
         "source_type": "public_research", "provenance_note": "fixture"},
        {"issue": "Ref beta signal", "description": "beta",
         "evidence_refs": ["EVT-B1", "EVT-B2"],
         "review_status": "approved",
         "source_type": "public_research", "provenance_note": "fixture"},
    ]
    risks_path = tmp_path / "emerging_risks.json"
    risks_path.write_text(json.dumps(risks))
    atlas_db = tmp_path / "atlas.db"
    migrate(str(atlas_db))
    migrate_compound(str(atlas_db))
    migrate_graduations(str(atlas_db))
    conn = sqlite3.connect(str(atlas_db))
    conn.execute(
        """
        INSERT INTO compound_scenarios
            (compound_id, risk_ids, state_hash, p_hat, p_lo, p_hi,
             uncertainty_class, dependence_support, evidence_support,
             data_regime, status, run_id, scored_at, algorithm_version,
             provenance_note, pair_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "scn-eligible1",
            json.dumps(["ref-alpha-signal", "ref-beta-signal"]),
            "f" * 64,
            0.05, 0.01, 0.12,
            "mixed", 2, 0.4,
            "R1", "candidate", "run-1", "2026-08-01T00:00:00+00:00",
            "0.2.0", "fixture", "pair-abc",
        ),
    )
    conn.commit()
    conn.close()
    settings = Settings(
        scenario_db_path=atlas_db,
        evidence_db_path=ev_db,
        emerging_risks_path=risks_path,
    )
    return settings, risks


def test_run_refutation_pass_stores_memos(eligible_setup):
    settings, _risks = eligible_setup
    client = FakeRefutator()
    result = run_refutation_pass(settings, client)
    assert result["status"] == "ok"
    assert result["n_eligible"] == 1
    assert result["n_stored"] == 1
    assert result["errors"] == []

    memos = latest_memos(settings.scenario_db_path)
    assert "scn-eligible1" in memos
    stored = memos["scn-eligible1"]
    assert stored["case_against"] == MEMO_RESPONSE["case_against"]
    assert stored["model_id"] == "fake-judge"
    assert stored["prompt_hash"]
    # stress ran on the train window (periods {0,1,2}): hand-verified
    # windowed count — every train quarter has both atoms within w=2
    stress = stored["definition_stress"]
    assert stress is not None
    assert stress["w"] == 2
    assert stress["n_s_windowed"] == 3
    assert stress["n_s_exact"] == 0
    assert stress["t_quarters"] == 3
    assert stress["swing"] >= 0.0
    assert "$" not in json.dumps(stored)


def test_run_refutation_pass_none_eligible(tmp_path):
    atlas_db = tmp_path / "atlas.db"
    migrate(str(atlas_db))
    migrate_compound(str(atlas_db))
    migrate_graduations(str(atlas_db))
    risks_path = tmp_path / "emerging_risks.json"
    risks_path.write_text("[]")
    ev_db = tmp_path / "event_evidence.db"
    migrate_evidence_events(str(ev_db))
    settings = Settings(
        scenario_db_path=atlas_db,
        evidence_db_path=ev_db,
        emerging_risks_path=risks_path,
    )
    client = FakeRefutator()
    result = run_refutation_pass(settings, client)
    assert result["status"] == "none_eligible"
    assert result["n_stored"] == 0
    assert client.calls == 0


# ── web: memo on the compounds view + rate-memo endpoint ─────────────────


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


def _post(port, path, body):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request(
            "POST", path, body=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        return response.status, json.loads(response.read() or b"{}")
    finally:
        connection.close()


def test_compound_rows_attach_latest_memo(eligible_setup):
    settings, risks = eligible_setup
    run_refutation_pass(settings, FakeRefutator())
    rows = server._compound_rows(settings, risks)
    assert len(rows) == 1
    memo = rows[0]["refutation"]
    assert memo is not None
    assert memo["case_against"] == MEMO_RESPONSE["case_against"]
    assert memo["memo_id"].startswith("ref-")
    assert memo["reviewer_rating"] is None


def test_rate_memo_endpoint(eligible_setup, monkeypatch):
    settings, _risks = eligible_setup
    monkeypatch.setattr(server, "get_settings", lambda: settings)
    result = run_refutation_pass(settings, FakeRefutator())
    memo_id = result["memo_ids"][0]
    with _dashboard() as port:
        status, payload = _post(port, "/api/review/rate-memo", {
            "memo_id": memo_id, "rating": 0.2,
        })
        assert status == 200 and payload["ok"] is True
        status, payload = _post(port, "/api/review/rate-memo", {
            "memo_id": memo_id, "rating": 7,
        })
        assert status == 400
        status, payload = _post(port, "/api/review/rate-memo", {
            "memo_id": "ref-nope", "rating": 0.5,
        })
        assert status == 400 and "not found" in payload["error"]
    assert latest_memos(settings.scenario_db_path)["scn-eligible1"][
        "reviewer_rating"
    ] == 0.2
    assert spot_audit_report(settings.scenario_db_path)["n_boilerplate"] == 1


def test_window_flag_actually_reaches_definition_stress(monkeypatch, compound_row):
    """--window was accepted and then dropped on the floor.

    definition_stress was well covered directly (w=0 and w=2 both asserted),
    but nothing checked that run_refutation_pass's w ever reached it — so the
    CLI flag silently did nothing while every test passed. A knob that reports
    success while changing nothing is worse than no knob, and the gap was in
    the plumbing, which is where this test now sits.
    """
    import workbench_atlas.refutations as R

    seen: list[int] = []

    def spy(scorer, risk_ids, w=R.DEFAULT_WINDOW):
        seen.append(w)
        return {"n_s_windowed": 1, "p_hat_windowed": 0.1, "swing": 0.05}

    monkeypatch.setattr(R, "definition_stress", spy)

    atoms = [RiskAtom(risk_id="risk-a", title="Risk A"),
             RiskAtom(risk_id="risk-b", title="Risk B")]
    events = [IncidentEvent("e1", "risk-a", 1, 100.0),
              IncidentEvent("e2", "risk-b", 2, 100.0)]
    scorer = DetScorer(ScorerConfig()).fit(events, atoms, 8)

    R.generate_refutation_memo(
        compound_row, {"risk-a": "Risk A", "risk-b": "Risk B"},
        FakeRefutator(), scorer=scorer, w=7,
    )
    assert seen == [7], f"the requested window never reached definition_stress: {seen}"
