"""Dashboard coverage for the innovation radar and Atlas Brief."""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import yaml

from workbench_atlas.web import server


INDEX = Path(server.STATIC_DIR) / "index.html"
ABOUT = Path(server.__file__).with_name("about.yaml")


def test_dashboard_contains_accessible_dependency_free_radar_and_brief():
    html = INDEX.read_text()

    assert 'id="risk-radar"' in html
    assert 'role="group"' in html
    assert 'aria-labelledby="radar-title radar-desc"' in html
    assert 'class="radar-signal" role="button" tabindex="0"' in html
    assert 'id="radar-detail" aria-live="polite"' in html
    assert 'aria-label="Search emerging risk radar"' in html
    assert "maturity.inferred" in html
    assert 'source: "unscored"' in html

    for driver in (
        "Technology",
        "Institutional change",
        "Demographic change",
        "Environment",
        "Societal values",
        "Cross-driver convergence",
    ):
        assert driver in html
    for maturity in (
        "weak",
        "emerging",
        "accelerating",
        "established",
        "materialized",
        "fading",
    ):
        assert f'"{maturity}"' in html

    assert 'id="atlas-brief-title"' in html
    assert '$("header.top").setAttribute("inert", "")' in html
    assert 'e.key === "Tab"' in html
    assert "focusable[focusable.length - 1]" in html
    assert "--dim: #8493aa" in html
    assert "--faint: #77869e" in html
    assert "var(--muted)" not in html
    assert "@media (max-width: 760px)" in html
    assert "nav.side {" in html and "overflow-x: auto" in html
    assert "#risk-radar { width: 720px; min-width: 720px" in html
    for heading in (
        "What changed?",
        "Business relevance",
        "Assumptions challenged",
        "What would happen next?",
        "Controls and dependencies to examine",
        "Options and opportunities",
        "What would weaken this scenario?",
        "Discussion prompts",
    ):
        assert heading in html

    # Dynamic radar labels and brief content remain escaped before innerHTML use.
    assert 'aria-label="${esc(aria)}"' in html
    assert "<li>${esc(item)}</li>" in html
    assert "<p>${esc(items[0])}</p>" in html

    script_tags = re.findall(r"<script\b([^>]*)>", html, flags=re.IGNORECASE)
    assert script_tags
    assert all("src=" not in attributes.lower() or 'src="/experience.js"' in attributes
               for attributes in script_tags)


