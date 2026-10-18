"""Unified research-intake and approved-coverage web contracts."""

import http.client
import json
import threading
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path

from contingency_atlas.db import list_evidence_events
from contingency_atlas.phases.phase2_risks import load_emerging_risks
from contingency_atlas.web import runner
from contingency_atlas.web.intake_flow import (
    IntakeDraftStore,
    prepare_research_material,
    submit_draft_to_review,
)
from contingency_atlas.web.server import DashboardHandler


@contextmanager
def _dashboard_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), DashboardHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _request(port, method, path, payload=None):
    body = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"} if body is not None else {}
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        data = json.loads(response.read() or b"{}")
        return response.status, data
    finally:
        connection.close()


class _IntakeClient:
    """Offline scripted client covering extraction, QA, and normalization."""

    def __init__(self, settings):
        self.settings = settings

    def chat_json(self, messages, **_kwargs):
        system = str(messages[0].get("content") or "")
        if "prepare DRAFT research intake" in system:
            return {
                "candidates": [
                    {
                        "kind": "evidence",
                        "title": "Public cloud service interruption",
                        "text": "A public notice reported a cloud service interruption.",
                        "source_urls": ["https://public.example/cloud-notice"],
                        "input_unit_ids": ["unit-001"],
                        "classification_reason": "Observed event in a cited notice.",
                        "record": {
                            "organization": "Example Cloud Provider",
                            "country": "Exampleland",
                            "region": "North America",
                            "source_date": "2026-08-29",
                            "risk_category": "Digital resilience",
                            "event_driver_class": "Technology disruption",
                            "primary_cause_class": "Process design",
                            "source_publisher": "Example Public Register",
                            "source_title": "Cloud service notice",
                            "source_type": "public_reporting",
                        },
                    },
                    {
                        "kind": "risk",
                        "title": "Cloud control-plane concentration",
                        "text": "The interruption suggests a concentration hypothesis for review.",
                        "source_urls": ["https://public.example/cloud-notice"],
                        "input_unit_ids": ["unit-001"],
                        "classification_reason": "Forward-looking hypothesis linked to the event.",
                    },
                ],
                "ignored": [],
            }
        if "semantic QA judge" in system:
            return {"verdict": "pass", "issues": [], "note": "faithful"}
        # Phase 1's normalizer sends one user message containing EXTRACT_PROMPT.
        content = str(messages[0].get("content") or "")
        if "extracting operational-risk events" in content:
            return {
                "headline": "Public cloud service interruption",
                "organization": "Example Cloud Provider",
                "country": "Exampleland",
                "region": "North America",
                "loss_amount_usd": None,
                "source_date": "2026-08-29",
                "risk_category": "Digital resilience",
                "event_driver_class": "Technology disruption",
                "primary_cause_class": "Process design",
                "source_publisher": "Example Public Register",
                "source_title": "Cloud service notice",
                "source_type": "public_reporting",
                "independently_authored_summary": (
                    "A public notice described an interruption affecting cloud service."
                ),
                "provenance_note": "Prepared from the cited public notice.",
            }
        raise AssertionError(f"unexpected prompt: {system[:80]!r}")


def test_scenario_count_allows_explicit_zero():
    assert runner._num_scenarios({"num_scenarios": 0}) == 0
    assert runner._num_scenarios({"num_scenarios": "0"}) == 0


def test_scenario_count_defaults_and_clamps():
    assert runner._num_scenarios({}) == 1
    assert runner._num_scenarios({"num_scenarios": -4}) == 0
    assert runner._num_scenarios({"num_scenarios": 99}) == runner.MAX_SCENARIOS


def test_zero_scenario_run_is_an_offline_noop(monkeypatch, settings):
    monkeypatch.setattr(runner, "get_settings", lambda: settings)

    def unexpected_reachability_check(*_args, **_kwargs):
        raise AssertionError("a zero-scenario run must not contact the model")

    monkeypatch.setattr(runner, "omlx_reachable", unexpected_reachability_check)
    result = runner.execute_phase("phase3", {"num_scenarios": 0})

    assert result["generated"] == 0
    assert result["inserted"] == 0
    assert result["errors"] == []
    assert "unchanged" in result["note"]


