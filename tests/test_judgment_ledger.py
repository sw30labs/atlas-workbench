"""P7 judgment ledger (wave 6) — hash chain, frozen forecasts, resolution,
calibration report, and the `atlas-eval ledger` CLI smoke test.

Fixtures mirror test_compound_join.py's `_make_settings` (tiny risks.json +
evidence-events table). No live LLM, no network; everything runs on tmp paths.
"""

from __future__ import annotations

import json
import logging
import sqlite3

import pytest

from workbench_atlas.compound_join import migrate_compound
from workbench_atlas.proposals import create_analogues, migrate_proposals
from workbench_atlas.review import approve_analogue
from workbench_atlas.scoring import judgment_ledger as jl
from workbench_atlas.scoring.cli import eval_main
from workbench_atlas.scoring.models import IncidentEvent, pair_id_for

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _read_lines(path):
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text().splitlines()
        if line.strip()
    ]


def _insert_compound(db_path, pair_id, risk_ids, *, p_hat=0.25, p_lo=0.05,
                     p_hi=0.6, scored_at="2026-08-01T00:00:00+00:00"):
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT OR REPLACE INTO compound_scenarios VALUES "
        "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            f"scn-{pair_id[5:]}",
            json.dumps(sorted(risk_ids)),
            "ab" * 32,              # state_hash
            p_hat, p_lo, p_hi,
            None, None, None,       # loss columns (activation regime)
            "mixed",                # uncertainty_class
            0,                      # dependence_support
            0.0,                    # evidence_support
            "R1", "candidate",
            "run-test",
            scored_at,
            "0.1.0",                # algorithm_version
            "test row",
            pair_id,
            "cd" * 32,              # data_hash
            "activation_units",
        ),
    )
    conn.commit()
    conn.close()


