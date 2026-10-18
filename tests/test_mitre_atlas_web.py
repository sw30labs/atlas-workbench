"""End-to-end tests for my display-only MITRE ATLAS dashboard overlay."""

from __future__ import annotations

import http.client
import json
import sqlite3
import threading
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from contingency_atlas.config import Settings
from contingency_atlas.db import get_scenario, update_scenario_fields, upsert_scenario
from contingency_atlas.frameworks import migrate_frameworks
from contingency_atlas.frameworks.mappings import list_mapping_proposals
from contingency_atlas.schema import migrate, migrate_evidence_events
from contingency_atlas.web import server
from contingency_atlas.web.server import DashboardHandler


def _add_object(
    conn: sqlite3.Connection,
    object_id: str,
    object_type: str,
    name: str,
    *,
    maturity: str | None = None,
    platforms: list[str] | None = None,
) -> None:
    """I add one normalized fixture object without retesting YAML parsing."""
    conn.execute(
        """
        INSERT INTO framework_objects VALUES
            ('mitre-atlas', '2026.07', ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            object_id,
            object_type,
            name,
            f"Description of {name}.",
            maturity,
            json.dumps(platforms or []),
            f"hash-{object_id}",
            json.dumps({"id": object_id, "name": name}),
        ),
    )


def _seed_catalog(db_path: Path, *, active: bool = True) -> None:
    """I seed a tiny pinned catalog so these tests stay offline and focused."""
    migrate_frameworks(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO framework_releases (
                framework, content_version, format_version, release_date,
                source_url, source_sha256, imported_at, is_active, is_sealed
            ) VALUES
                ('mitre-atlas', '2026.07', '6.0.0', '2026-07-31',
                 'https://example.test/ATLAS-2026.07.yaml', ?,
                 '2026-08-12T00:00:00+00:00', 0, 0)
            """,
            ("a" * 64,),
        )
        _add_object(conn, "AML.TA0001", "tactic", "AI Attack Staging")
        _add_object(
            conn,
            "AML.T0051.000",
            "technique",
            "Direct Prompt Injection",
            maturity="Realized",
            platforms=["Agentic AI"],
        )
        _add_object(
            conn,
            "AML.M0030",
            "mitigation",
            "Restrict Tool Invocation",
        )
        _add_object(
            conn,
            "AML.CS0001",
            "case-study",
            "Synthetic Prompt Injection Exercise",
        )
        relationships = (
            ("r-achieves", "AML.T0051.000", "achieves", "AML.TA0001"),
            ("r-mitigates", "AML.M0030", "mitigates", "AML.T0051.000"),
            ("r-employs", "AML.CS0001", "employs", "AML.T0051.000"),
        )
        for key, source_id, relationship_type, target_id in relationships:
            conn.execute(
                """
                INSERT INTO framework_relationships VALUES
                    ('mitre-atlas', '2026.07', ?, ?, ?, ?, '{}')
                """,
                (key, source_id, relationship_type, target_id),
            )
        conn.execute(
            """
            UPDATE framework_releases SET is_sealed = 1
            WHERE framework = 'mitre-atlas' AND content_version = '2026.07'
            """
        )
        if active:
            conn.execute(
                """
                UPDATE framework_releases SET is_active = 1
                WHERE framework = 'mitre-atlas'
                  AND content_version = '2026.07'
                """
            )


@pytest.fixture
def atlas_web_settings(tmp_path, monkeypatch):
    """I install only the stores the local dashboard reads in this workflow."""
    scenario_db = tmp_path / "atlas.db"
    evidence_db = tmp_path / "event_evidence.db"
    risks_path = tmp_path / "emerging_risks.json"
    migrate(str(scenario_db))
    migrate_evidence_events(str(evidence_db))
    risks_path.write_text("[]")
    upsert_scenario(
        str(scenario_db),
        {
            "scenario_id": "SCN-AI-001",
            "title": "Agent follows a poisoned retrieval instruction",
            "description": "A retrieved instruction redirects an agent tool.",
            "core_risks": json.dumps(["Model risk", "Data integrity"]),
            "transmission_mechanism": "Untrusted context invokes a tool.",
            "affected_capabilities": json.dumps(["Payments", "Data access"]),
            "urgency_score": 0.42,
            "status": "active",
        },
    )
    _seed_catalog(scenario_db)
    settings = Settings(
        scenario_db_path=scenario_db,
        evidence_db_path=evidence_db,
        emerging_risks_path=risks_path,
    )
    monkeypatch.setattr(server, "get_settings", lambda: settings)
    return settings


