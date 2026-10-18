"""Tests for the selectable LLM backend (OMLX vs DGX vLLM).

Covers provider normalization, env/YAML resolution, the effective
llm_* accessors, and the DGX payload quirks in the choke-point client.
"""

import json

import httpx
import pytest

from contingency_atlas.config import Settings, load_yaml_settings, normalize_llm_provider
from contingency_atlas.llm import LLMError, OMLXClient
from contingency_atlas.scoring.telemetry import TelemetryLogger

_LLM_ENV = [
    "ATLAS_LLM_PROVIDER", "LLM_PROVIDER",
    "DGX_BASE_URL", "DGX_API_KEY", "DGX_MODEL", "DGX_MODEL_JUDGE",
    "DGX_MODEL_FINAL_JUDGE", "DGX_TIMEOUT", "DGX_MAX_TOKENS",
    "DGX_ENABLE_THINKING",
    "VLLM_BASE_URL", "VLLM_API_KEY", "VLLM_MODEL", "VLLM_TIMEOUT_S",
    "VLLM_MAX_TOKENS", "VLLM_ENABLE_THINKING",
]


@pytest.fixture(autouse=True)
def _clean_llm_env(monkeypatch):
    for name in _LLM_ENV:
        monkeypatch.delenv(name, raising=False)


# ── provider normalization ────────────────────────────────────────────


def test_provider_aliases():
    assert normalize_llm_provider("omlx") == "omlx"
    assert normalize_llm_provider("MLX") == "omlx"
    assert normalize_llm_provider("dgx") == "dgx"
    assert normalize_llm_provider("vllm") == "dgx"
    with pytest.raises(ValueError):
        normalize_llm_provider("openai")


def test_provider_env_selects_dgx(monkeypatch):
    monkeypatch.setenv("ATLAS_LLM_PROVIDER", "dgx")
    s = Settings()
    assert s.llm_provider == "dgx"
    assert s.is_dgx
    assert s.llm_model == s.dgx_model
    assert s.llm_model_judge == s.dgx_model_judge
    assert s.llm_timeout == s.dgx_timeout


def test_llm_provider_env_alias(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "vllm")
    assert Settings().llm_provider == "dgx"


def test_dgx_env_beats_vllm_alias(monkeypatch):
    monkeypatch.setenv("VLLM_MODEL", "Qwen/alias-model")
    monkeypatch.setenv("DGX_MODEL", "Qwen/primary-model")
    s = Settings(llm_provider="dgx")
    assert s.llm_model == "Qwen/primary-model"


def test_vllm_env_aliases_apply(monkeypatch):
    monkeypatch.setenv("VLLM_MODEL", "Qwen/Qwen3-Coder-Next-FP8")
    monkeypatch.setenv("VLLM_API_KEY", "local")
    monkeypatch.setenv("VLLM_TIMEOUT_S", "900")
    s = Settings(llm_provider="dgx")
    assert s.llm_model == "Qwen/Qwen3-Coder-Next-FP8"
    assert s.llm_api_key == "local"
    assert s.llm_timeout == 900.0


def test_explicit_constructor_args_beat_env(monkeypatch):
    monkeypatch.setenv("ATLAS_LLM_PROVIDER", "dgx")
    s = Settings(llm_provider="omlx")
    assert s.llm_provider == "omlx"
    assert s.llm_model == s.omlx_model


# ── YAML ──────────────────────────────────────────────────────────────


def test_yaml_provider_and_dgx_section(tmp_path, monkeypatch):
    cfg = tmp_path / "atlas.yaml"
    cfg.write_text(
        "provider: dgx\n"
        "dgx:\n"
        "  model: Qwen/test-model\n"
        "  timeout: 300\n"
        "  enable_thinking: true\n"
    )
    monkeypatch.setenv("ATLAS_CONFIG", str(cfg))
    s = Settings()
    assert s.llm_provider == "dgx"
    assert s.llm_model == "Qwen/test-model"
    assert s.llm_timeout == 300.0
    assert s.llm_enable_thinking is True


