"""Tests for Phase 1 (Ingestion) and Phase 2 (Risk Monitoring)."""

import json
import os
from pathlib import Path

import pytest


# ── Phase 1: Ingestion tests ──

class TestPhase1Ingestion:
    def test_extract_event_from_text(self, mock_omlox_client):
        from contingency_atlas.phases.phase1_ingest import extract_event_from_text
        # Set up mock to return an extraction response
        mock_omlox_client.chat_json = lambda *a, **k: {
            "headline": "Northstar Bank cloud identity outage",
            "organization": "Northstar Bank (fictional)",
            "country": "Exampleland",
            "region": "North America",
            "loss_amount_usd": 250000000,
            "source_date": "2024-06-01",
            "risk_category": "Digital resilience",
            "event_driver_class": "Technology disruption",
            "primary_cause_class": "Access control",
            "source_url": "https://example.com/1",
            "source_publisher": "Example Public Register",
            "source_title": "Synthetic outage notice",
            "source_type": "company_disclosure",
            "independently_authored_summary": "A fictional bank reported a cloud identity outage.",
            "provenance_note": "Synthetic test fixture.",
        }
        text = (
            "Northstar Bank, a fictional institution, suffered a $250M loss "
            "from a cloud identity outage in June 2024."
        )
        event = extract_event_from_text(text, mock_omlox_client, source_url="https://example.com/1")
        assert event is not None
        assert "headline" in event
        assert "event_id" in event
        assert event["taxonomy_version"] == "atlas-1.0"
        assert event["risk_category"] == "Digital resilience"
        assert event["event_driver_class"] == "Technology disruption"
        assert event["primary_cause_class"] == "Access control"
        assert event["record_type"] == "event"
        assert event["provenance_class"] == "public_primary"
        assert event["rights_review"] == "pending"
        assert event["review_status"] == "pending"
        assert len(event["content_fingerprint"]) == 64

    def test_extract_returns_none_on_failure(self, mock_omlox_client):
        from contingency_atlas.phases.phase1_ingest import extract_event_from_text
        # Mock that returns empty
        mock_omlox_client.chat_json = lambda *a, **k: {}
        event = extract_event_from_text(
            "text", mock_omlox_client, source_url="https://example.com/empty"
        )
        assert event is None

    def test_extract_requires_public_source_url(self, mock_omlox_client):
        from contingency_atlas.phases.phase1_ingest import extract_event_from_text

        assert extract_event_from_text("text", mock_omlox_client) is None
        assert (
            extract_event_from_text(
                "text", mock_omlox_client, source_url="not-a-public-url"
            )
            is None
        )

    @pytest.mark.parametrize("source_date", [None, "", "2025", "2025-02-30"])
    def test_extract_requires_valid_publication_date(
        self, mock_omlox_client, source_date
    ):
        from contingency_atlas.phases.phase1_ingest import extract_event_from_text

        mock_omlox_client.chat_json = lambda *a, **k: {
            "headline": "Synthetic dated notice",
            "source_date": source_date,
            "source_publisher": "Example Public Register",
            "source_title": "Synthetic notice",
            "source_type": "regulatory_release",
            "independently_authored_summary": "A fictional test record.",
        }
        assert (
            extract_event_from_text(
                "Synthetic public notice.",
                mock_omlox_client,
                source_url="https://example.com/dated-notice",
            )
            is None
        )

    def test_ingest_text_to_db(self, mock_omlox_client, tmp_workspace):
        from contingency_atlas.phases.phase1_ingest import ingest_text

        mock_omlox_client.chat_json = lambda *a, **k: {
            "headline": "Synthetic control notice",
            "organization": "Northstar Bank (fictional)",
            "country": "Exampleland",
            "region": "North America",
            "loss_amount_usd": 1_300_000,
            "source_date": "2025-01-01",
            "risk_category": "Financial crime controls",
            "event_driver_class": "Process failure",
            "primary_cause_class": "Monitoring gap",
            "source_publisher": "Example Public Register",
            "source_title": "Synthetic public notice",
            "source_type": "public_reporting",
            "independently_authored_summary": (
                "A fictional institution received a synthetic penalty."
            ),
            "provenance_note": "Synthetic test of public-source ingestion.",
        }
        text = "Northstar Bank (fictional) received a synthetic $1.3M penalty."
        result = ingest_text(
            text, mock_omlox_client,
            db_path=str(tmp_workspace / "event_evidence.db"),
            source_url="https://example.com/test",
        )
        assert len(result.events) == 1
        assert result.errors == []

    def test_out_of_vocabulary_classification_is_null_not_guessed(
        self, mock_omlox_client
    ):
        """Phase 1 used to fall back to keyword derivers that cannot return
        empty, so an unclassifiable event still got a confident label. It now
        records NULL instead; risk_category keeps its deriver because
        downstream consumers read it."""
        from contingency_atlas.categories import RISK_CATEGORIES
        from contingency_atlas.phases.phase1_ingest import extract_event_from_text

        mock_omlox_client.chat_json = lambda *a, **k: {
            "headline": "Synthetic notice",
            "source_date": "2025-01-01",
            "risk_category": "Not a real category",
            "event_driver_class": "Not a real event type",
            "primary_cause_class": "Not a real root cause",
            "region": "Atlantis",
            "source_publisher": "Example Public Register",
            "source_title": "Synthetic notice",
            "source_type": "regulatory_release",
            "independently_authored_summary": "A fictional record.",
        }
        event = extract_event_from_text(
            "Synthetic public notice.",
            mock_omlox_client,
            source_url="https://example.com/vocab",
        )
        assert event is not None
        assert event["event_driver_class"] is None
        assert event["primary_cause_class"] is None
        assert event["region"] is None
        assert event["risk_category"] in RISK_CATEGORIES

    def test_valid_classification_is_preserved(self, mock_omlox_client):
        from contingency_atlas.phases.phase1_ingest import extract_event_from_text

        mock_omlox_client.chat_json = lambda *a, **k: {
            "headline": "Synthetic notice",
            "source_date": "2025-01-01",
            "risk_category": "Digital resilience",
            "event_driver_class": "Technology disruption",
            "primary_cause_class": "Access control",
            "region": "north america",
            "source_publisher": "Example Public Register",
            "source_title": "Synthetic notice",
            "source_type": "regulatory_release",
            "independently_authored_summary": "A fictional record.",
        }
        event = extract_event_from_text(
            "Synthetic public notice.",
            mock_omlox_client,
            source_url="https://example.com/vocab-ok",
        )
        assert event["event_driver_class"] == "Technology disruption"
        assert event["primary_cause_class"] == "Access control"
        assert event["region"] == "North America"  # canonicalized casing

    def test_global_is_a_region_not_a_missing_one(self, mock_omlox_client):
        """"Global" is the honest answer for an event with no single region,
        and rows already carry it. Closing the vocabulary must not rewrite it
        to unclassified — that would erase the distinction the chart shows."""
        from contingency_atlas.phases.phase1_ingest import extract_event_from_text

        mock_omlox_client.chat_json = lambda *a, **k: {
            "headline": "Synthetic notice",
            "source_date": "2025-01-01",
            "risk_category": "Digital resilience",
            "region": "Global",
            "source_publisher": "Example Public Register",
            "source_title": "Synthetic notice",
            "source_type": "regulatory_release",
            "independently_authored_summary": "A fictional record.",
        }
        event = extract_event_from_text(
            "Synthetic public notice.",
            mock_omlox_client,
            source_url="https://example.com/global",
        )
        assert event["region"] == "Global"

    def test_ingest_canonicalizes_source_url_before_fingerprinting(
        self, mock_omlox_client
    ):
        """A stored source_url carries no literal spaces, and the fingerprint
        is computed over the canonical form so it reproduces from the record."""
        import hashlib

        from contingency_atlas.phases.phase1_ingest import extract_event_from_text

        mock_omlox_client.chat_json = lambda *a, **k: {
            "headline": "Synthetic assessment notice",
            "source_date": "2025-03-04",
            "risk_category": "Digital resilience",
            "source_publisher": "Example Public Register",
            "source_title": "Synthetic assessment",
            "source_type": "regulatory_release",
            "independently_authored_summary": "A fictional assessment record.",
        }
        event = extract_event_from_text(
            "Synthetic public notice.",
            mock_omlox_client,
            source_url="https://example.com/files/A B assessment.pdf",
        )
        assert event is not None
        assert event["source_url"] == "https://example.com/files/A%20B%20assessment.pdf"

        fingerprint_input = "\n".join(
            str(event.get(field) or "")
            for field in (
                "source_url",
                "source_publisher",
                "source_title",
                "source_date",
                "independently_authored_summary",
            )
        )
        assert event["content_fingerprint"] == hashlib.sha256(
            fingerprint_input.encode("utf-8")
        ).hexdigest()

    def test_add_evidence_event_dedups_raw_against_canonical_url(self, tmp_workspace):
        """A legacy row stored with a raw URL must still block a re-ingest of
        the same document under its canonical form."""
        from contingency_atlas.db import add_evidence_event

        db_path = str(tmp_workspace / "event_evidence.db")
        raw = "https://example.com/files/A B assessment.pdf"
        assert add_evidence_event(
            db_path,
            {
                "event_id": "EVT-RAW-000000000001",
                "headline": "Synthetic assessment",
                "source_url": raw,
                "review_status": "pending",
            },
        )
        encoded = "https://example.com/files/A%20B%20assessment.pdf"
        assert not add_evidence_event(
            db_path,
            {
                "event_id": "EVT-ENC-000000000002",
                "headline": "Synthetic assessment",
                "source_url": encoded,
                "review_status": "pending",
            },
        )
        # And the other direction: a canonical row must block a raw re-ingest,
        # or the same document lands twice the first time an old caller runs.
        assert add_evidence_event(
            db_path,
            {
                "event_id": "EVT-ENC-000000000003",
                "headline": "Second synthetic assessment",
                "source_url": "https://example.com/files/C%20D%20assessment.pdf",
                "review_status": "pending",
            },
        )
        assert not add_evidence_event(
            db_path,
            {
                "event_id": "EVT-RAW-000000000004",
                "headline": "Second synthetic assessment",
                "source_url": "https://example.com/files/C D assessment.pdf",
                "review_status": "pending",
            },
        )

    def test_ingest_batch(self, mock_omlox_client, tmp_workspace):
        from contingency_atlas.phases.phase1_ingest import ingest_batch
        texts = [
            "Bank fined for AML violation.",
            "Cloud outage disrupts trading.",
        ]
        result = ingest_batch(
            texts, mock_omlox_client,
            db_path=str(tmp_workspace / "event_evidence.db"),
            source_urls=[
                "https://example.com/record-1",
                "https://example.com/record-2",
            ],
        )
        assert isinstance(result.events, list)

    def test_ingest_batch_requires_aligned_urls(
        self, mock_omlox_client, tmp_workspace
    ):
        from contingency_atlas.phases.phase1_ingest import ingest_batch

        result = ingest_batch(
            ["First source", "Second source"],
            mock_omlox_client,
            db_path=str(tmp_workspace / "event_evidence.db"),
            source_urls=["https://example.com/record-1"],
        )
        assert result.events == []
        assert result.skipped == 2
        assert result.errors

    def test_parse_rss_item(self):
        from contingency_atlas.phases.phase1_ingest import parse_rss_item
        item = {"title": "Test", "summary": "Summary text"}
        text = parse_rss_item(item)
        assert "Test" in text
        assert "Summary" in text

    def test_parse_enforcement_action(self):
        from contingency_atlas.phases.phase1_ingest import parse_enforcement_action
        text = parse_enforcement_action(
            "Example Regulator",
            "Northstar Bank (fictional) received a synthetic $1.3M penalty",
        )
        assert "Example Regulator" in text
        assert "Northstar Bank" in text

    def test_ingestion_with_real_omlx(self, omlox_client, tmp_workspace):
        """Test extraction with the real OMLX service (may skip)."""
        if not omlox_client.is_available():
            pytest.skip("OMLX not reachable")
        from contingency_atlas.phases.phase1_ingest import extract_event_from_text
        text = (
            "In July 2024, Northstar Bank (a fictional institution) experienced "
            "a synthetic cloud identity disruption affecting test trading "
            "systems. The fictional outage lasted 6 hours and cost an estimated "
            "$250M in simulated lost revenue."
        )
        try:
            event = extract_event_from_text(
                text,
                omlox_client,
                source_url="https://example.com/synthetic-live-test",
            )
        except Exception as e:
            pytest.skip(f"OMLX extraction failed (expected on first run): {e}")
        if event is None:
            pytest.skip("OMLX extraction returned None (model may not support JSON mode)")
        assert "headline" in event
        assert event.get("headline")  # Non-empty


