"""Egress gateway red-team tests (evaluation-plan.md section 7.4): synthetic
Local-detail strings must be denied; clean structural payloads must pass; the audit
chain must be tamper-evident."""

import json
import multiprocessing
from concurrent.futures import ThreadPoolExecutor

import pytest

from workbench_atlas.scoring.egress import (
    EgressGateway,
    verify_audit_chain,
)

CLEAN_PAYLOAD = {
    "scenario_id": "scn-0123abcd",
    "risk_ids": ["r0-identity-01", "r0-cloud-02"],
    "scoring_cell_codes": ["DEMO-CELL-TECHNOLOGY"],
    "channels": ["identity", "cloud"],
    "horizon": "1y",
    "p_hat": 0.12,
    "p_interval": [0.05, 0.22],
    "loss_quantiles": {"0.5": 120000.0, "0.95": 900000.0},
    "uncertainty_class": "mixed",
    "state_hash": "a" * 64,
}

CLEAN_SCORING_FEATURE = {
    "cell_id": "demo-group-a1b2c3d4",
    "scoring_cell_code": "DEMO-CELL-TECHNOLOGY",
    "segment_code": "DEMO-SEGMENT-01",
    "period": 4,
    "count": 12,
    "severity_quantiles": {"0.5": 100.0, "0.9": 500.0},
    "n_severity": 12,
    "plane": "aggregate",
    "source_system": "synthetic",
}

SYNTHETIC_LOCAL_DETAIL_NARRATIVE = (
    "On 2026-03-14 operator Jane Doe of the Frankfurt payments team filed "
    "incident INC-2026-0042 describing a sanctions-screening failure that "
    "led to a EUR 1.2m write-off; legal privilege applies. "
) * 3  # over 240 chars, narrative key, person name


def test_clean_payload_allowed(tmp_path):
    gw = EgressGateway(tmp_path / "audit.jsonl")
    decision = gw.validate(CLEAN_PAYLOAD, "scenario_structure", caller="test")
    assert decision.allowed, decision.reasons


def test_deny_email_like_token(tmp_path):
    gw = EgressGateway(tmp_path / "audit.jsonl")
    payload = dict(CLEAN_PAYLOAD, scenario_id="contact jane.doe@bank.example")
    decision = gw.validate(payload, "scenario_structure")
    assert not decision.allowed
    assert any("email_like" in r for r in decision.reasons)


def test_deny_account_like_digits(tmp_path):
    gw = EgressGateway(tmp_path / "audit.jsonl")
    payload = dict(CLEAN_PAYLOAD, horizon="acct 445566778899")
    decision = gw.validate(payload, "scenario_structure")
    assert not decision.allowed
    assert any("account_like" in r for r in decision.reasons)


def test_deny_local_detail_narrative_key_and_prose(tmp_path):
    gw = EgressGateway(tmp_path / "audit.jsonl")
    payload = dict(
        CLEAN_PAYLOAD,
        incident_narrative=SYNTHETIC_LOCAL_DETAIL_NARRATIVE,
    )
    decision = gw.validate(payload, "scenario_structure")
    assert not decision.allowed
    assert any("forbidden_key" in r for r in decision.reasons)
    assert any("key_not_in_allowlist" in r for r in decision.reasons)
    assert any("overlong_string" in r for r in decision.reasons)


def test_deny_nested_local_detail_in_list(tmp_path):
    gw = EgressGateway(tmp_path / "audit.jsonl")
    payload = dict(CLEAN_PAYLOAD, risk_ids=["r0-identity-01", {"narrative": "secret case file"}])
    decision = gw.validate(payload, "scenario_structure")
    assert not decision.allowed
    assert any("forbidden_key" in r for r in decision.reasons)


def test_deny_unknown_schema_and_extra_key(tmp_path):
    gw = EgressGateway(tmp_path / "audit.jsonl")
    assert not gw.validate(CLEAN_PAYLOAD, "no_such_schema").allowed
    payload = dict(CLEAN_PAYLOAD, surprise_field="x")
    decision = gw.validate(payload, "scenario_structure")
    assert not decision.allowed
    assert any("key_not_in_allowlist:surprise_field" in r for r in decision.reasons)