def _analogue_candidate(pair_id, risk_ids, **overrides):
    cand = {
        "pair_id": pair_id,
        "risk_ids": risk_ids,
        "horizon": "1y",
        "compound_def": "all_must_fire",
        "event": "Knight Capital deployment cascade",
        "period": "2012-08",
        "mechanism_map": {"channel": "model",
                          risk_ids[0]: "deployment pipeline",
                          risk_ids[1]: "permissioning failure"},
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


def _approved_analogue(db_path, cand):
    out = create_analogues(db_path, [cand])
    analogue_id = out["analogue_ids"][0]
    approve_analogue(db_path, analogue_id, reviewer="alice",
                     reason="solid mechanism map")
    return analogue_id


def _claim_entry(ledger, risk_ids, *, p_hat, analogue_id="ana-x"):
    return jl.record_claim(
        ledger,
        kind="analogue",
        elicitor={"model_id": "mock-model", "prompt_hash": "ab" * 32},
        claim={"analogue_id": analogue_id, "pair_id": pair_id_for(risk_ids),
               "risk_ids": risk_ids, "event": "e", "period": "2012-08",
               "co_fired": True, "outcome_class": "q95-band-3"},
        frozen_forecast={
            "forecast_def_version": 1,
            "pair_id": pair_id_for(risk_ids),
            "compound_id": "scn-x",
            "state_hash": "ab" * 32,
            "data_hash": "cd" * 32,
            "algorithm_version": "0.1.0",
            "horizon_quarters": 4,
            "compound_def": "all_must_fire",
            "p_hat": p_hat,
            "p_interval": [p_hat / 2, min(1.0, p_hat * 2)],
            "scored_at": "2026-08-01T00:00:00+00:00",
        },
        human_decision={"decision": "approved", "reviewer": "alice"},
    )


# ---------------------------------------------------------------------------
# hash chain: append / verify / tamper evidence / restart linking
# ---------------------------------------------------------------------------


def test_append_and_verify_chain(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    r1 = jl.append_entry(ledger, {"seq": 1})
    r2 = jl.append_entry(ledger, {"seq": 2})
    assert r1["prev_hash"] == ""
    assert r2["prev_hash"] == r1["entry_hash"]
    ok, detail = jl.verify_chain(ledger)
    assert ok, detail
    assert "2 entries" in detail


def test_tamper_with_earlier_line_fails_verification(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    for i in range(3):
        jl.append_entry(ledger, {"seq": i})
    lines = ledger.read_text().splitlines()
    # flip one byte of content in the FIRST line, keeping valid JSON:
    entry = json.loads(lines[0])
    entry["seq"] = 99
    lines[0] = json.dumps(entry, sort_keys=True)
    ledger.write_text("\n".join(lines) + "\n")
    ok, detail = jl.verify_chain(ledger)
    assert not ok
    assert "line 1" in detail


def test_appends_across_restart_keep_the_chain(tmp_path):
    # append_entry is stateless: every call re-seeds prev_hash from the last
    # line on disk, so a fresh process (here: a fresh call, no shared state)
    # appends onto the same chain.
    ledger = tmp_path / "ledger.jsonl"
    first = jl.append_entry(ledger, {"seq": 1})
    second = jl.append_entry(ledger, {"seq": 2})  # "new process"
    assert second["prev_hash"] == first["entry_hash"]
    ok, detail = jl.verify_chain(ledger)
    assert ok, detail


def test_verify_missing_ledger_is_vacuously_valid(tmp_path):
    ok, detail = jl.verify_chain(tmp_path / "nope.jsonl")
    assert ok
    assert "0 entries" in detail


# ---------------------------------------------------------------------------
# frozen forecasts
# ---------------------------------------------------------------------------


def test_freeze_forecast_carries_exact_deterministic_numbers():
    row = {
        "pair_id": "pair-abc123",
        "compound_id": "scn-def456",
        "state_hash": "ab" * 32,
        "data_hash": "cd" * 32,
        "algorithm_version": "0.1.0",
        "p_hat": 0.123456,
        "p_lo": 0.0101,
        "p_hi": 0.4242,
        "scored_at": "2026-08-01T00:00:00+00:00",
    }
    frozen = jl.freeze_forecast(row)
    # verbatim carriage — no recomputation of any number
    assert frozen["p_hat"] == 0.123456
    assert frozen["p_interval"] == [0.0101, 0.4242]
    assert frozen["state_hash"] == row["state_hash"]
    assert frozen["data_hash"] == row["data_hash"]
    assert frozen["algorithm_version"] == "0.1.0"
    assert frozen["scored_at"] == row["scored_at"]
    # pinned forecast definition v1
    assert frozen["forecast_def_version"] == 1
    assert frozen["horizon_quarters"] == 4
    assert frozen["compound_def"] == "all_must_fire"


def test_record_claim_rejects_unknown_kind(tmp_path):
    with pytest.raises(ValueError, match="unknown claim kind"):
        jl.record_claim(
            tmp_path / "ledger.jsonl",
            kind="bogus",
            elicitor={},
            claim={},
            frozen_forecast={},
        )


# ---------------------------------------------------------------------------
# record_analogue_claims
# ---------------------------------------------------------------------------


def test_record_analogue_claims_appends_frozen_forecast_and_is_idempotent(tmp_path):
    db = tmp_path / "atlas.db"
    migrate_proposals(db)
    migrate_compound(str(db))
    risk_ids = ["risk-a", "risk-b"]
    pair = pair_id_for(risk_ids)
    analogue_id = _approved_analogue(db, _analogue_candidate(pair, risk_ids))
    # two realizations for the pair: the frozen forecast must come from the
    # LATEST scored_at, not the first row found
    _insert_compound(db, pair, risk_ids, p_hat=0.11,
                     scored_at="2026-07-01T00:00:00+00:00")
    _insert_compound(db, pair, risk_ids, p_hat=0.33,
                     scored_at="2026-08-01T00:00:00+00:00")

    ledger = tmp_path / "ledger.jsonl"
    assert jl.record_analogue_claims(db, ledger) == 1

    entries = _read_lines(ledger)
    assert len(entries) == 1
    row = entries[0]
    assert row["kind"] == "analogue"
    assert row["claim"]["analogue_id"] == analogue_id
    assert row["claim"]["pair_id"] == pair
    assert row["claim"]["co_fired"] is True
    assert row["claim_hash"] == jl.hash_object(row["claim"])
    frozen = row["frozen_forecast"]
    assert frozen["p_hat"] == 0.33  # latest realization wins
    assert frozen["pair_id"] == pair
    assert row["elicitor"]["model_id"] == "mock-model"
    assert row["human_decision"]["reviewer"] == "alice"
    assert row["resolution"] is None and row["resolution_ts"] is None
    ok, detail = jl.verify_chain(ledger)
    assert ok, detail

    # second run: already ledgered -> nothing appended
    assert jl.record_analogue_claims(db, ledger) == 0
    assert len(_read_lines(ledger)) == 1


def test_record_analogue_claims_skips_without_realization(tmp_path, caplog):
    db = tmp_path / "atlas.db"
    migrate_proposals(db)
    migrate_compound(str(db))
    risk_ids = ["risk-x", "risk-y"]
    pair = pair_id_for(risk_ids)
    analogue_id = _approved_analogue(db, _analogue_candidate(pair, risk_ids))
    # no compound_scenarios row for this pair

    ledger = tmp_path / "ledger.jsonl"
    with caplog.at_level(logging.INFO, logger="workbench_atlas.scoring.judgment_ledger"):
        assert jl.record_analogue_claims(db, ledger) == 0
    assert _read_lines(ledger) == []
    assert any(
        "no deterministic realization" in r.getMessage()
        and analogue_id in r.getMessage()
        for r in caplog.records
    )


# ---------------------------------------------------------------------------
# resolve_claims
# ---------------------------------------------------------------------------


def _events_abcdef():
    # train quarters 0..5, holdout 6..7. Pair (r-a, r-b) co-fires in q6;
    # r-c fires without r-d (never co-fires).
    return [
        IncidentEvent(event_id="e1", risk_id="r-a", period=6, loss_amount=1.0),
        IncidentEvent(event_id="e2", risk_id="r-b", period=6, loss_amount=1.0),
        IncidentEvent(event_id="e3", risk_id="r-a", period=7, loss_amount=1.0),
        IncidentEvent(event_id="e4", risk_id="r-c", period=6, loss_amount=1.0),
    ]


def test_resolve_claims_appends_resolution_and_keeps_claims_immutable(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    claim_a = _claim_entry(ledger, ["r-a", "r-b"], p_hat=0.5, analogue_id="ana-1")
    claim_b = _claim_entry(ledger, ["r-c", "r-d"], p_hat=0.2, analogue_id="ana-2")
    events = _events_abcdef()

    # window not closed yet: no observed quarter past the train end
    pre = jl.resolve_claims(ledger, events, current_period=5, train_end_period=5)
    assert pre["resolved"] == 0
    assert pre["open"] == 2
    assert len(_read_lines(ledger)) == 2

    summary = jl.resolve_claims(ledger, events, current_period=7, train_end_period=5)
    assert summary["resolved"] == 2
    assert summary["open"] == 0
    by_id = {r["resolves_entry_id"]: r for r in summary["resolutions"]}
    assert by_id[claim_a["entry_id"]]["co_fired"] is True
    assert by_id[claim_a["entry_id"]]["resolved_periods"] == [6, 7]
    assert by_id[claim_b["entry_id"]]["co_fired"] is False

    entries = _read_lines(ledger)
    assert len(entries) == 4  # 2 claims + 2 resolutions
    resolutions = [e for e in entries if e["kind"] == "resolution"]
    assert {r["resolves_entry_id"] for r in resolutions} == {
        claim_a["entry_id"], claim_b["entry_id"]
    }
    # claims themselves are immutable: resolution stays null on the claim row
    for e in entries:
        if e["kind"] == "analogue":
            assert e["resolution"] is None
            assert e["resolution_ts"] is None
    ok, detail = jl.verify_chain(ledger)
    assert ok, detail

    # already-resolved claims are not re-resolved
    again = jl.resolve_claims(ledger, events, current_period=7, train_end_period=5)
    assert again["resolved"] == 0
    assert again["already_resolved"] == 2
    assert len(_read_lines(ledger)) == 4


# ---------------------------------------------------------------------------
# calibration_report
# ---------------------------------------------------------------------------


def test_calibration_report_brier_computed_by_hand(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    _claim_entry(ledger, ["r-a", "r-b"], p_hat=0.5, analogue_id="ana-1")
    _claim_entry(ledger, ["r-c", "r-d"], p_hat=0.2, analogue_id="ana-2")
    jl.resolve_claims(ledger, _events_abcdef(), current_period=7, train_end_period=5)
    # ledgered after the resolution pass -> open until the next quarterly job
    _claim_entry(ledger, ["r-e", "r-f"], p_hat=0.9, analogue_id="ana-3")

    report = jl.calibration_report(ledger)
    assert report["n_resolved"] == 2
    assert report["n_open"] == 1
    # hand: (0.5-1)^2 = 0.25, (0.2-0)^2 = 0.04 -> mean 0.145
    assert report["brier"] == pytest.approx(0.145)
    assert report["status"] == "ok"
    assert sum(report["resolved_per_quarter"].values()) == 2
    assert "deterministic" in report["brier_note"].lower()
    md = jl.render_report_md(report)
    assert "0.1450" in md
    assert "resolved claims: 2" in md


def test_calibration_report_zero_resolved_is_honest(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    _claim_entry(ledger, ["r-a", "r-b"], p_hat=0.5, analogue_id="ana-1")  # never resolved
    report = jl.calibration_report(ledger)
    assert report["n_resolved"] == 0
    assert report["n_open"] == 1
    assert report["brier"] is None
    assert report["status"] == "insufficient_data"
    assert "insufficient data" in report["note"].lower()
    assert "n_resolved=0" in report["note"]
    md = jl.render_report_md(report)
    assert "insufficient data" in md.lower()

    # missing ledger file: same honest n=0, no exception
    empty = jl.calibration_report(tmp_path / "nope.jsonl")
    assert empty["n_resolved"] == 0
    assert empty["brier"] is None


# ---------------------------------------------------------------------------
# CLI smoke: atlas-eval ledger end-to-end on a tmp fixture
# ---------------------------------------------------------------------------


def test_cli_ledger_end_to_end(tmp_path):
    slug_a, slug_b = "ledger-alpha-identity", "ledger-beta-cloud"
    # two risks, 8 quarterly buckets; both fire in holdout quarters 6-7.
    # titles slugify exactly to the risk_ids above (load_risk_atoms).
    risks = [
        {
            "issue": slug_a.replace("-", " "),
            "review_status": "approved",
            "description": "identity infrastructure",
            "evidence_refs": [f"EA{q}" for q in range(8)],
            "source_type": "synthetic",
        },
        {
            "issue": slug_b.replace("-", " "),
            "review_status": "approved",
            "description": "cloud region infrastructure",
            "evidence_refs": [f"EB{q}" for q in (6, 7)],
            "source_type": "synthetic",
        },
    ]
    risks_path = tmp_path / "emerging_risks.json"
    risks_path.write_text(json.dumps(risks))

    ev_db = tmp_path / "event_evidence.db"
    conn = sqlite3.connect(ev_db)
    conn.execute(
        "CREATE TABLE evidence_events (event_id TEXT, source_date TEXT, "
        "loss_amount_usd REAL, review_status TEXT DEFAULT 'approved')"
    )
    rows = []
    for q in range(8):  # same quarter->date math as test_compound_join
        month = q * 3 + 2
        year = 2024 + (month - 1) // 12
        month = (month - 1) % 12 + 1
        rows.append((f"EA{q}", f"{year}-{month:02d}-15", None))
        if q in (6, 7):
            rows.append((f"EB{q}", f"{year}-{month:02d}-16", None))
    conn.executemany(
        "INSERT INTO evidence_events (event_id, source_date, loss_amount_usd) VALUES (?,?,?)",
        rows,
    )
    conn.commit()
    conn.close()

    db = tmp_path / "atlas.db"
    migrate_proposals(db)
    migrate_compound(str(db))
    pair = pair_id_for([slug_a, slug_b])
    _approved_analogue(db, _analogue_candidate(pair, [slug_a, slug_b]))
    _insert_compound(db, pair, [slug_a, slug_b], p_hat=0.4)

    ledger = tmp_path / "judgment" / "ledger.jsonl"
    rc = eval_main([
        "ledger",
        "--db", str(db),
        "--ledger", str(ledger),
        "--risks", str(risks_path),
        "--evidence-db", str(ev_db),
    ])
    assert rc == 0

    entries = _read_lines(ledger)
    kinds = [e["kind"] for e in entries]
    assert kinds == ["analogue", "resolution"]  # no flags = record+resolve+report
    resolution = entries[1]["resolution"]
    assert resolution["co_fired"] is True
    assert resolution["resolved_periods"] == [6, 7]
    ok, detail = jl.verify_chain(ledger)
    assert ok, detail

    report = json.loads((tmp_path / "judgment" / "judgment_report.json").read_text())
    assert report["n_resolved"] == 1
    assert report["brier"] == pytest.approx((0.4 - 1.0) ** 2)
    assert (tmp_path / "judgment" / "judgment_report.md").exists()
