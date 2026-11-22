"""Tests for scoring core: models, hashing, inventory, channel graph."""

import json
from pathlib import Path

import pytest

from workbench_atlas.scoring.channels import (
    build_channel_graph,
    cooccurrence_counts,
    occurrence_series,
    recover_empirical_edges,
)
from workbench_atlas.scoring.inventory import load_risk_atoms, tag_channels
from workbench_atlas.scoring.models import (
    IncidentEvent,
    RiskAtom,
    canonical_json,
    hash_object,
    scenario_state_hash,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_canonical_json_sorted_and_compact():
    assert canonical_json({"b": 1, "a": {"d": 2, "c": 3}}) == '{"a":{"c":3,"d":2},"b":1}'


def test_hash_object_order_independent():
    assert hash_object({"x": [1, 2], "y": {"b": 1, "a": 0}}) == hash_object(
        {"y": {"a": 0, "b": 1}, "x": [1, 2]}
    )


def test_state_hash_excludes_scores_and_timestamps():
    h1 = scenario_state_hash(["a", "b"], "1y", graph_hash="g1")
    h2 = scenario_state_hash(["b", "a"], "1y", graph_hash="g1")
    assert h1 == h2  # risk order canonicalized
    assert h1 != scenario_state_hash(["a", "b"], "1y", graph_hash="g2")
    assert h1 != scenario_state_hash(["a", "b", "c"], "1y", graph_hash="g1")


def test_load_risk_atoms_repo_file():
    # emerging_risks.json is a runtime artifact updated by pipeline runs
    # (gitignored — ADR-012): skip when absent; when present, assert
    # structure, never a frozen count.
    path = REPO_ROOT / "emerging_risks.json"
    if not path.exists():
        pytest.skip("emerging_risks.json is a gitignored runtime artifact")
    atoms = load_risk_atoms(path)
    assert atoms  # the register parses to at least one record
    ids = [a.risk_id for a in atoms]
    assert len(set(ids)) == len(ids)  # unique slugs
    known = {
        "identity", "cloud", "payments", "model", "credit-chain",
        "physical", "energy", "legal", "third-party", "geopolitical",
        "data",
    }
    with_channels = 0
    for atom in atoms:
        assert set(atom.channels) <= known
        if atom.channels:
            with_channels += 1
    assert with_channels >= len(atoms) - 1  # at most one untagged


def test_persisted_risk_id_survives_a_title_edit(tmp_path):
    """C-03: identity is the stored risk_id, not the current title.

    Recomputing the slug on every load meant renaming a risk moved its join
    key and orphaned every compound row pointing at the old one.
    """
    register = tmp_path / "emerging_risks.json"
    register.write_text(
        json.dumps(
            [
                {
                    "issue": "Coordinated identity-service concentration",
                    "risk_id": "coordinated-identity-service-concentration",
                    "description": "",
                }
            ]
        )
    )
    assert [a.risk_id for a in load_risk_atoms(register)] == [
        "coordinated-identity-service-concentration"
    ]

    edited = json.loads(register.read_text())
    edited[0]["issue"] = "Coordinated identity-service concentration (revised)"
    register.write_text(json.dumps(edited))
    atoms = load_risk_atoms(register)
    assert [a.risk_id for a in atoms] == ["coordinated-identity-service-concentration"]
    assert atoms[0].title == "Coordinated identity-service concentration (revised)"


def test_risk_id_falls_back_to_the_slug_when_absent(tmp_path):
    register = tmp_path / "emerging_risks.json"
    register.write_text(json.dumps([{"issue": "Settlement dependencies across cloud regions"}]))
    assert [a.risk_id for a in load_risk_atoms(register)] == [
        "settlement-dependencies-across-cloud-regions"
    ]


def test_tag_channels_keyword_rules():
    assert "identity" in tag_channels("Coordinated identity-service concentration")
    assert "cloud" in tag_channels("Settlement dependencies across cloud regions")
    assert "payments" in tag_channels("Settlement dependencies across cloud regions")
    assert tag_channels("unrelated gibberish") == []
    # AI-agent cyber entries often omit the word "model"
    assert "model" in tag_channels(
        "Automated Offense-Only Asymmetry in Vulnerability Discovery",
        "agent collectives discover and chain zero-days",
    )
    assert "model" in tag_channels(
        "Automated Finding Without Automated Remediation Inundation",
        "automated finding without automated remediation",
    )


def _atom(rid, channels):
    return RiskAtom(risk_id=rid, title=rid, channels=channels)


def test_channel_graph_shared_channel_edges_and_hash():
    atoms = [
        _atom("a", ["identity"]),
        _atom("b", ["identity", "cloud"]),
        _atom("c", ["cloud"]),
        _atom("d", ["payments"]),
    ]
    graph = build_channel_graph(atoms)
    pairs = set(graph.edge_pairs())
    assert ("a", "b") in pairs
    assert ("b", "c") in pairs
    assert ("a", "d") not in pairs
    assert graph.compute_hash() == graph.graph_hash
    # hash is stable regardless of edge insertion order
    graph2 = build_channel_graph(list(reversed(atoms)))
    assert graph2.graph_hash == graph.graph_hash


def test_empirical_edges_and_cooccurrence():
    events = [
        IncidentEvent("e1", "a", 0, 100.0),
        IncidentEvent("e2", "b", 0, 100.0),
        IncidentEvent("e3", "a", 1, 100.0),
        IncidentEvent("e4", "b", 1, 100.0),
        IncidentEvent("e5", "c", 3, 100.0),
    ]
    series = occurrence_series(events, 4)
    assert series["a"] == {0, 1}
    counts = cooccurrence_counts(series, ["a", "b", "c"])
    assert counts[("a", "b")] == 2
    assert ("a", "c") not in counts

    atoms = [_atom("a", ["x"]), _atom("b", ["y"]), _atom("c", ["z"])]
    graph = build_channel_graph(
        atoms, events=events, n_periods=4, empirical_tau=2,
        include_shared_channel=False, include_empirical=True,
    )
    assert graph.edge_pairs() == [("a", "b")]


def test_recover_empirical_edges_lift_prefers_coupled_pairs():
    # a-b co-occur in 3 of 6 periods while quiet; c-d are both busy but
    # co-occur exactly as often as independence would predict.
    events = []
    seq = 0
    for t in (0, 1, 2):
        for rid in ("a", "b"):
            seq += 1
            events.append(IncidentEvent(f"e{seq}", rid, t, 1.0))
    for t in range(6):
        seq += 1
        events.append(IncidentEvent(f"e{seq}", "c", t, 1.0))
        seq += 1
        events.append(IncidentEvent(f"e{seq}", "d", t, 1.0))
    # c and d co-occur every period (lift = 1 for identical busy series);
    # a,b co-occur half the periods with low marginals (lift = 2).
    # Raw-count ranking would put (c,d) first; lift must not.
    recovered = recover_empirical_edges(events, ["a", "b", "c", "d"], 6, top_n=2)
    assert recovered[0] == ("a", "b")
    assert ("c", "d") in recovered
