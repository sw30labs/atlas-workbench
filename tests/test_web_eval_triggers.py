"""Dashboard triggers for the hybrid-scoring program (eval + elicitation).

Covers the six runner phases behind the new Pipeline cards (four
deterministic eval triggers, two OMLX-gated elicitation triggers), the P8
duplicate-scan endpoint, the compounds shadow strip + paper-trail
endpoint, and the Phase 8 modal's refutation-memo checkbox wiring.
"""

from __future__ import annotations

import http.client
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from workbench_atlas.compound_join import migrate_compound
from workbench_atlas.config import Settings
from workbench_atlas.proposals import (
    create_analogues,
    decide_analogue,
    list_proposals,
)
from workbench_atlas.schema import migrate, migrate_evidence_events
from workbench_atlas.web import runner, server
from workbench_atlas.web.server import DashboardHandler

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def web_settings(tmp_path, monkeypatch):
    """Hermetic tmp stores wired into both the runner and the server, with
    CWD at tmp_path so relative artifact paths (generated/...) stay tmp."""
    (tmp_path / "emerging_risks.json").write_text(json.dumps([]))
    migrate(str(tmp_path / "atlas.db"))
    migrate_evidence_events(str(tmp_path / "event_evidence.db"))
    settings = Settings(
        scenario_db_path=tmp_path / "atlas.db",
        evidence_db_path=tmp_path / "event_evidence.db",
        emerging_risks_path=tmp_path / "emerging_risks.json",
    )
    monkeypatch.setattr(runner, "get_settings", lambda: settings)
    monkeypatch.setattr(server, "get_settings", lambda: settings)
    monkeypatch.chdir(tmp_path)
    return settings


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


# ── runner phases ────────────────────────────────────────────────────────


def test_new_phases_are_valid():
    for phase in (
        "eval_r0_suite", "eval_r0_multiseed", "eval_ablations",
        "judgment_ledger", "channel_audit", "analogue_elicitation",
    ):
        assert phase in runner.VALID_PHASES


def test_eval_r0_suite_phase(web_settings):
    result = runner.execute_phase("eval_r0_suite", {"mc_draws": 300})
    assert result["recommendation"] in ("go", "iterate", "abandon")
    assert result["ap_lift"] is not None
    assert result["ap_lift_ci95"]
    assert result["task_b_pass"] in (True, False)
    assert result["task_c_pass"] in (True, False)
    assert result["bcal_coverage"] is not None
    assert result["bcal_pass"] in (True, False)
    assert result["task_l_fitted_minus_pooled"] is not None
    artifact = Path(result["artifact"])
    assert artifact.is_file()
    report = json.loads(artifact.read_text())
    assert report["recommendation"] == result["recommendation"]


def test_eval_r0_multiseed_phase(web_settings):
    result = runner.execute_phase(
        "eval_r0_multiseed", {"seeds": 2, "mc_draws": 200}
    )
    assert result["seeds"] == 2
    assert result["ap_lift_min"] <= result["ap_lift_median"] <= result["ap_lift_max"]
    assert 0.0 <= result["task_b_pass_fraction"] <= 1.0
    assert 0.0 <= result["go_fraction"] <= 1.0
    assert Path(result["report"]).is_file()


def test_eval_ablations_phase(web_settings, monkeypatch):
    """Wiring-level: params reach run_ablations, the report maps to the job
    result, and outputs land on disk. The live ~50s run stays a UI job."""
    from workbench_atlas.scoring import ablations

    fake_report = {
        "summary": {
            "n_consistent": 2, "n_runnable": 2,
            "n_historical": 3, "n_not_implemented": 1,
        },
        "sensitivity": {
            "challenger_vs_primary": {
                "shrunk_empirical": {
                    "coverage_90": 0.9,
                    "strata": {"supported_rare": {"coverage_90": 0.8}},
                },
                "mixture": {
                    "coverage_90": 0.85,
                    "strata": {"supported_rare": {"coverage_90": 0.75}},
                },
            },
        },
    }
    seen = {}

    def fake_run(atoms, events, *, truth=None, holdout_quarters=8, scorer_config=None):
        seen["n_atoms"] = len(atoms)
        seen["mc_draws"] = scorer_config.mc_draws
        seen["truth"] = truth
        return ablations.AblationReport(report=fake_report, report_md="# fake\n")

    monkeypatch.setattr(ablations, "run_ablations", fake_run)
    result = runner.execute_phase("eval_ablations", {"mc_draws": 1234})
    assert seen["n_atoms"] == 15  # the R0 world is generated in-process
    assert seen["mc_draws"] == 1234
    assert seen["truth"]  # the truth manifest flows through
    assert result["n_consistent"] == 2
    assert result["n_runnable"] == 2
    assert "shrunk_empirical: coverage 0.9 · supported-rare 0.8" in result["challenger_vs_primary"]
    assert "mixture: coverage 0.85 · supported-rare 0.75" in result["challenger_vs_primary"]
    assert Path(result["artifact"]).is_file()  # write_ablation_outputs ran


