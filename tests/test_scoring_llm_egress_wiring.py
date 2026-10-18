"""Remote scoring-lane prompts must pass the egress gateway first."""

from __future__ import annotations

import json

from contingency_atlas.compound_join_llm import propose_joins
from contingency_atlas.config import Settings
from contingency_atlas.llm import OMLXClient
from contingency_atlas.refutations import generate_refutation_memo
from contingency_atlas.scoring.egress import EgressGateway, verify_audit_chain
from contingency_atlas.scoring.elicitation import channel_audit, elicit_analogues
from contingency_atlas.scoring.telemetry import TelemetryLogger


def _remote_spy(monkeypatch, tmp_path, response):
    """Construct an explicitly opted-in remote client with no real transport."""
    monkeypatch.setenv("ATLAS_ALLOW_REMOTE_LLM", "1")
    telemetry = TelemetryLogger(tmp_path / "telemetry.jsonl", run_id="egress-test")
    client = OMLXClient(
        Settings(
            llm_provider="dgx",
            dgx_base_url="https://api.example.org/v1",
        ),
        telemetry=telemetry,
    )
    calls = []

    def chat_json(messages, **kwargs):
        calls.append((messages, kwargs))
        return response

    monkeypatch.setattr(client, "chat_json", chat_json)
    return client, calls, telemetry