def test_yaml_rejects_unknown_dgx_key(tmp_path):
    cfg = tmp_path / "atlas.yaml"
    cfg.write_text("dgx:\n  modle: typo\n")
    with pytest.raises(ValueError, match="unknown dgx key"):
        load_yaml_settings(cfg)


def test_yaml_rejects_bad_provider(tmp_path):
    cfg = tmp_path / "atlas.yaml"
    cfg.write_text("provider: wat\n")
    with pytest.raises(ValueError, match="provider"):
        load_yaml_settings(cfg)


# ── client behaviour ──────────────────────────────────────────────────


def _client_with(handler, settings):
    client = OMLXClient(settings)
    client._client = httpx.Client(
        base_url="http://mock",
        headers={"Authorization": "Bearer mock"},
        transport=httpx.MockTransport(handler),
        timeout=1.0,
    )
    return client


def _capture_payload():
    seen = {}

    def handler(request):
        seen["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "ok"}}]},
        )

    return seen, handler


def test_dgx_payload_disables_thinking_by_default():
    seen, handler = _capture_payload()
    client = _client_with(handler, Settings(llm_provider="dgx"))
    client.chat([{"role": "user", "content": "hi"}])
    assert seen["payload"]["chat_template_kwargs"] == {"enable_thinking": False}
    assert seen["payload"]["model"] == "Qwen/Qwen3.6-27B-FP8"


def test_dgx_thinking_opt_in_omits_kwargs():
    seen, handler = _capture_payload()
    client = _client_with(
        handler, Settings(llm_provider="dgx", dgx_enable_thinking=True)
    )
    client.chat([{"role": "user", "content": "hi"}])
    assert "chat_template_kwargs" not in seen["payload"]


def test_omlx_payload_never_sends_thinking_kwargs():
    seen, handler = _capture_payload()
    client = _client_with(handler, Settings(llm_provider="omlx"))
    client.chat([{"role": "user", "content": "hi"}])
    assert "chat_template_kwargs" not in seen["payload"]


def test_omlx_responses_api_posts_input_not_messages():
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        seen["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "output_text": "from responses",
                "usage": {"input_tokens": 4, "output_tokens": 2},
            },
        )

    client = _client_with(
        handler,
        Settings(llm_provider="omlx", omlx_use_responses_api=True),
    )
    text = client.chat(
        [
            {"role": "system", "content": "Be brief."},
            {"role": "user", "content": "hi"},
        ],
        response_format={"type": "json_object"},
    )
    assert text == "from responses"
    assert seen["path"].endswith("/responses")
    assert "messages" not in seen["payload"]
    assert seen["payload"]["instructions"] == "Be brief."
    assert seen["payload"]["input"] == [{"role": "user", "content": "hi"}]
    assert seen["payload"]["max_output_tokens"] == Settings().omlx_max_tokens
    assert seen["payload"]["text"] == {"format": {"type": "json_object"}}
    assert seen["payload"]["stream"] is False


def test_omlx_responses_extracts_nested_output_text():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "nested"}],
                    }
                ]
            },
        )

    client = _client_with(
        handler,
        Settings(llm_provider="omlx", omlx_use_responses_api=True),
    )
    assert client.chat([{"role": "user", "content": "hi"}]) == "nested"


def test_omlx_responses_truncation_is_loud():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "status": "incomplete",
                "incomplete_details": {"reason": "max_output_tokens"},
                "output_text": "cut",
            },
        )

    client = _client_with(
        handler,
        Settings(llm_provider="omlx", omlx_use_responses_api=True),
    )
    from contingency_atlas.llm import LLMTruncatedError

    with pytest.raises(LLMTruncatedError, match="truncated"):
        client.chat([{"role": "user", "content": "hi"}])


