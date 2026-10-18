"""The ontology guard, actually firing.

I exercise the fail-closed two-gate guard across its machine-producer write
seams because `tests/conftest.py` sets ATLAS_GUARD=off for
the whole suite and CI installs only `.[dev]`, so a green run proved nothing
about whether the gate opens, closes, or is even reachable.

These tests turn it on. They skip cleanly when the ontology bundle or its
dependencies are absent, which is the same shape the rest of the suite uses
for optional components — a skip says "not verified here", where the default
ATLAS_GUARD=off said "verified" while checking nothing.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from contingency_atlas.guard_hook import GuardError, GuardRejected

ROOT = Path(__file__).resolve().parents[1]

pytest.importorskip("rdflib", reason="ontology extra not installed")
pytest.importorskip("pyshacl", reason="ontology extra not installed")

if not (ROOT / "ontology" / "guard.py").exists():  # pragma: no cover
    pytest.skip("ontology bundle absent", allow_module_level=True)


@pytest.fixture
def guard_on(monkeypatch):
    monkeypatch.setenv("ATLAS_GUARD", "on")


def test_enabled_guard_fails_closed_when_bundle_is_missing(
    guard_on,
    monkeypatch,
    tmp_path,
):
    import contingency_atlas.guard_hook as hook

    monkeypatch.setattr(hook, "GUARD_SCRIPT", tmp_path / "missing-guard.py")
    with pytest.raises(GuardError, match="installed bundle is incomplete"):
        hook.validate([hook.risk_record({"issue": "synthetic risk"})], seam="test")


def test_synthetic_bundle_fixture_matches_expected_without_runtime_store(tmp_path):
    candidates = ROOT / "ontology" / "fixtures" / "candidates.json"
    expected = json.loads(
        (ROOT / "ontology" / "fixtures" / "expected.json").read_text()
    )
    output = tmp_path / "guard-output"
    process = subprocess.run(
        [
            sys.executable,
            str(ROOT / "ontology" / "guard.py"),
            str(candidates),
            "--out",
            str(output),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert process.returncode == 1  # deliberately rejected fixtures are present
    actual = {
        key: value["verdict"]
        for key, value in json.loads(
            (output / "verdicts.json").read_text()
        ).items()
    }
    assert actual == expected


# ── the adapter, which the ontology fixtures never exercise ──────────────
#
# ontology/fixtures/candidates.json hand-builds records already carrying the
# right discriminator, so the bundle selftest validates the GUARD and never
# the builders that feed it. A channel proposal shipped broken through that
# gap: its domain "kind" (edge_add) overwrote the door's own discriminator and
# every proposal was refused as an unknown record type.


@pytest.mark.parametrize(
    ("builder", "row", "expected"),
    [
        ("risk_record", {"issue": "x", "risk_id": "x"}, "risk"),
        ("evidence_event_record", {"event_id": "EVT-1"}, "evidence_event"),
        ("scenario_record", {"scenario_id": "S-1"}, "scenario"),
        (
            "channel_proposal_record",
            {"proposal_id": "cp-1", "kind": "edge_add"},
            "channel_proposal",
        ),
        (
            "mapping_proposal_record",
            {"proposal_id": "map-1", "kind": "rogue"},
            "mapping_proposal",
        ),
    ],
)
def test_builders_set_the_door_discriminator(builder, row, expected):
    import contingency_atlas.guard_hook as hook

    assert getattr(hook, builder)(row)["kind"] == expected


@pytest.mark.parametrize(
    ("builder", "row"),
    [
        ("risk_record", {"issue": "x", "risk_id": "x"}),
        ("evidence_event_record", {"event_id": "EVT-1"}),
        ("scenario_record", {"scenario_id": "S-1"}),
        ("channel_proposal_record", {"proposal_id": "cp-1"}),
        ("mapping_proposal_record", {"proposal_id": "map-1"}),
    ],
)
def test_a_producer_column_named_kind_cannot_shadow_the_discriminator(builder, row):
    """The exact failure mode: a source row carrying its own `kind` column."""
    import contingency_atlas.guard_hook as hook

    record = getattr(hook, builder)({**row, "kind": "rogue"})
    assert record["kind"] != "rogue"


def test_channel_proposal_carries_its_domain_kind_separately():
    from contingency_atlas.guard_hook import channel_proposal_record

    record = channel_proposal_record({"proposal_id": "cp-1", "kind": "edge_add"})
    assert record["kind"] == "channel_proposal"
    assert record["proposal_kind"] == "edge_add"


def _mapping_proposal(**overrides):
    record = {
        "proposal_id": "map-guard-pending",
        "framework": "mitre-atlas",
        "scenario_id": "SCN-AI-001",
        "content_version": "2026.07",
        "technique_id": "AML.T0043",
        "applicability": "hypothesized",
        "origin": "manual",
        "status": "pending",
    }
    record.update(overrides)
    return record


def test_mapping_proposals_reuse_both_c09_ledger_invariants(guard_on):
    from contingency_atlas.guard_hook import mapping_proposal_record, validate

    candidates = [
        _mapping_proposal(),
        _mapping_proposal(
            proposal_id="map-guard-applied",
            graph_version_applied=1,
        ),
        _mapping_proposal(
            proposal_id="map-guard-unreviewed",
            status="approved",
        ),
        _mapping_proposal(
            proposal_id="map-guard-reviewed",
            status="rejected",
            reviewer="alice",
            reason="I found the scenario-to-technique rationale insufficient.",
        ),
    ]
    report = validate(
        [mapping_proposal_record(row) for row in candidates],
        seam="test_mapping_proposal_c09",
    )

    assert report.verdicts == {
        "mapping_proposal:map-guard-pending": "ADMIT",
        "mapping_proposal:map-guard-applied": "REJECT",
        "mapping_proposal:map-guard-unreviewed": "REJECT",
        "mapping_proposal:map-guard-reviewed": "ADMIT",
    }


def _risk(**over):
    record = {
        "issue": "Guard seam fixture risk",
        "risk_id": "guard-seam-fixture-risk",
        "description": "synthetic",
        "status": "active",
        "review_status": "pending",
        "evidence_refs": ["EVT-GUARDSEAM-0001"],
        "first_seen": "2026-08-05T00:00:00+00:00",
    }
    record.update(over)
    return record


def test_a_clean_register_save_is_admitted(guard_on, tmp_path):
    from contingency_atlas.phases.phase2_risks import save_emerging_risks

    register = tmp_path / "emerging_risks.json"
    save_emerging_risks(register, [_risk()])
    assert json.loads(register.read_text())[0]["risk_id"] == "guard-seam-fixture-risk"


def test_a_rejected_batch_leaves_the_previous_register_byte_identical(
    guard_on, tmp_path
):
    """Fail-closed is only a property if the prior state actually survives.

    C-10: an approved risk whose evidence_refs carry a bare URL instead of a
    canonical event id is a REJECT — the same witness the bundle's own
    fixtures use.
    """
    from contingency_atlas.phases.phase2_risks import save_emerging_risks

    register = tmp_path / "emerging_risks.json"
    save_emerging_risks(register, [_risk()])
    before = register.read_bytes()

    bad = _risk(
        issue="Guard seam fixture — uncanonicalized approval",
        risk_id="guard-seam-fixture-uncanonicalized",
        review_status="approved",
        evidence_refs=["https://example.org/research/paper"],
    )
    with pytest.raises(GuardRejected):
        save_emerging_risks(register, [_risk(), bad])

    assert register.read_bytes() == before, (
        "a rejected batch must leave the previous register untouched"
    )


def test_a_channel_proposal_survives_the_guarded_seam(guard_on, tmp_path):
    """End-to-end at the seam that broke: create_channel_proposals drops
    guard-rejected rows rather than raising, so a discriminator bug here is
    silent — proposals simply stop existing, and the only symptom is a log
    line. Assert the row actually lands.
    """
    from contingency_atlas.proposals import (
        create_channel_proposals,
        list_proposals,
        migrate_proposals,
    )
    from contingency_atlas.schema import migrate

    db = str(tmp_path / "atlas.db")
    migrate(db)
    migrate_proposals(db)

    stored = create_channel_proposals(db, [{
        "kind": "edge_add",
        "risk_ids": ["risk-alpha", "risk-beta"],
        "channel": "",
        "mechanism": "a stated transmission mechanism",
        "citation_url": "",
        "citation_status": "unverified",
    }])

    assert stored["created"] == 1, "the guard refused a well-formed proposal"
    rows = list_proposals(db, kind="channel")
    assert [(r["kind"], r["status"]) for r in rows] == [("edge_add", "pending")]
