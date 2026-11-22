"""OpenAI-compatible LLM client — single choke point, two backends.

Backends (Settings.llm_provider):
  - "omlx": OMLX on the Mac (non-streaming required — OMLX hangs with
    streaming=True on large models).
  - "dgx": vLLM on the NVIDIA DGX. Also non-streaming; Qwen3.x gets
    chat_template_kwargs enable_thinking=false unless dgx_enable_thinking is
    set.

All calls go through httpx for reliability.

Telemetry (telemetry-spec.md section 2): this client is the single choke
point for LLM traffic — pass a ``TelemetryLogger`` and every chat call
emits one event per attempt (tokens from the endpoint's ``usage`` when
present, latency, status, node label, provider). Un-instrumented by
default so tests and offline paths stay quiet.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

import httpx
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception,
)

from .config import Settings
from .scoring.telemetry import TelemetryLogger

logger = logging.getLogger(__name__)


def _is_loopback(url: str) -> bool:
    host = httpx.URL(url).host
    return host in ("127.0.0.1", "localhost", "::1")


class LLMError(Exception):
    """Raised when the LLM backend is unreachable or returns an error."""


class LLMTruncatedError(LLMError):
    """The model stopped because it hit max_tokens, so the answer is cut off.

    Kept distinct from LLMError because the two want opposite handling: a
    connection blip is worth retrying, a budget that is too small produces
    exactly the same truncation every attempt. It subclasses LLMError so
    existing ``except LLMError`` handlers keep working, and :func:`_retryable`
    excludes it so a deterministic failure is not paid for three times.

    Silent truncation is the worst case of all, because a cut-off response is
    not obviously broken: JSON stops mid-structure, and the salvage path in
    :meth:`OMLXClient.chat_json` can still recover an inner fragment that
    looks like a healthy value. Failing loudly here is what makes the budget
    the visible problem instead of a downstream schema mismatch.
    """


def _retryable(exc: BaseException) -> bool:
    """Retry transport faults, never a truncated completion."""
    if isinstance(exc, LLMTruncatedError):
        return False
    return isinstance(exc, (httpx.HTTPError, LLMError))


def _messages_to_responses(
    messages: list[dict[str, str]],
) -> tuple[str | None, list[dict[str, str]] | str]:
    """Split chat messages into Responses ``instructions`` + ``input``."""
    instructions: list[str] = []
    items: list[dict[str, str]] = []
    for message in messages:
        role = str(message.get("role") or "user")
        content = str(message.get("content") or "")
        if role == "system":
            if content:
                instructions.append(content)
            continue
        items.append({"role": role, "content": content})
    return (
        "\n\n".join(instructions) if instructions else None,
        items if items else "",
    )


def _responses_output_text(data: dict[str, Any]) -> str:
    """Pull assistant text out of a Responses API body."""
    direct = data.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct
    chunks: list[str] = []
    for item in data.get("output") or []:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if isinstance(content, str) and content.strip():
            chunks.append(content)
            continue
        if isinstance(content, list):
            for part in content:
                if isinstance(part, str) and part.strip():
                    chunks.append(part)
                elif isinstance(part, dict):
                    text = part.get("text") or part.get("output_text") or ""
                    if text:
                        chunks.append(str(text))
        reasoning = item.get("reasoning") or item.get("summary")
        if not content and isinstance(reasoning, str) and reasoning.strip():
            chunks.append(reasoning)
    return "".join(chunks)


def _normalize_usage(usage: dict[str, Any] | None) -> dict[str, Any]:
    """Map chat or Responses usage onto the telemetry field names."""
    usage = usage or {}
    return {
        "prompt_tokens": int(
            usage.get("prompt_tokens") or usage.get("input_tokens") or 0
        ),
        "completion_tokens": int(
            usage.get("completion_tokens") or usage.get("output_tokens") or 0
        ),
        "cache_read_tokens": int(usage.get("cache_read_tokens") or 0),
        "cache_write_tokens": int(usage.get("cache_write_tokens") or 0),
    }


def _responses_truncated(data: dict[str, Any]) -> bool:
    if data.get("status") == "incomplete":
        reason = str((data.get("incomplete_details") or {}).get("reason") or "")
        if reason in {"max_output_tokens", "max_tokens", "length"}:
            return True
        if not reason:
            return True
    for item in data.get("output") or []:
        if isinstance(item, dict) and item.get("status") == "incomplete":
            return True
    return False


def _require_local_endpoint(base_url: str, provider: str) -> None:
    """Refuse to send prompts off-box unless the operator says so.

    docs/reference/security.md states that all inference is local and that
    outbound web traffic exists in exactly one feature (deep research). The
    DGX backend reads its base URL from DGX_BASE_URL / VLLM_BASE_URL, so a
    remote value would quietly turn every prompt —
    register titles, evidence prose, scenario drafts — into egress that no
    gateway inspects. A DGX reached over an SSH tunnel is loopback and passes.
    """
    if _is_loopback(base_url):
        return
    host = httpx.URL(base_url).host
    if os.environ.get("ATLAS_ALLOW_REMOTE_LLM", "").strip().lower() in {
        "1", "true", "yes", "on",
    }:
        logger.warning(
            "%s endpoint %s is NOT loopback: prompt content leaves this host "
            "(permitted by ATLAS_ALLOW_REMOTE_LLM)", provider, host,
        )
        return
    raise LLMError(
        f"{provider} endpoint {base_url!r} is not loopback. Prompts carry "
        "register and evidence content, so sending them off-box is opt-in: "
        "tunnel the backend (ssh -L 8000:127.0.0.1:8000 <host>) or set "
        "ATLAS_ALLOW_REMOTE_LLM=1 to accept the egress."
    )


class OMLXClient:
    """Thin OpenAI-compatible client backed by OMLX.

    Non-streaming only (OMLX hangs with streaming=True on large models).
    All public methods retry with exponential backoff on transient errors.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        telemetry: TelemetryLogger | None = None,
        node: str = "llm",
    ) -> None:
        self.settings = settings or Settings()
        self._base_url = self.settings.llm_base_url.rstrip("/")
        _require_local_endpoint(self._base_url, self.settings.llm_provider)
        self._api_key = self.settings.llm_api_key
        self._timeout = self.settings.llm_timeout
        self._client: httpx.Client | None = None
        # Set to True once json_object mode proves broken (echo/{} responses),
        # so subsequent calls skip straight to plain mode + extraction.
        self._json_mode_broken = False
        self._telemetry = telemetry
        self._node = node

    @property
    def _is_dgx(self) -> bool:
        return getattr(self.settings, "llm_provider", "omlx") == "dgx"

    @property
    def _backend(self) -> str:
        """Human label for log/error messages."""
        return "DGX" if self._is_dgx else "OMLX"

    @property
    def _provider_label(self) -> str:
        """Telemetry provider tag — 'mlx' kept for backward compatibility."""
        return "dgx" if self._is_dgx else "mlx"

    def _emit(
        self,
        *,
        model: str,
        status: str,
        latency_ms: int,
        usage: dict[str, Any] | None = None,
        error_class: str | None = None,
        node: str | None = None,
        scenario_id: str | None = None,
    ) -> None:
        """One telemetry event per call attempt; never breaks the caller."""
        if self._telemetry is None:
            return
        u = usage or {}
        try:
            self._telemetry.emit(
                node=node or self._node,
                phase="evaluation",
                provider=self._provider_label,
                model_id=model,
                input_tokens=int(u.get("prompt_tokens") or 0),
                output_tokens=int(u.get("completion_tokens") or 0),
                cache_read_tokens=int(u.get("cache_read_tokens") or 0),
                cache_write_tokens=int(u.get("cache_write_tokens") or 0),
                latency_ms=latency_ms,
                status=status,
                error_class=error_class,
                scenario_id=scenario_id,
            )
        except Exception:
            logger.warning("telemetry emit failed", exc_info=True)

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                base_url=self._base_url,
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=self._timeout,
                # Loopback servers (both backends' default) must never be
                # routed through an ambient http_proxy.
                trust_env=not _is_loopback(self._base_url),
            )
        return self._client

    def close(self) -> None:
        if self._client:
            self._client.close()
            self._client = None

    # ── Public API ──

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=2, max=30),
        retry=retry_if_exception(_retryable),
        reraise=True,
    )
    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        response_format: dict[str, str] | None = None,
        node: str | None = None,
        scenario_id: str | None = None,
    ) -> str:
        """Send a chat completion request and return the assistant content.

        ``scenario_id`` tags the telemetry event so scenario-scoped calls
        (e.g. Phase 3 generation) attribute their tokens to the scenario.

        Raises LLMError on non-2xx responses or empty content.
        """
        model = model or self.settings.llm_model
        max_tokens = max_tokens or self.settings.llm_max_tokens
        use_responses = bool(self.settings.llm_use_responses_api)
        if use_responses:
            instructions, items = _messages_to_responses(messages)
            payload = {
                "model": model,
                "input": items,
                "temperature": temperature,
                "max_output_tokens": max_tokens,
                "stream": False,
            }
            if instructions:
                payload["instructions"] = instructions
            if response_format and response_format.get("type"):
                payload["text"] = {"format": {"type": response_format["type"]}}
        else:
            payload = {
                "model": model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
            if response_format:
                payload["response_format"] = response_format
            if self._is_dgx and not self.settings.llm_enable_thinking:
                # Qwen3.x on vLLM can burn the whole budget in reasoning_content
                # and return empty content — thinking stays opt-in on the DGX.
                payload["chat_template_kwargs"] = {"enable_thinking": False}

        start = time.perf_counter()
        data: dict[str, Any] | None = None
        try:
            try:
                path = "/responses" if use_responses else "/chat/completions"
                resp = self.client.post(path, json=payload)
                resp.raise_for_status()
            except httpx.HTTPStatusError as e:
                raise LLMError(f"{self._backend} HTTP {e.response.status_code}: {e.response.text[:200]}") from e
            except httpx.HTTPError as e:
                raise LLMError(f"{self._backend} connection error: {e}") from e

            data = resp.json()
            if use_responses:
                content = _responses_output_text(data)
                if _responses_truncated(data):
                    raise LLMTruncatedError(
                        f"{self._backend} hit the {max_tokens}-token ceiling and the "
                        f"response is truncated ({len(content)} chars). Raise "
                        f"max_tokens for this call or omit it to use the configured "
                        f"limit; retrying the same budget cannot succeed."
                    )
            else:
                choices = data.get("choices", [])
                if not choices:
                    raise LLMError(f"{self._backend} returned no choices: {data}")
                choice = choices[0]
                message = choice.get("message", {})
                # Thinking models may leave the answer in reasoning_content.
                content = message.get("content") or message.get("reasoning_content") or ""
                if choice.get("finish_reason") == "length":
                    # The budget, not the model, is the defect. Say so with the
                    # number the operator has to change — a caller that hardcodes
                    # max_tokens silently overrides the configured ceiling.
                    raise LLMTruncatedError(
                        f"{self._backend} hit the {max_tokens}-token ceiling and the "
                        f"response is truncated ({len(content)} chars). Raise "
                        f"max_tokens for this call or omit it to use the configured "
                        f"limit; retrying the same budget cannot succeed."
                    )
            if not content:
                raise LLMError(f"{self._backend} returned empty content")
        except Exception as exc:
            # A truncated call is the most expensive kind there is — it
            # generated the entire budget before failing — and the usage block
            # is already parsed when it raises. Booking it at zero tokens would
            # make the ledger understate cost exactly where it is highest, so
            # pass through whatever the backend reported.
            self._emit(
                model=model,
                status="error",
                latency_ms=int((time.perf_counter() - start) * 1000),
                error_class=type(exc).__name__,
                usage=_normalize_usage((data or {}).get("usage", {})),
                node=node,
                scenario_id=scenario_id,
            )
            raise
        self._emit(
            model=model,
            status="ok",
            latency_ms=int((time.perf_counter() - start) * 1000),
            usage=_normalize_usage(data.get("usage", {})),
            node=node,
            scenario_id=scenario_id,
        )
        return content

    def chat_json(
        self,
        messages: list[dict[str, str]],
        *,
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        scenario_id: str | None = None,
    ) -> Any:
        """Like chat() but parses the response as JSON.

        OMLX may not reliably support response_format=json_object,
        so we try it first and fall back to extracting JSON from text.

        Callers must not assume a container type. ``json.loads`` can
        return an object, an array, a scalar, or None. Use
        :meth:`chat_json_object` or :meth:`chat_json_list` when the
        caller needs one of those shapes.

        Note: no outer @retry — chat() already retries transient failures.
        """
        model = model or self.settings.llm_model
        max_tokens = max_tokens or self.settings.llm_max_tokens
        text = None
        if not self._json_mode_broken:
            try:
                candidate = self.chat(
                    messages,
                    model=model,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    response_format={"type": "json_object"},
                    scenario_id=scenario_id,
                )
                if self._degenerate_json(candidate):
                    # This backend mishandles json_object mode — remember
                    # and don't pay for the extra call again this session.
                    self._json_mode_broken = True
                    logger.info(
                        "%s json_object mode returned a degenerate response; using plain mode",
                        self._backend,
                    )
                else:
                    text = candidate
            except LLMTruncatedError:
                # Must precede the LLMError branch, which this subclasses.
                # The fallback below re-sends the SAME budget with only
                # response_format dropped, so a ceiling that just truncated
                # would truncate again — and _retryable's no-retry guarantee
                # would be defeated one frame above where it is enforced.
                # Falling through here costs a second full-budget generation
                # to reach an identical failure.
                raise
            except LLMError:
                pass  # plain call below
        if text is None:
            # Fallback: no response_format, extract JSON from text
            text = self.chat(
                messages,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
                scenario_id=scenario_id,
            )

        # Parse JSON — handle ```json fences
        text = text.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            lines = [l for l in lines if not l.startswith("```")]
            text = "\n".join(lines)
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            # Extract the first parseable JSON structure embedded in prose.
            # raw_decode is a real parser, so braces inside strings, nested
            # arrays, and full top-level arrays are all handled correctly.
            decoder = json.JSONDecoder()
            for i, c in enumerate(text):
                if c not in "{[":
                    continue
                try:
                    obj, _ = decoder.raw_decode(text, i)
                except json.JSONDecodeError:
                    continue
                # Salvage succeeded, which is not the same as success. When the
                # response is a cut-off object the outer brace never closes, so
                # the first structure that parses is an *inner* one — an array
                # of the enclosing object's values, say, which reaches the
                # caller looking like a healthy answer and fails later as a
                # schema mismatch. Name it here so the log points at the
                # response rather than at whatever validates it next.
                logger.warning(
                    "%s response was not valid JSON; recovered a %s fragment at "
                    "offset %d of %d chars. If the response was truncated this "
                    "value is incomplete.",
                    self._backend, type(obj).__name__, i, len(text),
                )
                return obj
            raise LLMError(f"Cannot parse JSON from LLM response: {text[:200]}") from e

    def chat_json_object(
        self,
        messages: list[dict[str, str]],
        *,
        keys: tuple[str, ...] = (),
        **kw: Any,
    ) -> dict[str, Any]:
        """``chat_json``, guaranteed to return a dict.

        A list, scalar, null, or unparseable response becomes ``{}``.
        ``keys`` names the fields the caller intends to read. They are
        not filtered out of a valid object; a missing key is the
        caller's own ``.get``. This does not retry the model.
        """
        del keys  # documented intent, not a filter
        try:
            payload = self.chat_json(messages, **kw)
        except (LLMError, ValueError, TypeError):
            return {}
        if isinstance(payload, dict):
            return payload
        return {}

    def chat_json_list(
        self,
        messages: list[dict[str, str]],
        **kw: Any,
    ) -> list[Any]:
        """``chat_json``, guaranteed to return a list.

        A bare array is returned as-is. An object returns its first
        list-valued field, or ``[]`` when it has none. Anything else,
        including a parse failure, returns ``[]``. This does not retry
        the model.
        """
        try:
            payload = self.chat_json(messages, **kw)
        except (LLMError, ValueError, TypeError):
            return []
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            for value in payload.values():
                if isinstance(value, list):
                    return value
        return []

    @staticmethod
    def _degenerate_json(text: str) -> bool:
        """Detect broken json_object-mode responses: `{}` or a request echo.

        Some OMLX builds return an empty object or serialize the request
        itself (`{"messages": [...]}`) when given response_format=json_object.
        Such responses are never legitimate domain answers here.
        """
        stripped = text.strip()
        if stripped.startswith("```"):
            lines = stripped.split("\n")
            lines = [l for l in lines if not l.startswith("```")]
            stripped = "\n".join(lines).strip()
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError:
            return False  # unparseable → the normal extraction path will handle it
        if obj == {}:
            return True
        return (
            isinstance(obj, dict)
            and set(obj) == {"messages"}
            and isinstance(obj["messages"], list)
        )

    def responses_supported(self) -> tuple[bool, str]:
        """Probe whether this endpoint exposes POST /v1/responses.

        GET is enough: a real Responses route answers 405/401/403/422, while a
        missing route answers 404. No completion is issued.
        """
        try:
            resp = self.client.get("/responses")
            code = resp.status_code
        except httpx.HTTPError as exc:
            return False, f"GET /responses failed: {exc}"
        if code == 404:
            return False, "GET /responses returned 404"
        if code in {200, 400, 401, 403, 405, 422}:
            return True, f"GET /responses returned {code}"
        return False, f"GET /responses returned {code}"

    def list_models(self) -> list[str]:
        """List available models from OMLX."""
        try:
            resp = self.client.get("/models")
            resp.raise_for_status()
            data = resp.json()
            return [m["id"] for m in data.get("data", [])]
        except (httpx.HTTPError, KeyError) as e:
            raise LLMError(f"Failed to list models: {e}") from e

    def is_available(self) -> bool:
        """Check if OMLX is reachable."""
        try:
            self.list_models()
            return True
        except LLMError:
            return False