def test_schema_requires_every_declared_key(tmp_path):
    gw = EgressGateway(tmp_path / "audit.jsonl")
    assert gw.validate(CLEAN_SCORING_FEATURE, "scoring_features").allowed

    empty = gw.validate({}, "scoring_features")
    assert not empty.allowed
    assert "missing_required_key:plane" in empty.reasons

    missing_plane = dict(CLEAN_SCORING_FEATURE)
    missing_plane.pop("plane")
    decision = gw.validate(missing_plane, "scoring_features")
    assert not decision.allowed
    assert "missing_required_key:plane" in decision.reasons


@pytest.mark.parametrize("field", ["period", "count", "n_severity"])
def test_integer_fields_reject_booleans(tmp_path, field):
    gw = EgressGateway(tmp_path / "audit.jsonl")
    decision = gw.validate(
        {**CLEAN_SCORING_FEATURE, field: True},
        "scoring_features",
    )
    assert not decision.allowed
    assert f"type_mismatch:{field}" in decision.reasons


def test_audit_log_records_every_request_and_chains(tmp_path):
    audit = tmp_path / "audit.jsonl"
    gw = EgressGateway(audit)
    gw.validate(CLEAN_PAYLOAD, "scenario_structure", caller="node-a")
    gw.validate(
        dict(CLEAN_PAYLOAD, incident_text="local-detail"),
        "scenario_structure",
        caller="node-b",
    )
    lines = [json.loads(ln) for ln in audit.read_text().splitlines()]
    assert len(lines) == 2
    assert lines[0]["allowed"] is True
    assert lines[1]["allowed"] is False
    assert lines[0]["caller"] == "node-a"
    assert lines[1]["prev_hash"] == lines[0]["entry_hash"]
    assert verify_audit_chain(audit)


def test_audit_chain_tamper_evident(tmp_path):
    audit = tmp_path / "audit.jsonl"
    gw = EgressGateway(audit)
    gw.validate(CLEAN_PAYLOAD, "scenario_structure")
    gw.validate(CLEAN_PAYLOAD, "scenario_structure")
    lines = audit.read_text().splitlines()
    forged = json.loads(lines[0])
    forged["allowed"] = False  # attacker flips a decision
    lines[0] = json.dumps(forged, sort_keys=True)
    audit.write_text("\n".join(lines) + "\n")
    assert not verify_audit_chain(audit)


def test_independent_gateways_reread_current_tail(tmp_path):
    audit = tmp_path / "audit.jsonl"
    first, second = EgressGateway(audit), EgressGateway(audit)
    first.validate(CLEAN_PAYLOAD, "scenario_structure")
    second.validate(CLEAN_PAYLOAD, "scenario_structure")
    assert verify_audit_chain(audit)


def test_shared_gateway_serializes_concurrent_threads(tmp_path):
    audit = tmp_path / "audit.jsonl"
    gateway = EgressGateway(audit)
    with ThreadPoolExecutor(max_workers=4) as pool:
        decisions = list(pool.map(
            lambda index: gateway.validate(CLEAN_PAYLOAD, "scenario_structure", caller=str(index)),
            range(20),
        ))
    assert all(decision.allowed for decision in decisions)
    assert len(audit.read_text().splitlines()) == 20
    assert verify_audit_chain(audit)


def _append_audit_process(path, barrier):
    gateway = EgressGateway(path)
    barrier.wait(timeout=10)
    for index in range(10):
        gateway.validate(CLEAN_PAYLOAD, "scenario_structure", caller=str(index))


def test_gateways_serialize_separate_processes(tmp_path):
    audit = tmp_path / "audit.jsonl"
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(3)
    processes = [context.Process(target=_append_audit_process, args=(audit, barrier)) for _ in range(2)]
    try:
        for process in processes:
            process.start()
        barrier.wait(timeout=10)
        for process in processes:
            process.join(timeout=10)
            assert process.exitcode == 0
    finally:
        for process in processes:
            if process.is_alive():
                process.kill()
            process.join(timeout=2)
            process.close()
    assert len(audit.read_text().splitlines()) == 20
    assert verify_audit_chain(audit)


