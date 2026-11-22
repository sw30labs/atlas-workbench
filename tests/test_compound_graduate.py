"""Tests for compound graduation (Phase 8)."""

import json
import sqlite3

import pytest

from workbench_atlas.compound_graduate import (
    eligible_compounds,
    migrate_graduations,
    run_compound_graduation,
)
from workbench_atlas.compound_join import migrate_compound
from workbench_atlas.config import Settings
from workbench_atlas.schema import migrate
from workbench_atlas.scoring.models import pair_id_for


def _make_settings(tmp_path, risks, evidence_rows):
    risks_path = tmp_path / "emerging_risks.json"
    risks = [{**r, "review_status": r.get("review_status", "approved")} for r in risks]
    risks_path.write_text(json.dumps(risks))
    ev_db = tmp_path / "event_evidence.db"
    conn = sqlite3.connect(ev_db)
    conn.execute("CREATE TABLE evidence_events (event_id TEXT, source_date TEXT, loss_amount_usd REAL, review_status TEXT DEFAULT 'approved')")
    conn.executemany("INSERT INTO evidence_events (event_id, source_date, loss_amount_usd) VALUES (?,?,?)", evidence_rows)
    conn.commit()
    conn.close()
    db = tmp_path / "atlas.db"
    migrate(str(db))
    migrate_compound(str(db))
    migrate_graduations(str(db))
    return Settings(
        scenario_db_path=db,
        evidence_db_path=ev_db,
        emerging_risks_path=risks_path,
    )


RISKS = [
    {"issue": "identity plane concentration", "description": "identity dependency",
     "evidence_refs": ["EA1", "EA2", "EA3", "EA4", "EA5", "EA6"], "source_type": "synthetic"},
    {"issue": "directory replication conflict", "description": "identity directory",
     "evidence_refs": ["EB1", "EB2", "EB3", "EB4", "EB5", "EB6"], "source_type": "synthetic"},
]


def _evidence_rows(cooccur_in_holdout: bool):
    """6 quarters of activations for A and B. When cooccur_in_holdout, A and B
    share the last two quarters."""
    rows = []
    quarters = [(2024, 2), (2024, 5), (2024, 8), (2024, 11), (2025, 2), (2025, 5)]
    for i, (y, m) in enumerate(quarters):
        rows.append((f"EA{i + 1}", f"{y}-{m:02d}-15", 1000.0))
        rows.append((f"EB{i + 1}", f"{y}-{m:02d}-15", 1000.0))
    if not cooccur_in_holdout:
        # B's last two events move to earlier (already-covered) quarters
        rows = [r for r in rows if r[0] not in ("EB5", "EB6")]
    return rows


def _compound_row(settings, risk_ids=("identity-plane-concentration", "directory-replication-conflict")):
    conn = sqlite3.connect(settings.scenario_db_path)
    conn.execute(
        "INSERT INTO compound_scenarios (compound_id, risk_ids, state_hash,"
        " p_hat, p_lo, p_hi, loss_q50, loss_q95, loss_mean, uncertainty_class,"
        " dependence_support, evidence_support, data_regime, status, run_id,"
        " scored_at, algorithm_version, provenance_note, pair_id, loss_units)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "scn-testcompound", json.dumps(sorted(risk_ids)), "statehash123",
            0.42, 0.20, 0.60, 1e5, 9e5, 3e5, "mixed", 4, 0.7, "R1",
            "candidate", "compound-test", "2026-08-01T00:00:00+00:00",
            "0.1.0", "test row",
            pair_id_for(sorted(risk_ids)), "dollars",
        ),
    )
    conn.commit()
    conn.close()


class GradMockClient:
    """Routes doc-drafting vs judge prompts by content."""

    def __init__(self, judge_pass=True):
        self.judge_pass = judge_pass
        self.settings = Settings()

    def chat_json(self, messages, **kwargs):
        user = messages[-1]["content"]
        if "Write the scenario document" in user:
            return {
                "title": "Identity concentration + directory conflict compound",
                "description": "Joint materialization scenario.",
                "transmission_mechanism": "Shared identity channel.",
                "defensive_options": "Dual control planes.",
                "detailed_rationale": "4 co-occurring quarters; mixed uncertainty.",
            }
        return {
            "score": 0.85 if self.judge_pass else 0.5,
            "criteria": {},
            "feedback": "ok" if self.judge_pass else "weak",
            "passed": self.judge_pass,
        }

    def close(self):
        pass