@contextmanager
def _dashboard():
    """I serve the real handler on an ephemeral loopback port."""
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), DashboardHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd.server_address[1]
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def _request(port: int, method: str, path: str, body=None, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        payload = json.dumps(body).encode() if body is not None else None
        base = {"Content-Type": "application/json"} if payload is not None else {}
        connection.request(
            method,
            path,
            body=payload,
            headers={**base, **(headers or {})},
        )
        response = connection.getresponse()
        return response.status, json.loads(response.read() or b"{}")
    finally:
        connection.close()


def _same_origin(port: int) -> dict[str, str]:
    return {"Origin": f"http://127.0.0.1:{port}"}


def _propose(port: int) -> tuple[int, dict]:
    return _request(
        port,
        "POST",
        "/api/frameworks/mitre-atlas/proposals",
        {
            "scenario_id": "SCN-AI-001",
            "technique_id": "AML.T0051.000",
            "content_version": "2026.07",
            "rationale": "The scenario explicitly contains an injected instruction.",
        },
        _same_origin(port),
    )


def _approve(port: int, proposal_id: str) -> tuple[int, dict]:
    return _request(
        port,
        "POST",
        "/api/review/decide",
        {
            "kind": "mitre_atlas",
            "record_id": proposal_id,
            "action": "approve",
            "reviewer": "alice",
            "reason": "The pinned technique matches the described mechanism.",
        },
        _same_origin(port),
    )


def test_status_and_bounded_technique_search(atlas_web_settings):
    with _dashboard() as port:
        status_code, status = _request(
            port, "GET", "/api/frameworks/mitre-atlas/status"
        )
        search_code, search = _request(
            port,
            "GET",
            "/api/frameworks/mitre-atlas/techniques?q=prompt"
            "&platform=Agentic%20AI&maturity=Realized&limit=10",
        )
        invalid_code, invalid = _request(
            port, "GET", "/api/frameworks/mitre-atlas/techniques?limit=51"
        )

    assert status_code == 200
    assert status["active_release"] == "2026.07"
    assert search_code == 200
    assert search["content_version"] == "2026.07"
    assert [row["technique_id"] for row in search["techniques"]] == [
        "AML.T0051.000"
    ]
    assert invalid_code == 400
    assert "between 1 and 50" in invalid["error"]


def test_propose_review_and_display_never_change_urgency(atlas_web_settings):
    before = get_scenario(str(atlas_web_settings.scenario_db_path), "SCN-AI-001")
    assert before is not None

    with _dashboard() as port:
        create_code, created = _propose(port)
        proposal_id = created["proposal_id"]
        queue_code, queue = _request(port, "GET", "/api/review/pending")
        pending_data_code, pending_payload = _request(port, "GET", "/api/data")
        missing_reviewer_code, missing_reviewer = _request(
            port,
            "POST",
            "/api/review/decide",
            {
                "kind": "mitre_atlas",
                "record_id": proposal_id,
                "action": "approve",
                "reason": "A decision without a reviewer must fail closed.",
            },
            _same_origin(port),
        )
        approve_code, approved = _approve(port, proposal_id)
        data_code, payload = _request(port, "GET", "/api/data")

    assert create_code == 201
    assert created["proposal"]["status"] == "pending"
    assert queue_code == 200
    pending = queue["mitre_atlas_proposals"]["pending"]
    assert pending[0]["record_id"] == proposal_id
    assert pending[0]["record"]["technique_name"] == "Direct Prompt Injection"
    assert pending[0]["report"]["approvable"] is True
    assert pending_data_code == 200
    pending_scenario = next(
        row for row in pending_payload["scenarios"] if row["id"] == "SCN-AI-001"
    )
    assert pending_scenario["mitre_atlas"]["mappings"] == []
    assert missing_reviewer_code == 400
    assert "reviewer" in missing_reviewer["error"]
    assert approve_code == 200
    assert approved["record"]["decision"] == "approved"
    assert data_code == 200
    scenario = next(row for row in payload["scenarios"] if row["id"] == "SCN-AI-001")
    mappings = scenario["mitre_atlas"]["mappings"]
    assert len(mappings) == 1
    assert mappings[0]["state"] == "current"
    assert mappings[0]["name"] == "Direct Prompt Injection"
    assert mappings[0]["tactics"][0]["name"] == "AI Attack Staging"
    assert mappings[0]["mitigations"][0]["name"] == "Restrict Tool Invocation"
    assert mappings[0]["related_case_studies"][0]["case_study_id"] == "AML.CS0001"
    assert scenario["urgency_score"] == before["urgency_score"] == 0.42

    rows = list_mapping_proposals(atlas_web_settings.scenario_db_path)
    assert [row["status"] for row in rows] == ["approved"]
    after = get_scenario(str(atlas_web_settings.scenario_db_path), "SCN-AI-001")
    assert after is not None and after["urgency_score"] == before["urgency_score"]


def test_approved_mapping_is_visible_but_explicitly_stale_after_edit(
    atlas_web_settings,
):
    with _dashboard() as port:
        create_code, created = _propose(port)
        assert create_code == 201
        approve_code, _ = _approve(port, created["proposal_id"])
        assert approve_code == 200

        update_scenario_fields(
            str(atlas_web_settings.scenario_db_path),
            "SCN-AI-001",
            {"description": "The scenario meaning changed after review."},
        )
        data_code, payload = _request(port, "GET", "/api/data")

    assert data_code == 200
    scenario = next(row for row in payload["scenarios"] if row["id"] == "SCN-AI-001")
    assert scenario["mitre_atlas"]["mappings"][0]["state"] == "stale"


def test_stale_pending_mapping_cannot_be_approved_by_single_or_batch_review(
    atlas_web_settings,
):
    with _dashboard() as port:
        create_code, created = _propose(port)
        assert create_code == 201
        proposal_id = created["proposal_id"]
        update_scenario_fields(
            str(atlas_web_settings.scenario_db_path),
            "SCN-AI-001",
            {"description": "The scenario changed before review."},
        )

        queue_code, queue = _request(port, "GET", "/api/review/pending")
        approve_code, approve_error = _approve(port, proposal_id)
        batch_code, batch = _request(
            port,
            "POST",
            "/api/review/decide-batch",
            {
                "action": "approve",
                "reviewer": "alice",
                "note": "This stale proposal must fail in my batch too.",
                "items": [
                    {"kind": "mitre_atlas", "record_id": proposal_id}
                ],
            },
            _same_origin(port),
        )
        reject_code, rejected = _request(
            port,
            "POST",
            "/api/review/decide",
            {
                "kind": "mitre_atlas",
                "record_id": proposal_id,
                "action": "reject",
                "reviewer": "alice",
                "reason": "I reject the stale snapshot and will remap it.",
            },
            _same_origin(port),
        )

    assert queue_code == 200
    pending = queue["mitre_atlas_proposals"]["pending"][0]
    assert pending["record"]["scenario_state"] == "stale"
    assert pending["report"]["approvable"] is False
    assert approve_code == 400
    assert "changed after mapping proposal" in approve_error["error"]
    assert batch_code == 207
    assert batch["failed"] == 1
    assert "changed after mapping proposal" in batch["results"][0]["error"]
    assert reject_code == 200
    assert rejected["record"]["decision"] == "rejected"
    rows = list_mapping_proposals(atlas_web_settings.scenario_db_path)
    assert [row["status"] for row in rows] == ["rejected"]


def test_cross_origin_proposal_is_rejected_without_a_write(atlas_web_settings):
    with _dashboard() as port:
        status_code, payload = _request(
            port,
            "POST",
            "/api/frameworks/mitre-atlas/proposals",
            {
                "scenario_id": "SCN-AI-001",
                "technique_id": "AML.T0051.000",
                "rationale": "This request must not reach proposal intake.",
            },
            {"Origin": "https://attacker.invalid"},
        )

    assert status_code == 403
    assert "restricted" in payload["error"]
    assert list_mapping_proposals(atlas_web_settings.scenario_db_path) == []


def test_dashboard_and_search_degrade_cleanly_without_an_imported_catalog(
    tmp_path,
    monkeypatch,
):
    """I keep the core product usable when the optional catalog is absent."""
    scenario_db = tmp_path / "atlas.db"
    evidence_db = tmp_path / "event_evidence.db"
    risks_path = tmp_path / "emerging_risks.json"
    migrate(str(scenario_db))
    migrate_evidence_events(str(evidence_db))
    risks_path.write_text("[]")
    upsert_scenario(
        str(scenario_db),
        {
            "scenario_id": "SCN-CORE-001",
            "title": "Core scenario without an external framework",
            "description": "The optional overlay has not been installed.",
            "urgency_score": 0.31,
            "status": "active",
        },
    )
    settings = Settings(
        scenario_db_path=scenario_db,
        evidence_db_path=evidence_db,
        emerging_risks_path=risks_path,
    )
    monkeypatch.setattr(server, "get_settings", lambda: settings)

    with _dashboard() as port:
        data_code, payload = _request(port, "GET", "/api/data")
        search_code, search = _request(
            port, "GET", "/api/frameworks/mitre-atlas/techniques?q=prompt"
        )
        propose_code, rejected = _request(
            port,
            "POST",
            "/api/frameworks/mitre-atlas/proposals",
            {
                "scenario_id": "SCN-CORE-001",
                "technique_id": "AML.T0051",
                "rationale": "This cannot resolve without an imported release.",
            },
            _same_origin(port),
        )

    assert data_code == 200
    assert payload["scenarios"][0]["urgency_score"] == 0.31
    assert payload["scenarios"][0]["mitre_atlas"]["mappings"] == []
    assert payload["meta"]["mitre_atlas"]["installed"] is False
    assert search_code == 200
    assert search == {"content_version": None, "techniques": [], "count": 0}
    assert propose_code == 409
    assert "no active catalog release" in rejected["error"]


def test_installed_but_inactive_catalog_keeps_the_core_dashboard_usable(
    tmp_path,
    monkeypatch,
):
    """I distinguish an inactive catalog from an absent or active catalog."""
    scenario_db = tmp_path / "atlas.db"
    evidence_db = tmp_path / "event_evidence.db"
    risks_path = tmp_path / "emerging_risks.json"
    migrate(str(scenario_db))
    migrate_evidence_events(str(evidence_db))
    risks_path.write_text("[]")
    upsert_scenario(
        str(scenario_db),
        {
            "scenario_id": "SCN-INACTIVE-001",
            "title": "Scenario with an inactive external catalog",
            "description": "The catalog is installed but not selected.",
            "urgency_score": 0.27,
            "status": "active",
        },
    )
    _seed_catalog(scenario_db, active=False)
    settings = Settings(
        scenario_db_path=scenario_db,
        evidence_db_path=evidence_db,
        emerging_risks_path=risks_path,
    )
    monkeypatch.setattr(server, "get_settings", lambda: settings)

    with _dashboard() as port:
        status_code, status = _request(
            port, "GET", "/api/frameworks/mitre-atlas/status"
        )
        data_code, payload = _request(port, "GET", "/api/data")
        search_code, search = _request(
            port, "GET", "/api/frameworks/mitre-atlas/techniques?q=prompt"
        )
        propose_code, rejected = _request(
            port,
            "POST",
            "/api/frameworks/mitre-atlas/proposals",
            {
                "scenario_id": "SCN-INACTIVE-001",
                "technique_id": "AML.T0051.000",
                "rationale": "I require an active release for implicit intake.",
            },
            _same_origin(port),
        )

    assert status_code == 200
    assert status["installed"] is True
    assert status["available"] is False
    assert status["active_release"] is None
    assert data_code == 200
    assert payload["scenarios"][0]["urgency_score"] == 0.27
    assert search_code == 200
    assert search["techniques"] == []
    assert propose_code == 409
    assert "no active catalog release" in rejected["error"]


def test_dashboard_asset_names_the_governance_and_non_scoring_boundaries():
    html = (
        Path(__file__).resolve().parents[1]
        / "contingency_atlas/web/static/index.html"
    ).read_text()
    assert "/api/frameworks/mitre-atlas/techniques" in html
    assert "/api/frameworks/mitre-atlas/proposals" in html
    assert "MITRE ATLAS™ applicability mapping" in html
    assert "PROPOSE A MAPPING" in html
    assert "SCENARIO CHANGED — RE-REVIEW" in html
    assert "CURRENT REVIEWED MAPPING" in html
    assert "never changes this scenario's score" in html
    assert "esc(techniqueName)" in html
    assert "esc(mapping.rationale)" in html