@pytest.mark.parametrize("damage", ["partial_json", "missing_newline", "changed_hash"])
def test_damaged_audit_fails_closed_without_appending(tmp_path, damage):
    audit = tmp_path / "audit.jsonl"
    gateway = EgressGateway(audit)
    gateway.validate(CLEAN_PAYLOAD, "scenario_structure")
    previous = audit.read_text()
    if damage == "partial_json":
        damaged = previous + '{"allowed":'
    elif damage == "missing_newline":
        damaged = previous.rstrip("\n")
    else:
        entry = json.loads(previous)
        entry["allowed"] = False
        damaged = json.dumps(entry) + "\n"
    audit.write_text(damaged)
    assert not verify_audit_chain(audit)
    with pytest.raises(ValueError, match="egress audit"):
        gateway.validate(CLEAN_PAYLOAD, "scenario_structure")
    assert audit.read_text() == damaged


# ---------------------------------------------------------------------------
# Wave 5 schemas (P1 channel audit / P2 analogue elicitation / P6 memo)
# ---------------------------------------------------------------------------

CLEAN_CHANNEL_AUDIT = {
    "risk_id": "cloud-region-failover-congestion",
    "title": "Cloud region failover congestion",
    "proposed_channels": ["cloud", "third-party"],
    "proposed_edges": [
        {"other_risk_id": "identity-plane-concentration", "channel": "cloud"}
    ],
    "mechanism": "shared failover capacity couples outage propagation",
    "citation_url": "https://example.com/postmortem/2024-06",
}

CLEAN_ANALOGUE_ELICITATION = {
    "pair_id": "pair-0123abcd5678",
    "risk_ids": ["cloud-region-failover-congestion", "identity-plane-concentration"],
    "horizon": "1y",
    "event": "Knight Capital deployment cascade",
    "period": "2012-08",
    "mechanism_map": {"channel": "model", "atom_1_role": "deployment pipeline"},
    "co_fired": True,
    "outcome_class": "q95-band-3",
    "citations": ["https://example.com/sec/knight-capital"],
    "disanalogies": ["single firm, not a shared utility"],
}

CLEAN_REFUTATION_MEMO = {
    "compound_id": "scn-0123abcd5678",
    "risk_ids": ["cloud-region-failover-congestion", "identity-plane-concentration"],
    "claims": ["shared channel explains the co-occurrence mechanically"],
    "citations": ["https://example.com/reference"],
}


@pytest.mark.parametrize(
    "schema,payload",
    [
        ("channel_audit", CLEAN_CHANNEL_AUDIT),
        ("analogue_elicitation", CLEAN_ANALOGUE_ELICITATION),
        ("refutation_memo", CLEAN_REFUTATION_MEMO),
    ],
)
def test_wave5_clean_payloads_allowed(tmp_path, schema, payload):
    gw = EgressGateway(tmp_path / "audit.jsonl")
    decision = gw.validate(payload, schema, caller="test")
    assert decision.allowed, decision.reasons


@pytest.mark.parametrize(
    "schema,payload",
    [
        ("channel_audit", CLEAN_CHANNEL_AUDIT),
        ("analogue_elicitation", CLEAN_ANALOGUE_ELICITATION),
        ("refutation_memo", CLEAN_REFUTATION_MEMO),
    ],
)
def test_wave5_deny_forbidden_key_parts(tmp_path, schema, payload):
    gw = EgressGateway(tmp_path / "audit.jsonl")
    for bad_key in ("incident_narrative", "case_description", "reviewer_notes"):
        decision = gw.validate(
            dict(payload, **{bad_key: "synthetic local-detail text"}), schema
        )
        assert not decision.allowed, (schema, bad_key)
        assert any("forbidden_key" in r for r in decision.reasons), decision.reasons
        assert any("key_not_in_allowlist" in r for r in decision.reasons), decision.reasons


