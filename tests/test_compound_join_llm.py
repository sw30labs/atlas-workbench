"""Dark-matter LLM compound join (docs/research/llm-compound-join.md).

Covers the parse-time discipline (dark-matter rule, mandatory mechanism /
signature / disanalogy, citation clamp), the end-to-end run on a dense
synthetic series (engine scores from the production-fitted scorer, review
proposals filed pending, ledger claims frozen, graduation freeze), ledger
idempotence and resolution, the insufficiency gate, the honest empty run,
and the fiction canary. No live LLM, no network: clients and citation
resolvers are injected mocks.
"""

from __future__ import annotations

import json
import sqlite3
from itertools import combinations

import pytest

from workbench_atlas.compound_join import HOLDOUT_QUARTERS, migrate_compound
from workbench_atlas.compound_join_llm import (
    JOIN_ARTIFACT_NAME,
    propose_joins,
    run_llm_compound_join,
)
from workbench_atlas.config import Settings
from workbench_atlas.proposals import list_proposals, migrate_proposals
from workbench_atlas.scoring.judgment_ledger import (
    calibration_report,
    resolve_claims,
    verify_chain,
)
from workbench_atlas.scoring.r1 import build_signal_series
from workbench_atlas.scoring.scorer import ScorerConfig

FAST = ScorerConfig(mc_draws=300, interval_draws=300)


# ── fixtures (dense-series pattern from test_compound_join.py) ──────────────


def _make_settings(tmp_path, risks, evidence_rows):
    risks_path = tmp_path / "emerging_risks.json"
    risks_path.write_text(json.dumps(risks))
    ev_db = tmp_path / "event_evidence.db"
    conn = sqlite3.connect(ev_db)
    conn.execute(
        "CREATE TABLE evidence_events (event_id TEXT, source_date TEXT, loss_amount_usd REAL, review_status TEXT DEFAULT 'approved')"
    )
    conn.executemany("INSERT INTO evidence_events (event_id, source_date, loss_amount_usd) VALUES (?,?,?)", evidence_rows)
    conn.commit()
    conn.close()
    return Settings(
        scenario_db_path=tmp_path / "atlas.db",
        evidence_db_path=ev_db,
        emerging_risks_path=risks_path,
    )


def _dense_fixture(tmp_path):
    """6 risks, one linked evidence event per quarter across 8 quarters.

    Two channel groups so the join has visible pairs to score:
    identity (0,1,3) and cloud (2,4,5) — cross pairs are dark matter."""
    channels = {0: "identity", 1: "identity", 2: "cloud", 3: "identity", 4: "cloud", 5: "cloud"}
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
            if q == (i % 8):  # each risk is silent in one quarter
                continue
            month = q * 3 + 2
            year = 2024 + (month - 1) // 12
            month = (month - 1) % 12 + 1
            rows.append((f"E{i}q{q}", f"{year}-{month:02d}-15", 10_000.0 * (i + 1)))
    return risks, rows


def _atoms(settings):
    atoms, events, _diag = build_signal_series(
        settings.emerging_risks_path, settings.evidence_db_path
    )
    assert atoms and events
    return atoms


def _pair(atoms, *, shared_channel: bool):
    for a, b in combinations(atoms, 2):
        if bool(set(a.channels) & set(b.channels)) == shared_channel:
            return a, b
    raise AssertionError(f"fixture has no pair with shared_channel={shared_channel}")


# ── mock clients (OMLXClient-shaped; no network) ────────────────────────────


class _ScriptedClient:
    def __init__(self, payload):
        self._payload = payload
        self.settings = Settings()

    def chat_json(self, messages, **kwargs):
        return self._payload

    def is_available(self):
        return True

    def list_models(self):
        return ["mock-model"]

    def close(self):
        pass


class _ConfabulatingClient:
    """Parses the inventory out of the prompt and always proposes a
    cross-channel join — for ANY world, including fictional ones. The
    canary's measured confabulator."""

    def __init__(self):
        self.settings = Settings()

    def chat_json(self, messages, **kwargs):
        content = messages[-1]["content"]
        body = content.split(":\n", 1)[1].rsplit("\n\nReturn the JSON object.", 1)[0]
        inventory = json.loads(body)["inventory"]
        a, b = inventory[0], inventory[1]
        for x, y in combinations(inventory, 2):
            if not set(x["channels"]) & set(y["channels"]):
                a, b = x, y
                break
        return {
            "joins": [
                {
                    "risk_id_a": a["risk_id"],
                    "risk_id_b": b["risk_id"],
                    "mechanism": "confabulated shared dependency",
                    "signature": "both co-fire within 2 quarters",
                    "disanalogy": "invented for the test",
                }
            ]
        }

    def is_available(self):
        return True

    def list_models(self):
        return ["mock-model"]

    def close(self):
        pass


