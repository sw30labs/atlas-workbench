"""Tests for LangGraph pipeline orchestration."""

from types import SimpleNamespace

import pytest


class TestBuildPipeline:
    def test_build_pipeline_compiles(self):
        from workbench_atlas.pipeline import build_pipeline
        app = build_pipeline()
        assert app is not None

    def test_pipeline_nodes_exist(self):
        from workbench_atlas.pipeline import (
            node_phase1, node_phase2, node_phase3,
            node_phase4, node_phase5, node_phase6,
        )
        assert callable(node_phase1)
        assert callable(node_phase6)


class TestRunPipelineOffline:
    def test_run_skip_llm(self, tmp_workspace, settings):
        """Full graph runs offline without calling OMLX."""
        from workbench_atlas.pipeline import run_pipeline
        from workbench_atlas.config import Settings

        s = Settings(
            scenario_db_path=tmp_workspace / "atlas.db",
            evidence_db_path=tmp_workspace / "event_evidence.db",
            emerging_risks_path=tmp_workspace / "emerging_risks.json",
            driftlab_enabled=True,
        )
        # Ensure schema
        from workbench_atlas.schema import migrate
        migrate(str(s.scenario_db_path))

        result = run_pipeline(
            s,
            skip_llm=True,
            num_scenarios=0,
            evidence_texts=[],
            source_text="",
        )
        assert result.run_id
        assert result.state.get("phase1", {}).get("status") == "skipped"
        assert result.state.get("phase2", {}).get("status") == "skipped"
        assert result.state.get("phase3", {}).get("status") == "skipped"
        assert result.state.get("phase4", {}).get("status") == "ok"
        assert result.state.get("phase5", {}).get("status") == "ok"
        assert result.state.get("phase6", {}).get("status") == "ok"
        # DriftLab score may or may not be set depending on baseline availability
        assert "errors" in result.summary()

    def test_phase4_node_directly(self, tmp_workspace):
        from workbench_atlas.pipeline import node_phase4
        from workbench_atlas.schema import migrate
        migrate(str(tmp_workspace / "atlas.db"))
        state = {
            "run_id": "t1",
            "settings": {
                "scenario_db_path": str(tmp_workspace / "atlas.db"),
                "evidence_db_path": str(tmp_workspace / "event_evidence.db"),
                "emerging_risks_path": str(tmp_workspace / "emerging_risks.json"),
                "judge_score_threshold": 0.75,
                "max_judge_iterations": 3,
            },
            "errors": [],
            "driftlab_events": [],
            "skip_llm": True,
        }
        out = node_phase4(state)
        assert out["phase4"]["status"] == "ok"
        assert any(e.get("node") == "phase4" for e in out["driftlab_events"])

    def test_phase4_result_errors_propagate(self, monkeypatch, tmp_workspace):
        from workbench_atlas import pipeline
        from workbench_atlas.phases import phase4_prioritize

        monkeypatch.setattr(
            phase4_prioritize,
            "run_prioritization_cycle",
            lambda *a, **k: SimpleNamespace(updated=0, errors=["score failed"]),
        )
        monkeypatch.setattr(
            phase4_prioritize,
            "get_prioritized_portfolio",
            lambda *a, **k: [],
        )
        out = pipeline.node_phase4(self._node_state(tmp_workspace))
        assert out["phase4"]["status"] == "error"
        assert "phase4: score failed" in out["errors"]

    def test_phase5_result_errors_propagate(self, monkeypatch, tmp_workspace):
        from workbench_atlas import pipeline
        from workbench_atlas.phases import phase5_patterns

        result = SimpleNamespace(
            temporal_findings=[],
            thematic_patterns=[],
            llm_synthesis=[],
            errors=["analysis failed"],
        )
        monkeypatch.setattr(
            phase5_patterns,
            "run_pattern_analysis",
            lambda *a, **k: result,
        )
        monkeypatch.setattr(
            phase5_patterns,
            "persist_findings",
            lambda *a, **k: 0,
        )
        out = pipeline.node_phase5(self._node_state(tmp_workspace))
        assert out["phase5"]["status"] == "error"
        assert "phase5: analysis failed" in out["errors"]

    def test_phase6_result_errors_propagate(self, monkeypatch, tmp_workspace):
        from workbench_atlas import pipeline
        from workbench_atlas.phases import phase6_maintain

        result = SimpleNamespace(
            retired=0,
            flagged_stale=0,
            actions=[],
            errors=["maintenance failed"],
        )
        monkeypatch.setattr(
            phase6_maintain,
            "run_maintenance_cycle",
            lambda *a, **k: result,
        )
        out = pipeline.node_phase6(self._node_state(tmp_workspace))
        assert out["phase6"]["status"] == "error"
        assert "phase6: maintenance failed" in out["errors"]

    def test_phase6_passes_approved_evidence_store_for_signposts(
        self,
        monkeypatch,
        tmp_workspace,
    ):
        from workbench_atlas import pipeline
        from workbench_atlas.phases import phase6_maintain

        captured = {}

        def fake_cycle(*args, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                retired=0,
                flagged_stale=0,
                actions=[],
                signposts_evaluated=2,
                errors=[],
            )

        monkeypatch.setattr(
            phase6_maintain,
            "run_maintenance_cycle",
            fake_cycle,
        )
        state = self._node_state(tmp_workspace)
        state["skip_llm"] = True
        out = pipeline.node_phase6(state)

        assert captured["evidence_db_path"] == str(
            tmp_workspace / "event_evidence.db"
        )
        assert out["phase6"]["signposts_evaluated"] == 2

    @staticmethod
    def _node_state(tmp_workspace):
        return {
            "run_id": "error-propagation",
            "settings": {
                "scenario_db_path": str(tmp_workspace / "atlas.db"),
                "evidence_db_path": str(tmp_workspace / "event_evidence.db"),
                "emerging_risks_path": str(
                    tmp_workspace / "emerging_risks.json"
                ),
                "driftlab_enabled": False,
            },
            "errors": [],
            "driftlab_events": [],
            "skip_llm": True,
        }

    def test_pipeline_state_excludes_api_key(self, tmp_workspace):
        """The graph state must never carry the OMLX API key."""
        from workbench_atlas.pipeline import run_pipeline
        from workbench_atlas.config import Settings
        from workbench_atlas.schema import migrate

        s = Settings(
            scenario_db_path=tmp_workspace / "atlas.db",
            evidence_db_path=tmp_workspace / "event_evidence.db",
            emerging_risks_path=tmp_workspace / "emerging_risks.json",
            omlx_api_key="super-secret",
            driftlab_enabled=True,
        )
        migrate(str(s.scenario_db_path))
        result = run_pipeline(s, skip_llm=True, num_scenarios=0)
        assert "omlx_api_key" not in result.state["settings"]
