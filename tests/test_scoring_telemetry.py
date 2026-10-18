"""Telemetry tests: the 100% call-coverage invariant and run-report
consistency (telemetry-spec.md section 9)."""

import pytest

from contingency_atlas.scoring.telemetry import TelemetryLogger
from contingency_atlas.scoring.models import IncidentFeature, RiskAtom


def _stub_llm(messages, **kwargs):
    return {"content": "ok", "usage": {"input_tokens": 12, "output_tokens": 5}}


def _flaky_llm(messages, **kwargs):
    raise RuntimeError("backend down")


def test_coverage_invariant_every_call_has_event(tmp_path):
    logger = TelemetryLogger(tmp_path / "telemetry.jsonl", run_id="r1")
    n_calls = 5
    for i in range(n_calls):
        logger.instrumented_call(
            _stub_llm,
            [{"role": "user", "content": f"q{i}"}],
            node="score_jdg",
            phase="evaluation",
            provider="mlx",
            model_id="local/stub",
        )
    events = logger.load_events()
    # counter invariant: one telemetry event per LLM call, no more, no less
    assert len(events) == n_calls
    assert all(e["status"] == "ok" for e in events)
    assert all(e["plane"] == "public" for e in events)
    assert all(e["input_tokens"] == 12 and e["output_tokens"] == 5 for e in events)


def test_error_calls_emit_error_event(tmp_path):
    logger = TelemetryLogger(tmp_path / "telemetry.jsonl", run_id="r1")
    with pytest.raises(RuntimeError):
        logger.instrumented_call(
            _flaky_llm,
            [],
            node="score_jdg",
            phase="evaluation",
            provider="mlx",
            model_id="local/stub",
        )
    events = logger.load_events()
    assert len(events) == 1
    assert events[0]["status"] == "error"
    assert events[0]["error_class"] == "RuntimeError"


def test_scoring_planes_are_closed_and_shared_with_telemetry(tmp_path):
    assert RiskAtom(risk_id="public-risk", title="Public").plane == "public"
    assert IncidentFeature(
        cell_id="cell",
        scoring_cell_code="DEMO-CELL-TECHNOLOGY",
        segment_code="DEMO-SEGMENT-01",
        period=1,
        count=5,
        severity_quantiles={"0.5": 10.0},
        n_severity=5,
        plane="aggregate",
    ).plane == "aggregate"
    logger = TelemetryLogger(tmp_path / "planes.jsonl")
    assert logger.emit(node="local", phase="control", plane="local_detail")[
        "plane"
    ] == "local_detail"

    with pytest.raises(ValueError, match="plane must be one of"):
        RiskAtom(risk_id="invalid-risk", title="Invalid", plane="retired_plane")
    with pytest.raises(ValueError, match="plane must be one of"):
        logger.emit(node="invalid", phase="control", plane="retired_plane")


def test_run_report_cost_consistency(tmp_path):
    logger = TelemetryLogger(
        tmp_path / "telemetry.jsonl",
        run_id="r1",
        pricing={
            "version": "test-1",
            "models": {
                "local/stub": {
                    "input_per_mtok": 1.0,
                    "output_per_mtok": 2.0,
                    "shadow_input_per_mtok": 0.05,
                    "shadow_output_per_mtok": 0.05,
                }
            },
        },
    )
    for _ in range(3):
        logger.instrumented_call(
            _stub_llm, [], node="n", phase="generation",
            provider="mlx", model_id="local/stub",
        )
    logger.emit(node="egress", phase="control", status="denied_egress")
    report = logger.run_report()
    expected = 3 * (12 / 1e6 * 1.0 + 5 / 1e6 * 2.0)
    assert abs(report["spend_usd"] - expected) < 0.001
    assert report["egress_denies"] == 1
    assert report["tokens"] == {"input": 36, "output": 15}
    assert report["spend_shadow_usd"] > 0


def test_run_report_cost_per_promoted(tmp_path):
    logger = TelemetryLogger(tmp_path / "telemetry.jsonl", run_id="r1")
    out = logger.write_run_report(tmp_path / "run_report.json", n_promoted=0)
    import json

    report = json.loads(out.read_text())
    assert report["cost_per_promoted_usd"] is None
    assert report["n_promoted"] == 0
