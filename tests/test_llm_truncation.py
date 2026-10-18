"""A completion cut off at max_tokens must fail loudly and exactly once.

The bug this pins down: a hardcoded ``max_tokens=2048`` truncated real
documents, and nothing anywhere inspected ``finish_reason``. The truncated
text then reached :meth:`OMLXClient.chat_json`, whose salvage scanner walks
the string for the first parseable structure — on a cut-off object the outer
brace never closes, so it recovered the *inner* array and returned it as if
it were the answer. The caller saw a well-formed list where it wanted an
object, reported a schema mismatch, and burned a second attempt on a failure
that could never succeed. Thirty-three minutes of local inference produced an
empty evidence box with no warning.

So the contract has three parts, one test each: truncation raises, it is not
retried, and salvage announces itself.
"""

from __future__ import annotations

import logging

import httpx
import pytest

from contingency_atlas.llm import (
    LLMError,
    LLMTruncatedError,
    OMLXClient,
    _retryable,
)


def _client_with(handler):
    client = OMLXClient()
    client._client = httpx.Client(
        base_url="http://mock",
        headers={"Authorization": "Bearer mock"},
        transport=httpx.MockTransport(handler),
        timeout=1.0,
    )
    return client


#: The shape the real failure had: an object cut off mid-key, whose inner
#: array is itself perfectly parseable — which is what the salvage scanner
#: recovered and handed back as though it were the answer.
TRUNCATED_BODY = '{"records": [{"text": "first"}, {"text": "second"}], "mod'


def test_truncated_completion_raises_rather_than_returning_a_fragment():
    def handler(request):
        return httpx.Response(200, json={
            "choices": [{"message": {"content": TRUNCATED_BODY},
                         "finish_reason": "length"}],
        })

    client = _client_with(handler)
    with pytest.raises(LLMTruncatedError) as exc:
        client.chat([{"role": "user", "content": "hi"}], max_tokens=2048)
    # The message must name the budget, because that is the thing to change.
    assert "2048" in str(exc.value)


def test_truncation_is_not_retried():
    """Same budget, same truncation — three attempts cost triple for nothing."""
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(200, json={
            "choices": [{"message": {"content": TRUNCATED_BODY},
                         "finish_reason": "length"}],
        })

    client = _client_with(handler)
    with pytest.raises(LLMTruncatedError):
        client.chat([{"role": "user", "content": "hi"}])
    assert len(calls) == 1, f"truncation was retried {len(calls)} times"


def test_transport_faults_are_still_retryable():
    """Guard the narrowing: only truncation loses its retry, nothing else."""
    assert _retryable(LLMTruncatedError("cut off")) is False
    assert _retryable(LLMError("backend down")) is True
    assert _retryable(httpx.ConnectError("refused")) is True
    assert _retryable(ValueError("unrelated")) is False


def test_complete_response_is_unaffected():
    def handler(request):
        return httpx.Response(200, json={
            "choices": [{"message": {"content": '{"ok": true}'},
                         "finish_reason": "stop"}],
        })

    assert _client_with(handler).chat_json([{"role": "user", "content": "hi"}]) == {"ok": True}