def _join(a, b, **overrides):
    join = {
        "risk_id_a": a.risk_id,
        "risk_id_b": b.risk_id,
        "mechanism": "shared failover dependency on the same clearing utility",
        "signature": "both atoms co-fire within 2 quarters if the coupling is real",
        "disanalogy": "different geographies; drivers may stay decoupled",
        "citation_url": "https://example.com/clearing",
    }
    join.update(overrides)
    return join


RESOLVE = lambda url: True  # noqa: E731 — injected citation resolver
REJECT = lambda url: False  # noqa: E731


# ── parse-time discipline ───────────────────────────────────────────────────


def test_parse_keeps_only_dark_matter_with_mandatory_fields(tmp_path):
    settings = _make_settings(tmp_path, *_dense_fixture(tmp_path))
    atoms = _atoms(settings)
    dark_a, dark_b = _pair(atoms, shared_channel=False)
    vis_a, vis_b = _pair(atoms, shared_channel=True)
    visible = {tuple(sorted((vis_a.risk_id, vis_b.risk_id)))}
    payload = {
        "joins": [
            _join(dark_a, dark_b),                                     # survives
            _join(dark_a, dark_b),                                     # duplicate
            _join(vis_a, vis_b),                                       # visible pair
            _join(dark_a, dark_a),                                     # self pair
            {**_join(dark_a, dark_b), "risk_id_b": "no-such-atom"},    # unknown atom
            {k: v for k, v in _join(dark_a, dark_b).items() if k != "signature"},
            {k: v for k, v in _join(dark_a, dark_b).items() if k != "disanalogy"},
            {k: v for k, v in _join(dark_a, dark_b).items() if k != "mechanism"},
        ]
    }
    out = propose_joins(
        atoms, _ScriptedClient(payload), visible_pairs=visible, resolver=RESOLVE
    )
    assert out["error"] is None
    assert len(out["joins"]) == 1
    assert out["dropped"] == 7
    join = out["joins"][0]
    assert join["risk_ids"] == sorted((dark_a.risk_id, dark_b.risk_id))
    assert join["citation_status"] == "resolved"
    assert join["status"] == "pending"
    assert join["engine"] is None
    assert out["elicitor"]["prompt_version"]
    assert out["elicitor"]["prompt_hash"]


def test_parse_unresolvable_citation_auto_rejects(tmp_path):
    settings = _make_settings(tmp_path, *_dense_fixture(tmp_path))
    atoms = _atoms(settings)
    dark_a, dark_b = _pair(atoms, shared_channel=False)
    out = propose_joins(
        atoms,
        _ScriptedClient({"joins": [_join(dark_a, dark_b)]}),
        visible_pairs=set(),
        resolver=REJECT,
    )
    assert len(out["joins"]) == 1
    assert out["joins"][0]["citation_status"] == "unresolvable"
    assert out["joins"][0]["status"] == "auto_rejected"


def test_parse_model_failure_is_recorded_not_raised(tmp_path):
    class _BrokenClient(_ScriptedClient):
        def chat_json(self, messages, **kwargs):
            raise RuntimeError("backend down")

    settings = _make_settings(tmp_path, *_dense_fixture(tmp_path))
    atoms = _atoms(settings)
    out = propose_joins(atoms, _BrokenClient({}), visible_pairs=set(), resolver=RESOLVE)
    assert out["joins"] == []
    assert "backend down" in out["error"]


# ── end-to-end run ──────────────────────────────────────────────────────────


def _setup(tmp_path):
    """Dense world + proposal store, built exactly once per tmp_path."""
    settings = _make_settings(tmp_path, *_dense_fixture(tmp_path))
    migrate_proposals(settings.scenario_db_path)
    return settings


def _kwargs(tmp_path):
    return dict(scorer_config=FAST, resolver=RESOLVE, ledger_path=tmp_path / "ledger.jsonl")