def test_judgment_ledger_phase_empty_register(web_settings):
    result = runner.execute_phase("judgment_ledger", {})
    assert result["n_recorded"] == 0
    assert result["n_resolved"] == 0
    assert result["brier"] == "insufficient (n=0)"  # honest n=0, not hidden
    assert result["chain_ok"] is True
    assert Path(result["report"]).is_file()


def test_start_job_judgment_ledger_completes(web_settings):
    job_id, err = runner.start_job("judgment_ledger", {})
    assert err is None
    deadline = time.time() + 15
    while time.time() < deadline:
        job = runner.get_job(job_id)
        if job["status"] != "running":
            break
        time.sleep(0.1)
    job = runner.get_job(job_id)
    assert job["status"] == "done"
    assert job["result"]["chain_ok"] is True


def test_judgment_ledger_records_approved_analogue(web_settings):
    db = web_settings.scenario_db_path
    migrate_compound(str(db))
    stored = create_analogues(db, [{
        "pair_id": "pair-aaa",
        "risk_ids": ["risk-a", "risk-b"],
        "event": "2012 example outage",
        "period": "2012",
        "mechanism_map": {"channel": "identity"},
        "co_fired": True,
        "outcome_class": "q95-band-3",
        "citations": ["https://example.com/report"],
        "disanalogies": ["different regulator"],
        "elicitor": {"model_id": "fixture", "prompt_version": "t", "prompt_hash": "x"},
    }])
    assert stored["created"] == 1
    decide_analogue(
        db, stored["analogue_ids"][0], decision="approved",
        reviewer="tester", reason="fixture",
        decided_at="2026-08-01T00:00:00+00:00",
    )
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO compound_scenarios (compound_id, pair_id, risk_ids, state_hash,"
        " p_hat, p_lo, p_hi, loss_q50, loss_q95, loss_mean, uncertainty_class,"
        " dependence_support, evidence_support, data_regime, status, run_id,"
        " scored_at, algorithm_version, provenance_note, loss_units)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("scn-1", "pair-aaa", json.dumps(["risk-a", "risk-b"]), "hash",
         0.4, 0.2, 0.6, 1e5, 9e5, 3e5, "mixed", 5, 0.8, "R1", "candidate",
         "run-1", "2026-08-01T00:00:00+00:00", "0.1.0", "test", "dollars"),
    )
    conn.commit()
    conn.close()

    result = runner.execute_phase("judgment_ledger", {})
    assert result["n_recorded"] == 1
    assert result["chain_ok"] is True
    # empty evidence db -> no signal events -> nothing resolves (honest n=0)
    assert result["n_resolved"] == 0
    assert result["brier"] == "insufficient (n=0)"


def test_omlx_gated_phases_record_offline(web_settings, monkeypatch):
    """Offline is a recorded status, never an exception and never a call."""
    monkeypatch.setattr(runner, "omlx_reachable", lambda *a, **k: False)

    class _NoClient:  # any client construction here means the gate leaked
        def __init__(self, *a, **k):
            raise AssertionError("LLM client built while OMLX is offline")

    monkeypatch.setattr("workbench_atlas.llm.OMLXClient", _NoClient)

    audit = runner.execute_phase("channel_audit", {})
    assert audit["status"] == "omlx_offline"
    assert audit["n_proposals_created"] == 0
    assert audit["n_auto_rejected"] == 0

    elicit = runner.execute_phase("analogue_elicitation", {"canary": True})
    assert elicit["status"] == "omlx_offline"
    assert elicit["n_created"] == 0
    assert "canary_rate" not in elicit


# ── /api/review/scan-duplicates ──────────────────────────────────────────


@pytest.fixture()
def dupe_settings(web_settings):
    risks = [
        {"issue": "Cloud region failover congestion",
         "description": "Failover traffic jams the remaining cloud region",
         "review_status": "approved", "source_type": "synthetic"},
        {"issue": "Cloud region failover congestion risk",
         "description": "Failover traffic jams the remaining cloud region",
         "review_status": "approved", "source_type": "synthetic"},
        {"issue": "Sanctions screening rule churn",
         "description": "Screening rules change faster than tuning cycles",
         "review_status": "approved", "source_type": "synthetic"},
    ]
    web_settings.emerging_risks_path.write_text(json.dumps(risks))
    return web_settings


