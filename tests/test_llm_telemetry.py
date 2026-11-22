"""Tests for choke-point LLM telemetry: every OMLXClient call emits an
event when instrumented, none when not."""

import json

import httpx
import pytest

from workbench_atlas.llm import LLMError, OMLXClient
from workbench_atlas.scoring.telemetry import TelemetryLogger


def _client_with(handler, telemetry=None, node="test-node"):
    client = OMLXClient(telemetry=telemetry, node=node)
    client._client = httpx.Client(
        base_url="http://mock",
        headers={"Authorization": "Bearer mock"},
        transport=httpx.MockTransport(handler),
        timeout=1.0,
    )
    return client


def _ok_response(request):
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": '{"score": 0.9}'}}],
            "usage": {"prompt_tokens": 42, "completion_tokens": 7},
        },
    )


def test_success_emits_event_with_usage(tmp_path):
    logger = TelemetryLogger(tmp_path / "t.jsonl", run_id="t")
    client = _client_with(_ok_response, telemetry=logger)
    out = client.chat([{"role": "user", "content": "hi"}])
    assert out == '{"score": 0.9}'
    events = logger.load_events()
    assert len(events) == 1
    e = events[0]
    assert e["status"] == "ok"
    assert e["node"] == "test-node"
    assert e["provider"] == "mlx"
    assert e["model_id"]
    assert e["input_tokens"] == 42
    assert e["output_tokens"] == 7
    assert e["latency_ms"] >= 0


def test_missing_usage_records_zeros(tmp_path):
    def handler(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    logger = TelemetryLogger(tmp_path / "t.jsonl", run_id="t")
    client = _client_with(handler, telemetry=logger)
    client.chat([{"role": "user", "content": "hi"}])
    e = logger.load_events()[0]
    assert e["input_tokens"] == 0 and e["output_tokens"] == 0
    assert e["status"] == "ok"


def test_errors_emit_error_events_per_attempt(tmp_path):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(500, text="boom")

    logger = TelemetryLogger(tmp_path / "t.jsonl", run_id="t")
    client = _client_with(handler, telemetry=logger)
    with pytest.raises(LLMError):
        client.chat([{"role": "user", "content": "hi"}])
    events = logger.load_events()
    # tenacity retries (3 attempts) — each attempt is logged
    assert len(calls) == 3
    assert len(events) == 3
    assert all(e["status"] == "error" for e in events)
    assert all(e["error_class"] == "LLMError" for e in events)


def test_no_telemetry_by_default(tmp_path):
    client = _client_with(_ok_response)
    client.chat([{"role": "user", "content": "hi"}])
    assert not (tmp_path / "t.jsonl").exists()


def test_node_label_override(tmp_path):
    logger = TelemetryLogger(tmp_path / "t.jsonl", run_id="t")
    client = _client_with(_ok_response, telemetry=logger, node="default")
    client.chat([{"role": "user", "content": "hi"}], node="phase8")
    assert logger.load_events()[0]["node"] == "phase8"


def test_chat_json_emits_per_underlying_call(tmp_path):
    logger = TelemetryLogger(tmp_path / "t.jsonl", run_id="t")
    client = _client_with(_ok_response, telemetry=logger)
    result = client.chat_json([{"role": "user", "content": "hi"}])
    assert result == {"score": 0.9}
    # json mode succeeds -> exactly one underlying chat call
    assert len(logger.load_events()) == 1


def test_scenario_id_tags_the_event(tmp_path):
    """scenario-scoped callers (Phase 3) attribute tokens to the scenario."""
    logger = TelemetryLogger(tmp_path / "t.jsonl", run_id="t")
    client = _client_with(_ok_response, telemetry=logger)
    client.chat([{"role": "user", "content": "hi"}], scenario_id="SCN-2099-001")
    e = logger.load_events()[0]
    assert e["scenario_id"] == "SCN-2099-001"
    assert e["input_tokens"] == 42


def test_scenario_id_defaults_to_none(tmp_path):
    logger = TelemetryLogger(tmp_path / "t.jsonl", run_id="t")
    client = _client_with(_ok_response, telemetry=logger)
    client.chat([{"role": "user", "content": "hi"}])
    assert logger.load_events()[0]["scenario_id"] is None