def test_run_end_to_end_shadow_discipline(tmp_path):
    settings = _setup(tmp_path)
    atoms = _atoms(settings)
    dark_a, dark_b = _pair(atoms, shared_channel=False)
    vis_a, vis_b = _pair(atoms, shared_channel=True)
    client = _ScriptedClient({"joins": [_join(dark_a, dark_b), _join(vis_a, vis_b)]})
    r = run_llm_compound_join(settings, client, **_kwargs(tmp_path))

    assert r.status == "ok", r.errors
    assert r.n_proposed == 1 and r.n_dropped == 1  # the visible pair died
    assert r.n_scored == 1
    assert r.n_proposals_created == 1
    assert r.n_claims_frozen == 1

    # artifact: engine numbers, never LLM numbers
    artifact = settings.emerging_risks_path.parent / JOIN_ARTIFACT_NAME
    doc = json.loads(artifact.read_text())
    assert doc["graduation_freeze"] is True
    assert len(doc["joins"]) == 1
    join = doc["joins"][0]
    assert join["engine"] is not None
    assert 0.0 <= join["engine"]["p_hat"] <= 1.0
    assert join["signature"].startswith("both atoms co-fire")
    assert join["review_status"] == "pending"

    # review lane: a pending edge_add proposal carries the mechanism
    props = list_proposals(settings.scenario_db_path, kind="channel", status="pending")
    assert len(props) == 1
    assert props[0]["kind"] == "edge_add"
    assert sorted(props[0]["risk_ids"]) == sorted((dark_a.risk_id, dark_b.risk_id))

    # ledger lane: one llm_join claim, frozen engine forecast, chain intact
    entries = [
        json.loads(line)
        for line in (tmp_path / "ledger.jsonl").read_text().splitlines()
        if line.strip()
    ]
    claims = [e for e in entries if e["kind"] == "llm_join"]
    assert len(claims) == 1
    claim = claims[0]
    assert claim["claim"]["signature"] == join["signature"]
    assert claim["claim"]["risk_ids"] == sorted((dark_a.risk_id, dark_b.risk_id))
    assert claim["human_decision"] is None  # decision lives on the proposal row
    assert claim["frozen_forecast"]["p_hat"] == pytest.approx(join["engine"]["p_hat"])
    assert claim["frozen_forecast"]["compound_def"] == "all_must_fire"
    ok, detail = verify_chain(tmp_path / "ledger.jsonl")
    assert ok, detail

    # graduation freeze: production compound stores were never touched
    migrate_compound(str(settings.scenario_db_path))
    conn = sqlite3.connect(str(settings.scenario_db_path))
    try:
        n_compounds = conn.execute("SELECT COUNT(*) FROM compound_scenarios").fetchone()[0]
        n_realizations = conn.execute("SELECT COUNT(*) FROM score_realizations").fetchone()[0]
    finally:
        conn.close()
    assert n_compounds == 0
    assert n_realizations == 0


def test_guard_rejected_join_is_marked_and_kept_out_of_the_ledger(
    tmp_path, monkeypatch
):
    """create_channel_proposals runs the ontology guard and DROPS rejected
    rows rather than raising, so a join can score yet reach no review queue.

    Left unmarked it renders as "awaiting review" while sitting in no queue —
    a claim that looks dispositionable but that no human can ever reach, and a
    frozen ledger claim with no disposition path.
    """
    import workbench_atlas.compound_join_llm as mod

    settings = _setup(tmp_path)
    atoms = _atoms(settings)
    dark_a, dark_b = _pair(atoms, shared_channel=False)
    client = _ScriptedClient({"joins": [_join(dark_a, dark_b)]})

    # Simulate the door refusing the row: nothing stored, nothing raised.
    monkeypatch.setattr(
        mod, "create_channel_proposals",
        lambda *a, **k: {
            "created": 0, "skipped_existing_pending": 0, "proposal_ids": [],
        },
    )
    r = run_llm_compound_join(settings, client, **_kwargs(tmp_path))

    assert r.status == "ok"
    assert r.n_scored == 1
    assert r.n_proposals_created == 0
    assert r.n_claims_frozen == 0          # not pre-registered
    assert any("ontology door" in e for e in r.errors)

    doc = json.loads(
        (settings.emerging_risks_path.parent / JOIN_ARTIFACT_NAME).read_text()
    )
    assert doc["joins"][0]["review_status"] == "guard_rejected"
    assert "proposal_id" not in doc["joins"][0]