def test_scan_duplicates_creates_pending_proposals(dupe_settings, monkeypatch):
    monkeypatch.setattr(runner, "omlx_reachable", lambda *a, **k: False)
    with _dashboard() as port:
        status, first = _request(port, "POST", "/api/review/scan-duplicates", {})
        assert status == 200
        assert first["n_scanned"] == 3
        assert first["n_candidates"] == 1
        assert first["n_created"] == 1
        assert first["n_already_pending"] == 0
        # OMLX offline: advisories are skipped, never blocking
        assert first["advisories_attached"] == 0
        # second scan: the pending pair dedups, nothing new is created
        status, second = _request(port, "POST", "/api/review/scan-duplicates", {})
        assert status == 200
        assert second["n_created"] == 0
        assert second["n_already_pending"] == 1
    pending = list_proposals(dupe_settings.scenario_db_path, status="pending")
    assert len(pending) == 1
    assert pending[0]["status"] == "pending"  # proposals only, never approved
    assert {pending[0]["title_a"], pending[0]["title_b"]} == {
        "Cloud region failover congestion",
        "Cloud region failover congestion risk",
    }


def test_scan_duplicates_empty_register(web_settings, monkeypatch):
    monkeypatch.setattr(runner, "omlx_reachable", lambda *a, **k: False)
    with _dashboard() as port:
        status, payload = _request(port, "POST", "/api/review/scan-duplicates", {})
    assert status == 200
    assert payload["n_scanned"] == 0
    assert payload["n_created"] == 0


# ── compounds shadow (what-if watch) ─────────────────────────────────────


def test_compounds_payload_includes_shadow(web_settings):
    shadow = {
        "n_proposals_applied": 1,
        "n_delta_pairs": 2,
        "coverage_delta": [
            {"pair": ["risk-a", "risk-b"], "pair_id": "p1", "p_hat": 0.12},
            {"pair": ["risk-a", "risk-c"], "pair_id": "p2", "p_hat": 0.05},
        ],
        "graduation_freeze": True,
    }
    artifact = web_settings.emerging_risks_path.parent / "compound_scenarios.json"
    artifact.write_text(json.dumps({"compounds": [], "shadow": shadow}))
    payload = server.build_payload()
    assert payload["compound_shadow"] == shadow


def test_compounds_payload_shadow_absent_is_null(web_settings):
    # decision: the key is always present; absence reads as null
    assert server.build_payload()["compound_shadow"] is None
    artifact = web_settings.emerging_risks_path.parent / "compound_scenarios.json"
    artifact.write_text(json.dumps({"compounds": []}))
    assert server.build_payload()["compound_shadow"] is None
    artifact.write_text("{corrupt")
    assert server.build_payload()["compound_shadow"] is None


# ── /api/compounds/history (paper trail) ─────────────────────────────────


def _seed_realizations(db):
    migrate_compound(str(db))
    conn = sqlite3.connect(str(db))
    conn.executemany(
        "INSERT INTO score_realizations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            ("rlz-2", "run-2", "pair-a", "scn-1", "h2", None, "det", "0.2.0",
             "cfg-b", "7", 0.5, 0.3, 0.7, "2026-08-02T00:00:00+00:00", "{}"),
            ("rlz-1", "run-1", "pair-a", "scn-1", "h1", None, "det", "0.1.0",
             "cfg-a", "7", 0.4, 0.2, 0.6, "2026-08-01T00:00:00+00:00", "{}"),
            ("rlz-3", "run-1", "pair-b", "scn-2", "h3", None, "det", "0.1.0",
             "cfg-a", "7", 0.1, 0.05, 0.2, "2026-08-01T00:00:00+00:00", "{}"),
        ],
    )
    conn.commit()
    conn.close()


def test_compounds_history_endpoint(web_settings):
    _seed_realizations(web_settings.scenario_db_path)
    with _dashboard() as port:
        status, payload = _request(port, "GET", "/api/compounds/history?pair_id=pair-a")
        assert status == 200
        history = payload["history"]
        # ordered by scored_at, oldest first; the other pair is excluded
        assert [r["run_id"] for r in history] == ["run-1", "run-2"]
        assert history[0]["p_hat"] == 0.4 and history[1]["p_hat"] == 0.5
        assert history[0]["p_lo"] == 0.2 and history[0]["p_hi"] == 0.6
        assert history[0]["model_config_hash"] == "cfg-a"
        assert history[0]["algorithm_version"] == "0.1.0"
        assert history[1]["algorithm_version"] == "0.2.0"
        assert history[0]["judgment_version"] == "det"
        assert history[0]["scored_at"] < history[1]["scored_at"]
        _, empty = _request(port, "GET", "/api/compounds/history?pair_id=nope")
        assert empty["history"] == []
        _, blank = _request(port, "GET", "/api/compounds/history")
        assert blank["history"] == []