@pytest.mark.parametrize(
    "schema,payload,key",
    [
        ("channel_audit", CLEAN_CHANNEL_AUDIT, "mechanism"),
        ("analogue_elicitation", CLEAN_ANALOGUE_ELICITATION, "event"),
        ("refutation_memo", CLEAN_REFUTATION_MEMO, "compound_id"),
    ],
)
def test_wave5_deny_overlong_strings(tmp_path, schema, payload, key):
    gw = EgressGateway(tmp_path / "audit.jsonl")
    decision = gw.validate(dict(payload, **{key: "x" * 241}), schema)
    assert not decision.allowed
    assert any("overlong_string" in r for r in decision.reasons), decision.reasons


def test_wave5_overlong_url_still_allowed(tmp_path):
    """URLs are exempt from the prose-length deny rule (structural refs)."""
    gw = EgressGateway(tmp_path / "audit.jsonl")
    payload = dict(CLEAN_CHANNEL_AUDIT, citation_url="https://example.com/" + "a" * 300)
    decision = gw.validate(payload, "channel_audit")
    assert decision.allowed, decision.reasons


@pytest.mark.parametrize(
    "schema,payload,key,value",
    [
        # Aggregate-boundary leakage: incident ids, account-like digit runs, emails
        ("channel_audit", CLEAN_CHANNEL_AUDIT, "title", "incident 445566778899 at bank"),
        ("analogue_elicitation", CLEAN_ANALOGUE_ELICITATION, "event",
         "contact jane.doe@bank.example for the file"),
        ("refutation_memo", CLEAN_REFUTATION_MEMO, "claims",
         ["root cause per incident 123456789012"]),
    ],
)
def test_wave5_deny_aggregate_boundary_leakage(
    tmp_path, schema, payload, key, value
):
    gw = EgressGateway(tmp_path / "audit.jsonl")
    decision = gw.validate(dict(payload, **{key: value}), schema)
    assert not decision.allowed
    assert any(
        ("account_like" in r) or ("email_like" in r) for r in decision.reasons
    ), decision.reasons


def test_wave5_deny_nested_aggregate_boundary_leakage(tmp_path):
    """The deny scan walks nested structures, not just top-level values."""
    gw = EgressGateway(tmp_path / "audit.jsonl")
    payload = dict(
        CLEAN_ANALOGUE_ELICITATION,
        mechanism_map={"channel": "model", "incident_ref": "INC-1"},
    )
    decision = gw.validate(payload, "analogue_elicitation")
    assert not decision.allowed
    assert any("forbidden_key" in r for r in decision.reasons), decision.reasons


@pytest.mark.parametrize(
    "schema,payload,key,value",
    [
        ("channel_audit", CLEAN_CHANNEL_AUDIT, "proposed_channels", "identity"),
        ("channel_audit", CLEAN_CHANNEL_AUDIT, "proposed_edges", {"a": "b"}),
        ("channel_audit", CLEAN_CHANNEL_AUDIT, "citation_url", 42),
        ("analogue_elicitation", CLEAN_ANALOGUE_ELICITATION, "co_fired", "yes"),
        ("analogue_elicitation", CLEAN_ANALOGUE_ELICITATION, "mechanism_map", ["model"]),
        ("analogue_elicitation", CLEAN_ANALOGUE_ELICITATION, "disanalogies", "none"),
        ("analogue_elicitation", CLEAN_ANALOGUE_ELICITATION, "co_fired", 1),
        ("refutation_memo", CLEAN_REFUTATION_MEMO, "claims", "a claim"),
        ("refutation_memo", CLEAN_REFUTATION_MEMO, "risk_ids", "r0-identity-01"),
    ],
)
def test_wave5_deny_wrong_types(tmp_path, schema, payload, key, value):
    gw = EgressGateway(tmp_path / "audit.jsonl")
    decision = gw.validate(dict(payload, **{key: value}), schema)
    assert not decision.allowed
    assert any(f"type_mismatch:{key}" == r for r in decision.reasons), decision.reasons