def test_join_artifact_is_gitignored():
    """The artifact holds governed register titles and model prose, and lands in
    the workspace root — untracked and unignored, it becomes a public-release
    candidate scanned against every forbidden-content rule."""
    import subprocess
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    message = (
        f"{JOIN_ARTIFACT_NAME} is not ignored; every sibling runtime "
        "artifact is (see .gitignore)"
    )
    if (root / ".git").exists():
        proc = subprocess.run(
            ["git", "check-ignore", JOIN_ARTIFACT_NAME],
            cwd=root, capture_output=True,
        )
        assert proc.returncode == 0, message
    else:
        # An sdist or archive export has no git metadata; assert the rule is
        # present rather than skipping, so the guarantee still has cover.
        rules = (root / ".gitignore").read_text().split()
        assert f"/{JOIN_ARTIFACT_NAME}" in rules, message


def test_ledger_idempotent_on_rerun(tmp_path):
    settings = _setup(tmp_path)
    atoms = _atoms(settings)
    dark_a, dark_b = _pair(atoms, shared_channel=False)
    client = _ScriptedClient({"joins": [_join(dark_a, dark_b)]})
    r1 = run_llm_compound_join(settings, client, **_kwargs(tmp_path))
    assert r1.n_claims_frozen == 1
    # same join proposed again: same (pair_id, signature) — nothing refrozen,
    # and the pending proposal dedups too
    r2 = run_llm_compound_join(settings, client, **_kwargs(tmp_path))
    assert r2.n_claims_frozen == 0
    assert r2.n_proposals_created == 0


def test_resolution_and_report_cover_llm_join(tmp_path):
    settings = _setup(tmp_path)
    atoms = _atoms(settings)
    dark_a, dark_b = _pair(atoms, shared_channel=False)
    client = _ScriptedClient({"joins": [_join(dark_a, dark_b)]})
    r = run_llm_compound_join(settings, client, **_kwargs(tmp_path))
    assert r.status == "ok"

    _atoms2, events, _diag = build_signal_series(
        settings.emerging_risks_path, settings.evidence_db_path
    )
    n_periods = max(e.period for e in events) + 1
    summary = resolve_claims(
        tmp_path / "ledger.jsonl",
        events,
        current_period=n_periods - 1,
        train_end_period=n_periods - HOLDOUT_QUARTERS - 1,
    )
    assert summary["resolved"] == 1
    # dense fixture: both atoms fire in the holdout quarters
    assert summary["resolutions"][0]["co_fired"] is True

    report = calibration_report(tmp_path / "ledger.jsonl")
    assert report["status"] == "ok"
    assert report["n_resolved"] == 1
    assert report["brier"] is not None
    assert report["claims"][0]["outcome"] == 1.0


def test_insufficient_data_records_honestly(tmp_path):
    risks = [
        {"issue": "lonely risk", "description": "sparse", "evidence_refs": ["E1"],
         "review_status": "approved", "source_type": "synthetic"}
    ]
    rows = [("E1", "2024-01-15", 1000.0)]
    settings = _make_settings(tmp_path, risks, rows)
    r = run_llm_compound_join(
        settings, _ConfabulatingClient(), scorer_config=FAST,
        resolver=RESOLVE, ledger_path=tmp_path / "ledger.jsonl",
    )
    assert r.status == "insufficient_data"
    assert r.note
    assert not (settings.emerging_risks_path.parent / JOIN_ARTIFACT_NAME).exists()
    assert not (tmp_path / "ledger.jsonl").exists()


def test_no_joins_is_an_honest_run(tmp_path):
    settings = _setup(tmp_path)
    r = run_llm_compound_join(settings, _ScriptedClient({"joins": []}), **_kwargs(tmp_path))
    assert r.status == "no_joins"
    assert r.n_proposed == 0
    assert "good result" in r.note
    doc = json.loads(
        (settings.emerging_risks_path.parent / JOIN_ARTIFACT_NAME).read_text()
    )
    assert doc["joins"] == []


def test_signature_is_mandatory_end_to_end(tmp_path):
    settings = _setup(tmp_path)
    atoms = _atoms(settings)
    dark_a, dark_b = _pair(atoms, shared_channel=False)
    no_signature = {k: v for k, v in _join(dark_a, dark_b).items() if k != "signature"}
    r = run_llm_compound_join(settings, _ScriptedClient({"joins": [no_signature]}), **_kwargs(tmp_path))
    assert r.status == "no_joins"
    assert r.n_dropped == 1
    assert r.n_claims_frozen == 0
    doc = json.loads(
        (settings.emerging_risks_path.parent / JOIN_ARTIFACT_NAME).read_text()
    )
    assert doc["joins"] == []