def test_compounds_history_without_join_table(web_settings):
    # atlas.db predates the first join: no score_realizations table -> []
    with _dashboard() as port:
        status, payload = _request(port, "GET", "/api/compounds/history?pair_id=pair-a")
    assert status == 200
    assert payload["history"] == []


# ── Phase 8 refutation-memo wiring ───────────────────────────────────────


def test_graduate_with_refutations_param_wiring(web_settings, monkeypatch):
    """The modal checkbox must reach the phase params; the refutation pass
    runs only when it is set (run_refutation_pass mocked)."""
    monkeypatch.setattr(runner, "omlx_reachable", lambda *a, **k: True)

    class FakeClient:
        def close(self):
            pass

    monkeypatch.setattr(
        "workbench_atlas.llm.OMLXClient", lambda *a, **k: FakeClient()
    )

    import workbench_atlas.compound_graduate as graduate_module
    import workbench_atlas.refutations as refutations_module

    monkeypatch.setattr(
        graduate_module,
        "run_compound_graduation",
        lambda settings, client=None: SimpleNamespace(
            status="ok", n_eligible=0, n_graduated=0, n_judge_rejected=0,
            graduated=[], note="", errors=[],
        ),
    )
    calls = []
    monkeypatch.setattr(
        refutations_module,
        "run_refutation_pass",
        lambda settings, client: calls.append(1) or {"n_stored": 2, "errors": []},
    )

    without = runner.execute_phase("compound_graduate", {})
    assert calls == []
    assert "refutation_memos" not in without

    with_it = runner.execute_phase("compound_graduate", {"with_refutations": True})
    assert calls == [1]
    assert with_it["refutation_memos"] == 2


# ── page wiring ──────────────────────────────────────────────────────────


def test_index_has_eval_trigger_cards():
    html = (ROOT / "workbench_atlas/web/static/index.html").read_text()
    for name in (
        "CRASH TEST", "MULTIVERSE", "WHAT BREAKS FIRST?",
        "REALITY CHECK", "HIDDEN CONNECTIONS", "PRECEDENT",
    ):
        assert name in html
    for phase in (
        "eval_r0_suite", "eval_r0_multiseed", "eval_ablations",
        "judgment_ledger", "channel_audit", "analogue_elicitation",
    ):
        assert f'id: "{phase}"' in html
        assert f"{phase}: {{ name:" in html
    # OMLX-gated cards carry the offline modal block
    assert 'short: "Hidden Connections"' in html
    assert 'short: "Precedent"' in html
    assert "is unavailable" in html


def test_index_has_graduate_refutation_checkbox():
    html = (ROOT / "workbench_atlas/web/static/index.html").read_text()
    assert "with_refutations" in html
    assert "Add devil&#39;s advocate memos (display only)" in html or \
           "Add devil's advocate memos (display only)" in html


def test_index_has_scan_duplicates_button():
    html = (ROOT / "workbench_atlas/web/static/index.html").read_text()
    assert 'id="review-scan-dupes"' in html
    assert "FIND LOOKALIKE RISKS" in html
    assert "/api/review/scan-duplicates" in html


def test_index_has_compounds_shadow_and_trail():
    html = (ROOT / "workbench_atlas/web/static/index.html").read_text()
    assert 'id="cmp-shadow"' in html
    assert "WHAT-IF WATCH" in html
    assert "graduation frozen" in html
    assert "paper trail" in html
    assert "/api/compounds/history" in html


def test_index_has_playground_view_hosting_eval_elicit_cards():
    html = (ROOT / "workbench_atlas/web/static/index.html").read_text()
    # nav option, view section, router entry
    assert 'data-view="playground"' in html
    assert 'id="view-playground"' in html
    assert '"playground"' in html
    # the EVAL/ELICIT trigger cards render into the playground container
    assert 'id="playground-cards"' in html
    assert "PHASES.filter(p => p.tag)" in html
    assert "PHASES.filter(p => !p.tag)" in html
    # the latest-artifacts window lives on the playground page, not pipeline
    playground = html.split('id="view-playground"', 1)[1]
    assert 'id="eval-reports-panel"' in playground
    pipeline = html.split('id="view-pipeline"', 1)[1].split(
        'id="view-playground"', 1)[0]
    assert 'id="eval-reports-panel"' not in pipeline