def test_prepare_then_submit_queues_linked_pending_records(settings):
    store = IntakeDraftStore(max_drafts=2, ttl_seconds=60)
    payload = prepare_research_material(
        "Public notice and risk discussion https://public.example/cloud-notice",
        settings=settings,
        client=_IntakeClient(settings),
        store=store,
    )
    assert payload["summary"] == {
        "ready": 2,
        "needs_attention": 0,
        "already_known": 0,
    }, payload["items"]
    selected = [item["id"] for item in payload["items"] if item["selected"]]
    result = submit_draft_to_review(
        payload["draft_id"], selected, settings=settings, store=store
    )

    assert result["submitted"] == {"evidence": 1, "risks": 1}
    event = list_evidence_events(str(settings.evidence_db_path))[0]
    risk = load_emerging_risks(settings.emerging_risks_path)[-1]
    assert event["review_status"] == "pending"
    assert risk["review_status"] == "pending"
    assert risk["evidence_refs"] == [event["event_id"]]


def test_risk_stays_held_when_supporting_prepared_evidence_is_not_selected(settings):
    store = IntakeDraftStore(max_drafts=2, ttl_seconds=60)
    payload = prepare_research_material(
        "Public notice and risk discussion https://public.example/cloud-notice",
        settings=settings,
        client=_IntakeClient(settings),
        store=store,
    )
    risk_id = next(item["id"] for item in payload["items"] if item["kind"] == "risk")

    result = submit_draft_to_review(
        payload["draft_id"], [risk_id], settings=settings, store=store
    )

    assert result["queued"] == 0
    assert result["submitted"] == {"evidence": 0, "risks": 0}
    assert "supporting evidence" in result["held"][0]["reason"]
    assert len(load_emerging_risks(settings.emerging_risks_path)) == 1


def test_prepare_and_submit_http_routes_dispatch(monkeypatch, settings):
    from contingency_atlas.web import intake_flow, server

    monkeypatch.setattr(server, "get_settings", lambda: settings)
    monkeypatch.setattr(runner, "omlx_reachable", lambda *_args, **_kwargs: True)
    captured = {}

    monkeypatch.setattr(
        intake_flow,
        "prepare_research_material",
        lambda material, **kwargs: captured.update(
            {"material": material, **kwargs}
        ) or {
            "draft_id": "draft-http",
            "items": [],
            "material_seen": material,
        },
    )
    monkeypatch.setattr(
        intake_flow,
        "submit_draft_to_review",
        lambda draft_id, selected_ids, **_kwargs: {
            "draft_id": draft_id,
            "queued": len(selected_ids),
        },
    )

    with _dashboard_server() as port:
        sources_status, sources = _request(port, "GET", "/api/intake/sources")
        prepare_status, prepared = _request(
            port,
            "POST",
            "/api/intake/prepare",
            {
                "research_material": "one mixed research packet",
                "include_singularity_atlas": True,
            },
        )
        submit_status, submitted = _request(
            port,
            "POST",
            "/api/intake/submit",
            {"draft_id": "draft-http", "selected_ids": ["candidate-1"]},
        )

    assert sources_status == 200
    assert sources["singularity_atlas"]["available"] in {True, False}
    assert prepare_status == 201
    assert prepared["material_seen"] == "one mixed research packet"
    assert captured.get("include_singularity_atlas") is True
    assert submit_status == 201
    assert submitted == {"draft_id": "draft-http", "queued": 1}


def test_recommendation_http_route_is_approved_only(monkeypatch, settings):
    from contingency_atlas.web import server

    monkeypatch.setattr(server, "get_settings", lambda: settings)
    with _dashboard_server() as port:
        status, payload = _request(
            port, "GET", "/api/scenarios/recommendation"
        )

    assert status == 200
    assert payload["based_on"] == "approved_corpus"
    assert payload["recommended_count"] == 1
    assert payload["basis"]["ignored_unapproved_evidence"] == 0


def test_dashboard_exposes_staged_intake_and_separate_approved_planner():
    html = (
        Path(__file__).parents[1]
        / "contingency_atlas/web/static/index.html"
    ).read_text()

    assert '{ k: "research_material", label: "Research material", required: true' in html
    assert 'fetch("/api/intake/prepare"' in html
    assert 'fetch("/api/intake/submit"' in html
    assert 'id="plan-scenarios-btn"' in html
    assert 'fetch("/api/scenarios/recommendation"' in html
    assert 'id="scenario-plan-count" type="number" min="0" max="10"' in html
    assert "Queued research cannot influence analysis until it is approved" in html
    assert 'if (activeRunPhase === "full") renderIntakeStage();' in html
    assert "the job will continue" not in html
    assert "include_singularity_atlas" in html
    assert 'fetch("/api/intake/sources"' in html
    assert "Include Singularity Atlas stories" in html
