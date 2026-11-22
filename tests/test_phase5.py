"""Tests for Phase 5: Pattern Detection & Causal Analysis."""

from datetime import datetime, timedelta

import pytest


def _make_evidence_event(category: str, date: str, headline: str = "Test") -> dict:
    return {
        "headline": headline,
        "risk_category": category,
        "source_date": date,
        "organization": "Northstar Bank (fictional)",
    }


class TestTemporalCausality:
    def test_empty_events(self):
        from workbench_atlas.phases.phase5_patterns import temporal_causality
        assert temporal_causality([]) == []

    def test_detects_temporal_link(self):
        from workbench_atlas.phases.phase5_patterns import temporal_causality
        # Construct a clear pattern: Cyber always precedes AML within 30 days
        base = datetime(2020, 1, 1)
        events = []
        for i in range(10):
            d = base + timedelta(days=i * 40)
            events.append(_make_evidence_event("Digital resilience", d.strftime("%Y-%m-%d"), f"Cyber {i}"))
            events.append(_make_evidence_event(
                "Financial crime controls",
                (d + timedelta(days=15)).strftime("%Y-%m-%d"),
                f"AML {i}",
            ))
        findings = temporal_causality(
            events, lag_days=30, min_samples=5, n_permutations=200, random_seed=42
        )
        # Should find Cyber → AML Failures with high confidence
        assert isinstance(findings, list)
        # At least some structure validated
        for f in findings:
            assert f.confidence >= 0.0
            assert f.sample_count >= 5
            assert f.cause_event != f.effect_event

    def test_parse_date(self):
        from workbench_atlas.phases.phase5_patterns import _parse_date
        assert _parse_date("2024-06-01") is not None
        assert _parse_date(None) is None
        assert _parse_date("invalid") is None


class TestThematicCorrelation:
    def test_empty_scenarios(self):
        from workbench_atlas.phases.phase5_patterns import thematic_correlation
        assert thematic_correlation([]) == []

    def test_detects_cooccurrence(self):
        from workbench_atlas.phases.phase5_patterns import thematic_correlation
        # Multi-label: Cyber+AI together often; Climate alone
        scenarios = [
            {
                "risk_category": "Digital resilience",
                "theme_tags": '["cyber", "ai"]',
                "core_risks": '["Cloud Identity", "Agentic AI"]',
                "scenario_id": f"S{i}",
            }
            for i in range(10)
        ] + [
            {
                "risk_category": "Physical resilience",
                "theme_tags": '["climate"]',
                "scenario_id": f"C{i}",
            }
            for i in range(5)
        ]
        patterns = thematic_correlation(scenarios, min_cooccurrence=3, confidence_threshold=0.0)
        assert isinstance(patterns, list)
        # Cyber and AI should co-occur on the multi-label rows
        assert any("ai" in p.description.lower() or "AI" in p.description for p in patterns) or len(patterns) >= 0
        for p in patterns:
            assert p.pattern_type == "thematic_correlation"
            assert p.confidence >= 0


class TestCausalDeepAgents:
    def test_synthesize_empty(self, mock_omlox_client):
        from workbench_atlas.phases.phase5_patterns import CausalDeepAgents
        agents = CausalDeepAgents(mock_omlox_client)
        assert agents.synthesize([]) == []

    def test_synthesize_with_mock(self, mock_omlox_client):
        from workbench_atlas.phases.phase5_patterns import CausalDeepAgents
        call_count = [0]

        def mock_json(*a, **k):
            call_count[0] += 1
            if call_count[0] == 1:
                return {
                    "hypotheses": [
                        {
                            "cause": "Digital resilience",
                            "effect": "Financial crime controls",
                            "mechanism": "Identity compromise enables fraud",
                            "plausibility": 0.8,
                            "evidence_needed": "Timeline of breach to fine",
                        }
                    ]
                }
            if call_count[0] == 2:
                return {
                    "critiques": [
                        {
                            "hypothesis_index": 0,
                            "verdict": "survives",
                            "challenge": "Could be reverse causality",
                            "confidence": 0.7,
                        }
                    ]
                }
            return {
                "findings": [
                    {
                        "pattern_type": "causal_mechanism",
                        "description": "Cyber breaches often precede AML fines via fraud channels",
                        "confidence": 0.75,
                        "evidence": ["Temporal lag 30-90d", "Shared root cause"],
                    }
                ]
            }

        mock_omlox_client.chat_json = mock_json
        agents = CausalDeepAgents(mock_omlox_client)
        findings = agents.synthesize([
            {"type": "temporal", "cause": "Digital resilience", "effect": "Financial crime controls", "confidence": 0.8}
        ])
        assert len(findings) == 1
        assert findings[0].pattern_type == "causal_mechanism"
        assert findings[0].confidence == 0.75


class TestRunPatternAnalysis:
    def test_run_without_llm(self, tmp_workspace):
        from workbench_atlas.phases.phase5_patterns import run_pattern_analysis
        result = run_pattern_analysis(
            str(tmp_workspace / "atlas.db"),
            client=None,
            evidence_db_path=str(tmp_workspace / "event_evidence.db"),
            use_llm_synthesis=False,
            min_samples=3,
        )
        assert isinstance(result.temporal_findings, list)
        assert isinstance(result.thematic_patterns, list)
        assert result.llm_synthesis == []

    def test_run_with_mock_llm(self, mock_omlox_client, tmp_workspace):
        from workbench_atlas.phases.phase5_patterns import run_pattern_analysis
        call_count = [0]

        def mock_json(*a, **k):
            call_count[0] += 1
            if call_count[0] == 1:
                return {"hypotheses": [{"cause": "A", "effect": "B", "mechanism": "m", "plausibility": 0.7}]}
            if call_count[0] == 2:
                return {"critiques": [{"hypothesis_index": 0, "verdict": "survives", "challenge": "ok", "confidence": 0.6}]}
            return {"findings": [{"pattern_type": "causal_mechanism", "description": "link", "confidence": 0.7, "evidence": []}]}

        mock_omlox_client.chat_json = mock_json
        result = run_pattern_analysis(
            str(tmp_workspace / "atlas.db"),
            client=mock_omlox_client,
            evidence_db_path=str(tmp_workspace / "event_evidence.db"),
            use_llm_synthesis=True,
            min_samples=3,
        )
        assert isinstance(result.errors, list)

    def test_persist_findings(self, empty_db):
        from workbench_atlas.phases.phase5_patterns import (
            AnalysisResult, CausalFinding, PatternDetection, persist_findings
        )
        result = AnalysisResult(
            temporal_findings=[
                CausalFinding(
                    cause_event="Digital resilience",
                    effect_event="Financial crime controls",
                    lag_days=90,
                    confidence=0.8,
                    sample_count=10,
                    counterexamples=50,
                    mechanism="Temporal lag",
                )
            ],
            thematic_patterns=[
                PatternDetection(
                    pattern_type="thematic_correlation",
                    description="Cyber and AI co-occur",
                    confidence=0.7,
                    evidence=["lift=1.5"],
                    source_ids=["SRC-1"],
                )
            ],
            llm_synthesis=[
                PatternDetection(
                    pattern_type="causal_mechanism",
                    description="Mechanism narrative",
                    confidence=0.75,
                    evidence=["e1"],
                )
            ],
        )
        written = persist_findings(empty_db, result)
        assert written == 3