def test_build_payload_passes_enriched_and_legacy_records(monkeypatch, tmp_path):
    scenario_db = tmp_path / "atlas.db"
    evidence_db = tmp_path / "evidence.db"
    risks_path = tmp_path / "emerging_risks.json"

    with sqlite3.connect(scenario_db) as conn:
        conn.execute(
            """
            CREATE TABLE scenarios (
                scenario_id TEXT,
                risk_category TEXT,
                title TEXT,
                status TEXT,
                urgency_score REAL,
                external_drivers TEXT,
                strengthening_signposts TEXT,
                weakening_signposts TEXT,
                affected_capabilities TEXT,
                strategic_options TEXT,
                discussion_prompts TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO scenarios VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "ATLAS-TEST-001",
                "Digital resilience",
                "Shared control-plane disruption",
                "active",
                74.0,
                '["Technology", "Institutional change"]',
                '["Supplier support window shortens"]',
                '["Comparable migrations complete safely"]',
                '["Identity and access management"]',
                '["Remove a concentrated dependency"]',
                '["Which control fails first?"]',
            ),
        )
        conn.execute(
            "CREATE TABLE pipeline_runs (run_id TEXT, phase TEXT, status TEXT)"
        )
        conn.execute(
            "CREATE TABLE pattern_detections (pattern_type TEXT, description TEXT)"
        )
        conn.execute(
            "CREATE TABLE causal_findings (cause_event TEXT, effect_event TEXT)"
        )

    with sqlite3.connect(evidence_db) as conn:
        conn.execute(
            """
            CREATE TABLE evidence_events (
                event_id TEXT,
                headline TEXT,
                loss_amount_usd REAL,
                source_date TEXT,
                risk_category TEXT,
                event_driver_class TEXT,
                primary_cause_class TEXT,
                source_url TEXT,
                review_status TEXT DEFAULT 'approved'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO evidence_events (
                event_id, headline, source_date, risk_category,
                event_driver_class, primary_cause_class, source_url
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "EV-1",
                "Public service disruption",
                "2026-01-01",
                "Digital resilience",
                "Technology disruption",
                "Process design",
                "https://example.org/event/1",
            ),
        )
        conn.execute(
            "INSERT INTO evidence_events (event_id, headline, source_url, review_status) "
            "VALUES ('EV-REJ', 'Rejected event', 'https://example.org/event/2', 'rejected')"
        )

    risks = [
        {
            "issue": "Concentrated identity services",
            "description": "A reviewed signal.",
            "external_drivers": ["Technology", "Institutional change"],
            "signal_maturity": "accelerating",
            "momentum": "accelerating",
            "evidence_strength": "strong",
            "first_observed": "2026-01-02",
            "last_confirmed": "2026-07-20",
            "related_scenarios": ["ATLAS-TEST-001"],
            "evidence_refs": ["EV-1", "EV-2"],
        },
        {
            "issue": "Legacy signal",
            "description": "Predates radar fields.",
            "status": "monitoring",
            "trend": "rising",
            "evidence_refs": [],
        },
        {
            "issue": "Rejected signal",
            "description": "A human rejected this signal; it is not corpus.",
            "review_status": "rejected",
            "evidence_refs": [],
        },
    ]
    risks_path.write_text(json.dumps(risks))

    settings = SimpleNamespace(
        scenario_db_path=scenario_db,
        evidence_db_path=evidence_db,
        emerging_risks_path=risks_path,
        omlx_model="test-model",
        llm_model="test-model",
        llm_provider="omlx",
        auto_retire=False,
    )
    monkeypatch.setattr(server, "get_settings", lambda: settings)

    payload = server.build_payload()

    # Rejected records are not corpus: excluded from counts, kept in payload.
    assert payload["meta"]["risk_count"] == 2
    assert payload["meta"]["evidence_event_count"] == 1
    assert len(payload["risks"]) == 3
    assert len(payload["evidence_events"]) == 2
    assert payload["evidence_events"][0]["ref"] == "EV-1"
    assert (
        payload["evidence_events"][0]["event_driver_class"]
        == "Technology disruption"
    )
    assert (
        payload["evidence_events"][0]["primary_cause_class"]
        == "Process design"
    )
    legacy_collection = "ne" + "ws"
    legacy_count = legacy_collection + "_count"
    assert legacy_collection not in payload
    assert legacy_count not in payload["meta"]
    assert payload["scenarios"][0]["id"] == "ATLAS-TEST-001"
    assert payload["scenarios"][0]["category"] == "Digital resilience"
    assert json.loads(payload["scenarios"][0]["external_drivers"]) == [
        "Technology",
        "Institutional change",
    ]
    assert json.loads(payload["scenarios"][0]["discussion_prompts"]) == [
        "Which control fails first?"
    ]
    assert payload["risks"][0]["signal_maturity"] == "accelerating"
    assert "signal_maturity" not in payload["risks"][1]


def test_about_page_explains_foresight_and_scout_governance():
    about = yaml.safe_load(ABOUT.read_text())
    section = next(
        item
        for item in about["sections"]
        if item["heading"] == "Foresight Radar and Briefs"
    )
    text = " ".join(section["paragraphs"] + section["bullets"])

    assert "external drivers" in text.lower()
    assert "weak to emerging" in text.lower()
    assert "strengthening and weakening signposts" in text.lower()
    assert "atlas brief" in text.lower()
    assert "scouts may propose packets, but cannot approve evidence" in text.lower()