def test_missing_finish_reason_is_not_treated_as_truncation():
    """Backends that omit the field must not start failing."""
    def handler(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    assert _client_with(handler).chat([{"role": "user", "content": "hi"}]) == "ok"


def test_chat_json_does_not_pay_for_the_truncation_twice():
    """The no-retry guarantee has to survive chat_json's json-mode fallback.

    LLMTruncatedError subclasses LLMError, and chat_json wrapped its
    json_object attempt in `except LLMError: pass` before re-sending the same
    request in plain mode with the SAME budget. That defeated _retryable one
    frame above where it is enforced: tenacity was stopped from retrying, and
    the fallback did the retry anyway — a second full-budget generation to
    reach an identical failure.
    """
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(200, json={
            "choices": [{"message": {"content": TRUNCATED_BODY},
                         "finish_reason": "length"}],
        })

    client = _client_with(handler)
    with pytest.raises(LLMTruncatedError):
        client.chat_json([{"role": "user", "content": "hi"}], max_tokens=2048)
    assert len(calls) == 1, (
        f"truncation cost {len(calls)} generations; the json-mode fallback "
        "re-sent a budget that had just proven too small"
    )


def test_non_truncation_llm_errors_still_fall_back_to_plain_mode():
    """Guard the narrowing: only truncation skips the fallback."""
    seen = []

    def handler(request):
        import json as _json
        body = _json.loads(request.content)
        seen.append(body.get("response_format"))
        if body.get("response_format"):
            return httpx.Response(500, text="json mode unsupported")
        return httpx.Response(200, json={
            "choices": [{"message": {"content": '{"ok": true}'}, "finish_reason": "stop"}],
        })

    client = _client_with(handler)
    assert client.chat_json([{"role": "user", "content": "hi"}]) == {"ok": True}
    assert seen[0] == {"type": "json_object"} and seen[-1] is None


def test_truncated_call_is_billed_for_the_tokens_it_burned(tmp_path):
    """A call that generated the whole budget must not be booked at zero.

    Truncation is the most expensive failure there is, and the usage block is
    already parsed when it raises — recording zeros would make the ledger
    understate cost precisely where it is highest.
    """
    from contingency_atlas.scoring.telemetry import TelemetryLogger

    def handler(request):
        return httpx.Response(200, json={
            "choices": [{"message": {"content": TRUNCATED_BODY},
                         "finish_reason": "length"}],
            "usage": {"prompt_tokens": 900, "completion_tokens": 2048},
        })

    logger = TelemetryLogger(tmp_path / "t.jsonl", run_id="t")
    client = OMLXClient(telemetry=logger, node="ingest_convert")
    client._client = httpx.Client(
        base_url="http://mock", headers={"Authorization": "Bearer mock"},
        transport=httpx.MockTransport(handler), timeout=1.0,
    )
    with pytest.raises(LLMTruncatedError):
        client.chat([{"role": "user", "content": "hi"}], max_tokens=2048)

    event = logger.load_events()[0]
    assert event["status"] == "error"
    assert event["error_class"] == "LLMTruncatedError"
    assert event["input_tokens"] == 900
    assert event["output_tokens"] == 2048


def test_the_judge_does_not_share_the_extractor_budget():
    """Two calls with different appetites must not share one ceiling.

    The extractor sees one 6000-char chunk (~1.8k tokens observed) and answers
    about that chunk. The judge sees the artifact window plus the whole
    extraction (~20k-27k observed) and must enumerate every missed source — an
    output that grows with the source list. Sharing the extractor's 4096 made
    the judge the only call site in the codebase with negative headroom: it
    truncated in both real sessions on record, at exactly 4096 tokens.

    No upper bound is asserted. It depends on the backend's generation rate, so
    any threshold here would encode one machine's model — and the earlier
    attempt to bound it by the request timeout was simply wrong: httpx applies
    its timeout per socket read, not to total generation.
    """
    import inspect

    from contingency_atlas.ingest_artifacts import (
        EXTRACT_MAX_TOKENS,
        RUBRIC_MAX_TOKENS,
        _extract_chunk,
        rubric_check,
    )

    # A 6000-char chunk was measured needing ~2540 tokens to close the object;
    # at or below that, real documents truncate.
    assert EXTRACT_MAX_TOKENS > 2540, "below the tokens a real chunk was measured to need"
    assert RUBRIC_MAX_TOKENS > EXTRACT_MAX_TOKENS, (
        "the judge's output scales with the source list while the extractor's "
        "scales with one chunk — it cannot be the smaller budget"
    )

    # Each must pass an explicit ceiling: deferring to the configured limit is
    # what let an unbounded generation run for four minutes.
    for fn, const in ((_extract_chunk, "EXTRACT_MAX_TOKENS"), (rubric_check, "RUBRIC_MAX_TOKENS")):
        assert const in inspect.getsource(fn), (
            f"{fn.__name__} does not pass {const}"
        )


def test_salvaged_fragment_is_announced(caplog):
    """Recovering JSON from prose is a degraded outcome, not a silent success."""
    def handler(request):
        return httpx.Response(200, json={
            "choices": [{"message": {"content": 'Here you go: {"a": 1} — hope that helps'},
                         "finish_reason": "stop"}],
        })

    client = _client_with(handler)
    with caplog.at_level(logging.WARNING):
        assert client.chat_json([{"role": "user", "content": "hi"}]) == {"a": 1}
    assert any("recovered" in r.message.lower() or "recovered" in r.getMessage().lower()
               for r in caplog.records), "salvage happened without a warning"
