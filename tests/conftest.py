"""Pytest configuration and synthetic fixtures."""

import os

# Unit tests run without the ontology guard: it gates the production write
# seams (ontology/guard.py), and its subprocess cost and store snapshot are
# meaningless against synthetic tmp workspaces.
os.environ.setdefault("ATLAS_GUARD", "off")

import pytest


@pytest.fixture
def tmp_workspace(tmp_path):
    """Create an isolated workspace populated only with synthetic data."""
    import json
    from datetime import date, timedelta

    from workbench_atlas.db import add_evidence_event
    from workbench_atlas.schema import migrate, migrate_evidence_events

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    scenario_db = workspace / "atlas.db"
    evidence_db = workspace / "event_evidence.db"
    migrate(str(scenario_db))
    migrate_evidence_events(str(evidence_db))

    start = date(2024, 1, 1)
    for index in range(12):
        add_evidence_event(str(evidence_db), {
            "event_id": f"SYN-EVT-{index:03d}",
            "headline": f"Synthetic operational event {index}",
            "organization": "Northstar Bank (fictional)",
            "country": "Exampleland",
            "region": "North America",
            "loss_amount_usd": float((index + 1) * 1_000_000),
            "source_date": (start + timedelta(days=index * 20)).isoformat(),
            "risk_category": (
                "Digital resilience"
                if index % 2 == 0
                else "Processing integrity"
            ),
            "event_driver_class": "Technology disruption",
            "primary_cause_class": "Process design",
            "source_url": f"https://example.com/synthetic/{index}",
            "source_publisher": "Example Public Register",
            "source_title": f"Synthetic notice {index}",
            "retrieved_at": "2026-01-01T00:00:00+00:00",
            "source_type": "synthetic",
            "record_type": "event",
            "provenance_class": "synthetic",
            "source_locator": None,
            "independently_authored_summary": "A fictional event for automated tests.",
            "research_inference": "No factual inference; synthetic test record.",
            "rights_review": "not_applicable_synthetic",
            "content_fingerprint": None,
            "review_status": "approved_synthetic",
            "reviewed_at": "2026-01-01",
            "taxonomy_version": "atlas-1.0",
            "provenance_note": "Synthetic fixture; not a real event.",
        })

    (workspace / "emerging_risks.json").write_text(json.dumps([
        {
            "issue": "Synthetic cloud concentration",
            "description": "A fictional shared-service dependency for tests.",
            "status": "active",
            "trend": "stable",
            "evidence_refs": ["SYN-EVT-000"],
            "source_type": "synthetic",
            "provenance_note": "Synthetic fixture; not a real risk signal.",
            "review_status": "approved_synthetic",
        }
    ]))
    (workspace / "events.json").write_text("[]")

    return workspace


@pytest.fixture
def settings(tmp_workspace):
    """Settings pointing to the temp workspace."""
    from workbench_atlas.config import Settings
    return Settings(
        scenario_db_path=tmp_workspace / "atlas.db",
        evidence_db_path=tmp_workspace / "event_evidence.db",
        emerging_risks_path=tmp_workspace / "emerging_risks.json",
        scenario_export_path=tmp_workspace / "scenario_export.json",
        # A checkout config may enable the operator's private workspace.
        # Fixtures stay on an absent temp store unless a test opts in.
        context_enabled=False,
        context_db_path=tmp_workspace / "organization-context.db",
    )


@pytest.fixture
def omlox_client(settings):
    """OMLX client (may not be reachable in CI)."""
    from workbench_atlas.llm import OMLXClient
    client = OMLXClient(settings)
    yield client
    client.close()


@pytest.fixture
def mock_omlox_client():
    """Mock OMLX client that doesn't make real calls."""
    from workbench_atlas.llm import OMLXClient
    from workbench_atlas.config import Settings

    class MockClient(OMLXClient):
        def __init__(self):
            self.settings = Settings()
            self._base_url = "http://mock"
            self._api_key = "mock"
            self._timeout = 1.0
            self._client = None

        def chat(self, messages, **kwargs):
            return '{"score": 0.82, "criteria": {"clarity": 0.9, "foresight_value": 0.8, "evidence_grounding": 0.75, "actionability": 0.8, "no_hallucination": 0.85}, "feedback": "Good scenario with clear risk convergence.", "passed": true}'

        def chat_json(self, messages, **kwargs):
            import json
            return json.loads(self.chat(messages, **kwargs))

        def is_available(self):
            return True

        def list_models(self):
            return ["mock-model"]

    return MockClient()


@pytest.fixture
def empty_db(tmp_path):
    """Create an empty atlas.db with the full schema."""
    from workbench_atlas.schema import migrate
    db_path = str(tmp_path / "test_atlas.db")
    migrate(db_path)
    return db_path