# ── canary + runner registration ────────────────────────────────────────────


def test_fiction_canary_measures_confabulation(tmp_path):
    settings = _setup(tmp_path)
    r = run_llm_compound_join(settings, _ConfabulatingClient(), canary=True, **_kwargs(tmp_path))
    assert r.status == "ok"
    assert r.canary is not None
    # the confabulating mock invents a join for the fictional world too —
    # every one of them is a measured confabulation
    assert r.canary["n_joins"] >= 1
    assert r.canary["confabulated"] == r.canary["n_joins"]


def test_runner_phase_registered():
    from workbench_atlas.web.runner import VALID_PHASES

    assert "compound_join_llm" in VALID_PHASES


# ── human steer (directed search) ────────────────────────────────────────


def test_steer_marks_the_run_directed_and_travels_with_every_claim(tmp_path):
    """A steered run is a DIRECTED search. If the record cannot tell it from
    the undirected sweep, the ledger's hit rate becomes a p-hacked number —
    so the steer, its hash and the elicitation mode ride on the run, the
    artifact and each join."""
    settings = _setup(tmp_path)
    atoms = _atoms(settings)
    a, b = _pair(atoms, shared_channel=False)
    client = _ScriptedClient({"joins": [_join(a, b)]})
    steer = "cheap inference on old GPUs triggers a flight from AI infrastructure"

    r = run_llm_compound_join(settings, client, steer=steer, **_kwargs(tmp_path))

    assert r.elicitation == "directed"
    assert r.steer == steer
    doc = json.loads(
        (settings.emerging_risks_path.parent / JOIN_ARTIFACT_NAME).read_text()
    )
    assert doc["elicitation"] == "directed"
    assert doc["steer"] == steer
    assert doc["elicitor"]["elicitation"] == "directed"
    assert doc["elicitor"]["steer_hash"]
    assert doc["joins"][0]["elicitation"] == "directed"
    assert doc["joins"][0]["steer_hash"] == doc["elicitor"]["steer_hash"]


def test_unsteered_run_is_marked_undirected_and_carries_no_steer(tmp_path):
    settings = _setup(tmp_path)
    atoms = _atoms(settings)
    a, b = _pair(atoms, shared_channel=False)
    r = run_llm_compound_join(
        settings, _ScriptedClient({"joins": [_join(a, b)]}), **_kwargs(tmp_path)
    )
    assert r.elicitation == "undirected"
    assert r.steer == ""
    doc = json.loads(
        (settings.emerging_risks_path.parent / JOIN_ARTIFACT_NAME).read_text()
    )
    assert doc["elicitation"] == "undirected"
    assert "steer" not in doc["elicitor"]
    assert "steer_hash" not in doc["joins"][0]


def test_steer_does_not_relax_intake(tmp_path):
    """The failure this feature must not have: a leading question buying a
    join that would otherwise be dropped. Mandatory fields stay mandatory."""
    settings = _setup(tmp_path)
    atoms = _atoms(settings)
    a, b = _pair(atoms, shared_channel=False)
    bad = _join(a, b)
    bad.pop("disanalogy")           # the disconfirming-evidence slot
    r = run_llm_compound_join(
        settings, _ScriptedClient({"joins": [bad]}),
        steer="I am certain these two are coupled", **_kwargs(tmp_path),
    )
    assert r.n_proposed == 0, "a steer must not buy a join past intake"
    assert r.n_dropped == 1


def test_steer_is_capped_and_normalized():
    """Untrusted free text: bounded so it cannot become a second prompt."""
    from workbench_atlas.compound_join_llm import MAX_STEER_CHARS, propose_joins

    seen = {}

    class _Capture:
        settings = None
        def chat_json(self, messages, **k):
            seen["content"] = messages[-1]["content"]
            return {"joins": []}
        def list_models(self): return ["m"]
        def close(self): pass

    class _A:
        def __init__(self, i, t, c):
            self.risk_id, self.title, self.channels = i, t, c

    out = propose_joins(
        [_A("a", "A", ["cloud"]), _A("b", "B", ["model"])], _Capture(),
        visible_pairs=set(), steer="  " + ("x" * (MAX_STEER_CHARS + 500)) + "  ",
    )
    assert len(out["elicitor"]["steer"]) == MAX_STEER_CHARS
    # The trailing sentinel is a parsing contract for the canary's client.
    assert seen["content"].endswith("\n\nReturn the JSON object.")