def _audit_rows(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_remote_channel_audit_denies_local_detail_before_transport(
    monkeypatch, tmp_path
):
    client, calls, telemetry = _remote_spy(
        monkeypatch,
        tmp_path,
        {"missing_channels": [], "new_channels": [], "expert_edges": []},
    )
    audit = tmp_path / "channel-egress.jsonl"
    rows = channel_audit(
        [
            {
                "risk_id": "risk-a",
                "title": "Local synthetic detail",
                "channels": ["cloud"],
                "plane": "local_detail",
            }
        ],
        client,
        resolver=lambda _url: True,
        egress_gateway=EgressGateway(audit),
    )

    assert calls == []
    assert len(rows) == 1
    assert "plane_not_egressible:local_detail" in rows[0]["error"]
    assert _audit_rows(audit)[0]["schema"] == "channel_audit_request"
    assert verify_audit_chain(audit)
    assert telemetry.load_events()[0]["status"] == "denied_egress"


def test_remote_analogue_denies_scanned_value_before_transport(monkeypatch, tmp_path):
    client, calls, _telemetry = _remote_spy(
        monkeypatch, tmp_path, {"analogues": []}
    )
    audit = tmp_path / "analogue-egress.jsonl"
    rows = elicit_analogues(
        [
            {
                "pair_id": "pair-a-b",
                "risk_ids": ["risk-a", "risk-b"],
                "atom_titles": {
                    "risk-a": "Contact private.owner@example.invalid",
                    "risk-b": "Public risk B",
                },
                "plane": "public",
            }
        ],
        client,
        resolver=lambda _url: True,
        egress_gateway=EgressGateway(audit),
    )

    assert calls == []
    assert len(rows) == 1
    assert "email_like:atoms.0.title" in rows[0]["error"]
    assert _audit_rows(audit)[0]["allowed"] is False
    assert verify_audit_chain(audit)


def test_remote_refutation_denies_overlong_value_before_transport(
    monkeypatch, tmp_path
):
    client, calls, _telemetry = _remote_spy(monkeypatch, tmp_path, {})
    audit = tmp_path / "refutation-egress.jsonl"
    memo = generate_refutation_memo(
        {
            "compound_id": "scn-a-b",
            "pair_id": "pair-a-b",
            "risk_ids": ["risk-a", "risk-b"],
            "plane": "public",
        },
        {"risk-a": "x" * 241, "risk-b": "Public risk B"},
        client,
        egress_gateway=EgressGateway(audit),
    )

    assert calls == []
    assert "overlong_string:atoms.0.title" in memo["error"]
    assert _audit_rows(audit)[0]["schema"] == "refutation_memo_request"
    assert verify_audit_chain(audit)


def test_clean_remote_request_is_audited_then_calls_client(monkeypatch, tmp_path):
    client, calls, telemetry = _remote_spy(
        monkeypatch,
        tmp_path,
        {"missing_channels": [], "new_channels": [], "expert_edges": []},
    )
    audit = tmp_path / "allowed-egress.jsonl"
    rows = channel_audit(
        [{"risk_id": "risk-a", "title": "Public cloud risk", "channels": []}],
        client,
        resolver=lambda _url: True,
        egress_gateway=EgressGateway(audit),
    )

    assert rows == []
    assert len(calls) == 1
    assert _audit_rows(audit)[0]["allowed"] is True
    assert telemetry.load_events() == []


def test_remote_gateway_failure_still_cannot_reach_transport(monkeypatch, tmp_path):
    client, calls, telemetry = _remote_spy(
        monkeypatch,
        tmp_path,
        {"missing_channels": [], "new_channels": [], "expert_edges": []},
    )

    class BrokenGateway:
        def validate(self, payload, schema, *, caller):
            raise OSError("audit store unavailable")

    rows = channel_audit(
        [{"risk_id": "risk-a", "title": "Public risk", "channels": []}],
        client,
        resolver=lambda _url: True,
        egress_gateway=BrokenGateway(),
    )

    assert calls == []
    assert "egress preflight failed closed" in rows[0]["error"]
    assert telemetry.load_events()[0]["status"] == "denied_egress"


def test_remote_compound_join_denies_local_detail_before_transport(
    monkeypatch, tmp_path
):
    client, calls, telemetry = _remote_spy(monkeypatch, tmp_path, {"joins": []})
    audit = tmp_path / "compound-join-denied-egress.jsonl"
    result = propose_joins(
        [
            {
                "risk_id": "risk-a",
                "title": "Local synthetic detail",
                "channels": ["cloud"],
                "plane": "local_detail",
            },
            {
                "risk_id": "risk-b",
                "title": "Public model risk",
                "channels": ["model"],
                "plane": "public",
            },
        ],
        client,
        visible_pairs=set(),
        egress_gateway=EgressGateway(audit),
    )

    assert calls == []
    assert "plane_not_egressible:local_detail" in result["error"]
    assert _audit_rows(audit)[0]["schema"] == "compound_join_request"
    assert _audit_rows(audit)[0]["allowed"] is False
    assert verify_audit_chain(audit)
    assert telemetry.load_events()[0]["status"] == "denied_egress"


def test_clean_remote_compound_join_is_audited_then_calls_client(
    monkeypatch, tmp_path
):
    client, calls, telemetry = _remote_spy(monkeypatch, tmp_path, {"joins": []})
    audit = tmp_path / "compound-join-allowed-egress.jsonl"
    result = propose_joins(
        [
            {
                "risk_id": "risk-a",
                "title": "Public cloud risk",
                "channels": ["cloud"],
                "plane": "public",
            },
            {
                "risk_id": "risk-b",
                "title": "Public model risk",
                "channels": ["model"],
                "plane": "public",
            },
        ],
        client,
        visible_pairs=set(),
        egress_gateway=EgressGateway(audit),
    )

    assert result["error"] is None
    assert result["joins"] == []
    assert len(calls) == 1
    row = _audit_rows(audit)[0]
    assert row["schema"] == "compound_join_request"
    assert row["allowed"] is True
    assert verify_audit_chain(audit)
    assert telemetry.load_events() == []


def test_loopback_inference_bypasses_remote_egress_policy(monkeypatch, tmp_path):
    client = OMLXClient(Settings())
    calls = []

    def chat_json(messages, **kwargs):
        calls.append((messages, kwargs))
        return {"missing_channels": [], "new_channels": [], "expert_edges": []}

    monkeypatch.setattr(client, "chat_json", chat_json)
    audit = tmp_path / "local-egress.jsonl"
    rows = channel_audit(
        [
            {
                "risk_id": "risk-local",
                "title": "Local synthetic detail",
                "channels": [],
                "plane": "local_detail",
            }
        ],
        client,
        resolver=lambda _url: True,
        egress_gateway=EgressGateway(audit),
    )

    assert rows == []
    assert len(calls) == 1
    assert not audit.exists()
