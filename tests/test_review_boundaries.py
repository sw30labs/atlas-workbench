"""Regression tests for the reviewed-input execution boundary."""

from __future__ import annotations

import inspect
import json

from contingency_atlas.db import add_evidence_event, list_evidence_events
from contingency_atlas.phases import phase3_generate, phase5_patterns


def _add_pending_evidence(db_path: str) -> None:
    inserted = add_evidence_event(
        db_path,
        {
            "event_id": "PENDING-EVT-001",
            "headline": "Pending evidence must remain outside model context",
            "source_date": "2099-01-01",
            "source_url": "https://example.org/pending-evidence",
            "review_status": "pending",
            "rights_review": "pending",
        },
    )
    assert inserted is True


def test_list_evidence_events_approved_only_excludes_pending(tmp_workspace):
    db_path = str(tmp_workspace / "event_evidence.db")
    _add_pending_evidence(db_path)

    rows = list_evidence_events(db_path, approved_only=True)

    assert rows
    assert {row["review_status"] for row in rows} == {"approved_synthetic"}
    assert "PENDING-EVT-001" not in {row["event_id"] for row in rows}


def test_phase3_context_and_references_exclude_pending(tmp_workspace):
    db_path = str(tmp_workspace / "event_evidence.db")
    _add_pending_evidence(db_path)

    context = phase3_generate._build_loss_patterns(db_path, limit=50)
    refs = phase3_generate._collect_evidence_refs(db_path, limit=50)

    assert "Pending evidence" not in context
    assert "PENDING-EVT-001" not in refs
    assert any(ref.startswith("SYN-EVT-") for ref in refs)


def test_phase3_filters_risks_before_applying_limit():
    pending = [
        {
            "issue": f"Pending risk {index}",
            "review_status": "pending",
            "evidence_refs": ["https://example.org/pending"],
        }
        for index in range(20)
    ]
    approved = {
        "issue": "Approved risk",
        "description": "Reviewed research hypothesis.",
        "review_status": "approved",
        "evidence_refs": ["https://example.org/reviewed"],
    }

    context = phase3_generate._format_emerging_risks(
        [*pending, approved],
        limit=1,
    )

    assert "Approved risk" in context
    assert "Pending risk" not in context
    assert phase3_generate._collect_risk_refs([*pending, approved]) == [
        "https://example.org/reviewed"
    ]


def test_phase3_has_no_direct_unreviewed_signal_channel():
    generation_parameters = inspect.signature(
        phase3_generate.run_generation_cycle
    ).parameters
    scenario_parameters = inspect.signature(
        phase3_generate.generate_scenario
    ).parameters

    assert "threat_signals" not in generation_parameters
    assert "threat_signal_refs" not in generation_parameters
    assert "threat_signals" not in scenario_parameters
    assert "threat_signal_refs" not in scenario_parameters
    assert "threat signal" not in phase3_generate.GENERATE_PROMPT.lower()


def test_phase5_receives_only_approved_evidence(tmp_workspace, monkeypatch):
    evidence_db = str(tmp_workspace / "event_evidence.db")
    scenario_db = str(tmp_workspace / "atlas.db")
    _add_pending_evidence(evidence_db)
    captured: dict[str, object] = {}

    def _capture(evidence_events, **kwargs):
        captured["evidence_events"] = evidence_events
        return []

    monkeypatch.setattr(phase5_patterns, "temporal_causality", _capture)
    result = phase5_patterns.run_pattern_analysis(
        scenario_db,
        evidence_db_path=evidence_db,
        use_llm_synthesis=False,
    )

    rows = captured["evidence_events"]
    assert isinstance(rows, list)
    assert rows
    assert {row["review_status"] for row in rows} == {"approved_synthetic"}
    assert result.errors == []


def test_phase5_llm_context_excludes_pending_risks(tmp_workspace, monkeypatch):
    risks_path = tmp_workspace / "phase5-risks.json"
    risks_path.write_text(
        json.dumps(
            [
                {
                    "issue": "Pending risk",
                    "description": "Must remain outside model context.",
                    "review_status": "pending",
                    "evidence_refs": ["https://example.org/pending"],
                },
                {
                    "issue": "Approved risk",
                    "description": "Reviewed research hypothesis.",
                    "review_status": "approved",
                    "evidence_refs": ["https://example.org/reviewed"],
                },
            ]
        )
    )
    captured: dict[str, object] = {}

    def _capture(self, patterns):
        captured["patterns"] = patterns
        return []

    monkeypatch.setattr(phase5_patterns.CausalDeepAgents, "synthesize", _capture)
    result = phase5_patterns.run_pattern_analysis(
        str(tmp_workspace / "atlas.db"),
        client=object(),
        evidence_db_path=str(tmp_workspace / "event_evidence.db"),
        emerging_risks_path=str(risks_path),
        use_llm_synthesis=True,
    )

    context = captured["patterns"]
    assert isinstance(context, list)
    rendered = json.dumps(context)
    assert "Approved risk" in rendered
    assert "Pending risk" not in rendered
    assert result.errors == []
