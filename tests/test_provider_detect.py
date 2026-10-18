"""Backend detection: ask the server, do not guess the machine."""

from __future__ import annotations

import pytest

from contingency_atlas.config import Settings
from contingency_atlas.provider_detect import detect_provider, main


@pytest.fixture
def served(monkeypatch):
    """Control what each endpoint reports as its served model list."""
    listing: dict[str, list[str]] = {}

    def fake_probe(settings: Settings, timeout: float) -> list[str]:
        return listing.get(settings.llm_base_url, [])

    monkeypatch.setattr(
        "contingency_atlas.provider_detect._probe", fake_probe
    )
    return listing


def _model(provider: str) -> str:
    return Settings(llm_provider=provider).llm_model


def _url(provider: str) -> str:
    return Settings(llm_provider=provider).llm_base_url


def test_detects_dgx_when_the_endpoint_serves_the_dgx_model(served):
    served[_url("dgx")] = [_model("dgx")]
    result = detect_provider()
    assert result.provider == "dgx"
    assert bool(result) is True


def test_detects_omlx_when_the_endpoint_serves_the_omlx_model(served):
    served[_url("omlx")] = [_model("omlx"), "some-other-model"]
    assert detect_provider().provider == "omlx"


def test_unreachable_endpoint_is_a_non_answer_not_a_crash(served):
    result = detect_provider()
    assert result.provider is None
    assert "no model list" in result.reason


def test_wrong_model_reports_what_is_actually_served(served):
    """The trap this exists to catch: reachable, but serving something else.

    Naming the served model is the whole diagnostic — 'unreachable' would send
    the operator to the wrong problem entirely.
    """
    served[_url("omlx")] = ["Qwen/SomethingElse-FP8"]
    result = detect_provider()
    assert result.provider is None
    assert "neither configured model" in result.reason
    assert "Qwen/SomethingElse-FP8" in result.reason


def test_ambiguous_when_one_server_offers_both_models(served):
    """Picking one silently would be a guess wearing a fact's hat."""
    served[_url("omlx")] = [_model("omlx"), _model("dgx")]
    result = detect_provider()
    assert result.provider is None
    assert "ambiguous" in result.reason


def test_ambiguous_across_two_servers_names_both_endpoints(monkeypatch, served):
    """The realistic ambiguity: both backends genuinely running, on different
    ports. The message must say where each was found, or the operator cannot
    act on it."""
    monkeypatch.setenv("DGX_BASE_URL", "http://127.0.0.1:8123/v1")
    served[_url("omlx")] = [_model("omlx")]
    served["http://127.0.0.1:8123/v1"] = [_model("dgx")]
    result = detect_provider()
    assert result.provider is None
    assert "omlx at" in result.reason and "dgx at" in result.reason
    assert "8123" in result.reason
    assert "ATLAS_LLM_PROVIDER" in result.reason


def test_cli_prints_provider_and_exits_zero(served, capsys):
    served[_url("dgx")] = [_model("dgx")]
    assert main([]) == 0
    assert capsys.readouterr().out.strip() == "dgx"


def test_cli_exits_nonzero_and_prints_nothing_when_undecided(served, capsys):
    assert main([]) == 1
    assert capsys.readouterr().out.strip() == ""


def test_cli_verbose_explains_on_stderr_only(served, capsys):
    """stdout stays machine-readable so a shell can capture it directly."""
    served[_url("dgx")] = [_model("dgx")]
    assert main(["-v"]) == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == "dgx"
    assert "detect:" in captured.err


def test_detection_makes_no_network_call_during_settings_construction():
    """Config resolution must stay pure: a probe inside Settings would make
    every test that builds one non-deterministic and slow."""
    import contingency_atlas.provider_detect as mod

    calls = []
    original = mod._probe
    mod._probe = lambda *a, **k: calls.append(1) or []
    try:
        Settings()
        Settings(llm_provider="dgx")
        assert calls == []
    finally:
        mod._probe = original