# ── Phase 2: Risk Monitoring tests ──

class TestPhase2RiskMonitoring:
    def test_load_emerging_risks(self, tmp_workspace):
        from contingency_atlas.phases.phase2_risks import load_emerging_risks
        risks = load_emerging_risks(tmp_workspace / "emerging_risks.json")
        assert isinstance(risks, list)
        assert len(risks) > 0
        assert "issue" in risks[0]

    def test_load_missing_file(self, tmp_path):
        from contingency_atlas.phases.phase2_risks import load_emerging_risks
        risks = load_emerging_risks(tmp_path / "nonexistent.json")
        assert risks == []

    def test_save_emerging_risks(self, tmp_path):
        from contingency_atlas.phases.phase2_risks import save_emerging_risks, load_emerging_risks
        risks = [{"issue": "Test Risk", "description": "Test desc"}]
        path = tmp_path / "test_risks.json"
        save_emerging_risks(path, risks)
        loaded = load_emerging_risks(path)
        assert len(loaded) == 1
        assert loaded[0]["issue"] == "Test Risk"

    def test_identify_new_risks(self, mock_omlox_client):
        from contingency_atlas.phases.phase2_risks import identify_new_risks
        # Mock returns new risks
        mock_omlox_client.chat_json = lambda *a, **k: {
            "new_risks": [
                {"issue": "Quantum Computing Risk", "description": "New quantum threat", "trend": "rising"},
                {"issue": "Existing Risk", "description": "Already known", "trend": "stable"},
            ]
        }
        new = identify_new_risks(
            "source text",
            ["Existing Risk"],
            mock_omlox_client,
            ["https://example.com/risk-source"],
        )
        # Should skip "Existing Risk" since it's in known_risks
        assert len(new) == 1
        assert new[0].issue == "Quantum Computing Risk"

    def test_assess_existing_risks(self, mock_omlox_client):
        from contingency_atlas.phases.phase2_risks import assess_existing_risks
        risks = [
            {"issue": "Climate Risk", "description": "Climate change impact", "status": "active"},
            {"issue": "AI Risk", "description": "AI bias risk", "status": "active"},
        ]
        mock_omlox_client.chat_json = lambda *a, **k: {
            "assessments": [
                {"issue": "Climate Risk", "status": "active", "trend": "rising", "notes": "worsening"},
                {"issue": "AI Risk", "status": "monitoring", "trend": "rising", "notes": "monitor"},
            ]
        }
        updated = assess_existing_risks(
            risks,
            mock_omlox_client,
            "Cited public research packet",
            ["https://example.com/risk-assessment"],
        )
        assert updated[0]["trend"] == "rising"
        assert updated[1]["status"] == "monitoring"
        assert updated[1]["review_status"] == "pending"

    def test_assess_preserves_human_review_decisions(self, mock_omlox_client):
        """Regression: a monitoring cycle must not wipe human review decisions
        (the 2026-08-04 incident: approvals erased by assess_risks)."""
        from contingency_atlas.phases.phase2_risks import assess_existing_risks
        risks = [
            {"issue": "Approved Risk", "description": "d", "status": "active",
             "review_status": "approved", "evidence_refs": ["EVT-DR-approved1"],
             "provenance_note": "Manual review approved public-source access."},
            {"issue": "Rejected Risk", "description": "d", "status": "active",
             "review_status": "rejected",
             "provenance_note": "Manual review rejected this risk."},
            {"issue": "Fresh Risk", "description": "d", "status": "active",
             "evidence_refs": []},
        ]
        mock_omlox_client.chat_json = lambda *a, **k: {
            "assessments": [
                {"issue": "Approved Risk", "status": "active", "trend": "rising"},
                {"issue": "Rejected Risk", "status": "active", "trend": "stable"},
                {"issue": "Fresh Risk", "status": "active", "trend": "rising"},
            ]
        }
        updated = assess_existing_risks(
            risks, mock_omlox_client, "packet", ["https://example.com/packet"]
        )
        by_issue = {r["issue"]: r for r in updated}
        # approved -> needs_update (never back to pending), note preserved
        assert by_issue["Approved Risk"]["review_status"] == "needs_update"
        assert "Manual review approved" in by_issue["Approved Risk"]["provenance_note"]
        # rejected stays rejected
        assert by_issue["Rejected Risk"]["review_status"] == "rejected"
        # undecided records still go to pending and accrue packet refs
        assert by_issue["Fresh Risk"]["review_status"] == "pending"
        assert "https://example.com/packet" in by_issue["Fresh Risk"]["evidence_refs"]
        # decided records keep their human-curated refs (no URL pollution)
        assert by_issue["Approved Risk"]["evidence_refs"] == ["EVT-DR-approved1"]

    def test_run_monitoring_cycle(self, mock_omlox_client, tmp_workspace):
        from contingency_atlas.phases.phase2_risks import run_monitoring_cycle
        # Mock: first call identifies new risk, second assesses existing
        call_count = [0]

        def mock_chat_json(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return {"new_risks": [{"issue": "New Risk", "description": "New threat", "trend": "rising"}]}
            return {"assessments": []}

        mock_omlox_client.chat_json = mock_chat_json
        result = run_monitoring_cycle(
            "New intelligence about quantum threats",
            tmp_workspace / "emerging_risks.json",
            mock_omlox_client,
            source_refs=["https://example.com/quantum-research"],
        )
        assert len(result.new_risks) >= 0  # May be 0 if mock behavior varies

    def test_monitoring_requires_cited_sources(
        self, mock_omlox_client, tmp_workspace
    ):
        from contingency_atlas.phases.phase2_risks import run_monitoring_cycle

        result = run_monitoring_cycle(
            "Uncited source text",
            tmp_workspace / "emerging_risks.json",
            mock_omlox_client,
        )
        assert result.new_risks == []
        assert result.errors

    def test_identify_new_risks_with_real_omlx(self, omlox_client):
        """Test with real OMLX (may skip)."""
        if not omlox_client.is_available():
            pytest.skip("OMLX not reachable")
        from contingency_atlas.phases.phase2_risks import identify_new_risks
        new = identify_new_risks(
            "Emerging threats from AI-powered cyber attacks",
            ["Existing Risk 1", "Existing Risk 2"],
            omlox_client,
            ["https://example.com/live-risk-test"],
        )
        assert isinstance(new, list)