@pytest.fixture()
def settings_with_compound(tmp_path):
    settings = _make_settings(tmp_path, RISKS, _evidence_rows(cooccur_in_holdout=True))
    _compound_row(settings)
    return settings


def test_eligibility_requires_holdout_survival(settings_with_compound):
    eligible, n = eligible_compounds(settings_with_compound)
    assert n == 1 and len(eligible) == 1


def test_eligibility_rejects_in_sample_only(tmp_path):
    settings = _make_settings(tmp_path, RISKS, _evidence_rows(cooccur_in_holdout=False))
    _compound_row(settings)
    eligible, n = eligible_compounds(settings)
    assert n == 1 and len(eligible) == 0


def test_graduation_writes_scenario_with_provenance(settings_with_compound):
    result = run_compound_graduation(settings_with_compound, client=GradMockClient())
    assert result.status == "ok", result.note
    assert result.n_graduated == 1
    conn = sqlite3.connect(settings_with_compound.scenario_db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM scenarios WHERE scenario_id LIKE 'ATLAS-CMP-%'").fetchone()
    ledger = conn.execute("SELECT * FROM compound_graduations").fetchone()
    conn.close()
    assert row is not None
    assert row["origin"] == "compound_graduation"
    assert row["likelihood"] is None  # public-report probability is not operational likelihood
    scoped = json.loads(row["additional_information"])
    assert scoped["signal_co_activation_band"] == "Moderate"
    assert scoped["observation_scope"] == "public_signal_co_activation"
    assert row["financial_impact_band"] == "Limited"  # q95 banded per docs/reference/TAXONOMY.md
    assert "identity plane concentration" in row["core_risks"]
    info = json.loads(row["additional_information"])
    assert info["origin"] == "compound_graduation"
    assert info["compound_id"] == "scn-testcompound"
    assert info["p_hat"] == 0.42  # raw estimate preserved here, never in the enum field
    assert info["p_interval"] == [0.2, 0.6]
    assert row["judge_score"] == 0.85
    assert ledger["compound_id"] == "scn-testcompound"
    assert json.loads(ledger["eligibility"])["holdout_survival"] is True


def test_graduation_idempotent(settings_with_compound):
    first = run_compound_graduation(settings_with_compound, client=GradMockClient())
    assert first.n_graduated == 1
    second = run_compound_graduation(settings_with_compound, client=GradMockClient())
    assert second.status == "none_eligible"
    conn = sqlite3.connect(settings_with_compound.scenario_db_path)
    n = conn.execute("SELECT COUNT(*) FROM scenarios WHERE scenario_id LIKE 'ATLAS-CMP-%'").fetchone()[0]
    conn.close()
    assert n == 1


def test_judge_rejection_blocks_insert(settings_with_compound):
    result = run_compound_graduation(settings_with_compound, client=GradMockClient(judge_pass=False))
    assert result.n_graduated == 0
    assert result.n_judge_rejected == 1
    conn = sqlite3.connect(settings_with_compound.scenario_db_path)
    n = conn.execute("SELECT COUNT(*) FROM scenarios WHERE scenario_id LIKE 'ATLAS-CMP-%'").fetchone()[0]
    conn.close()
    assert n == 0


def test_no_client_is_recorded_error(settings_with_compound):
    result = run_compound_graduation(settings_with_compound, client=None)
    assert result.status == "error"
    assert result.errors


def test_pipeline_graduate_skips_without_llm(tmp_workspace):
    from workbench_atlas.pipeline import run_pipeline
    from workbench_atlas.config import Settings

    settings = Settings(
        scenario_db_path=tmp_workspace / "atlas.db",
        evidence_db_path=tmp_workspace / "event_evidence.db",
        emerging_risks_path=tmp_workspace / "emerging_risks.json",
        scenario_export_path=tmp_workspace / "scenario_export.json",
    )
    result = run_pipeline(settings, skip_llm=True)
    assert result.state["compound_graduate"]["status"] == "skipped"
    assert "compound_graduate" in result.summary()["phases"]