def test_dgx_ignores_omlx_responses_flag():
    seen, handler = _capture_payload()
    client = _client_with(
        handler,
        Settings(llm_provider="dgx", omlx_use_responses_api=True),
    )
    client.chat([{"role": "user", "content": "hi"}])
    assert seen["payload"]["messages"][0]["content"] == "hi"


def test_responses_supported_treats_405_as_present():
    def handler(request):
        return httpx.Response(405, json={"detail": "Method Not Allowed"})

    client = _client_with(handler, Settings(llm_provider="omlx"))
    supported, detail = client.responses_supported()
    assert supported is True
    assert "405" in detail


def test_responses_supported_treats_404_as_absent():
    def handler(request):
        return httpx.Response(404, json={"detail": "Not Found"})

    client = _client_with(handler, Settings(llm_provider="omlx"))
    supported, detail = client.responses_supported()
    assert supported is False
    assert "404" in detail


def test_reasoning_content_fallback():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": "", "reasoning_content": "the answer"}}
                ]
            },
        )

    client = _client_with(handler, Settings(llm_provider="dgx"))
    assert client.chat([{"role": "user", "content": "hi"}]) == "the answer"


def test_dgx_telemetry_provider_label(tmp_path):
    logger = TelemetryLogger(tmp_path / "t.jsonl", run_id="t")
    client = _client_with(_capture_payload()[1], Settings(llm_provider="dgx"))
    client._telemetry = logger
    client.chat([{"role": "user", "content": "hi"}])
    assert logger.load_events()[0]["provider"] == "dgx"


def test_safe_dict_carries_provider_without_keys():
    s = Settings(llm_provider="dgx", dgx_api_key="secret")
    d = s.to_safe_dict()
    assert d["llm_provider"] == "dgx"
    assert d["dgx_model"] == s.dgx_model
    assert "dgx_api_key" not in d
    assert "omlx_api_key" not in d


# ── outbound boundary ────────────────────────────────────────────────────


def test_remote_endpoint_is_refused_by_default(monkeypatch):
    """security.md states inference is local and outbound traffic exists in
    exactly one feature. DGX_BASE_URL / VLLM_BASE_URL may be inherited, so
    a remote value would quietly turn every prompt into uninspected egress."""
    monkeypatch.delenv("ATLAS_ALLOW_REMOTE_LLM", raising=False)
    with pytest.raises(LLMError, match="not loopback"):
        OMLXClient(Settings(
            llm_provider="dgx", dgx_base_url="https://api.example.org/v1"))


def test_remote_endpoint_allowed_with_explicit_opt_in(monkeypatch):
    monkeypatch.setenv("ATLAS_ALLOW_REMOTE_LLM", "1")
    client = OMLXClient(Settings(
        llm_provider="dgx", dgx_base_url="https://api.example.org/v1"))
    assert client._base_url == "https://api.example.org/v1"


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost"])
def test_loopback_endpoints_need_no_opt_in(monkeypatch, host):
    """A DGX reached over an SSH tunnel is loopback and must not be gated."""
    monkeypatch.delenv("ATLAS_ALLOW_REMOTE_LLM", raising=False)
    client = OMLXClient(Settings(
        llm_provider="dgx", dgx_base_url=f"http://{host}:8000/v1"))
    assert client.settings.llm_provider == "dgx"


def test_client_does_not_trust_ambient_proxy_for_loopback(monkeypatch):
    """Constructed for real, not through MockTransport: a loopback call must
    not be routed via an ambient proxy, while a permitted remote one may be."""
    monkeypatch.delenv("ATLAS_ALLOW_REMOTE_LLM", raising=False)
    local = OMLXClient(Settings())
    assert local.client.trust_env is False
    monkeypatch.setenv("ATLAS_ALLOW_REMOTE_LLM", "1")
    remote = OMLXClient(Settings(
        llm_provider="dgx", dgx_base_url="https://api.example.org/v1"))
    assert remote.client.trust_env is True
