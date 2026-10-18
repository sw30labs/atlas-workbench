"""Tests for the R0 synthetic generator."""

from collections import defaultdict

from contingency_atlas.scoring.channels import cooccurrence_counts, occurrence_series
from contingency_atlas.scoring.synthetic import (
    R0Config,
    aggregate_incident_features,
    generate_r0,
    load_r0_events,
    load_r0_risks,
)


def test_r0_deterministic_same_seed():
    a = generate_r0(R0Config(seed=7))
    b = generate_r0(R0Config(seed=7))
    assert [e.to_dict() for e in a.events] == [e.to_dict() for e in b.events]
    assert a.truth == b.truth


def test_r0_different_seed_differs():
    a = generate_r0(R0Config(seed=7))
    b = generate_r0(R0Config(seed=8))
    assert [e.to_dict() for e in a.events] != [e.to_dict() for e in b.events]


def test_r0_default_shape_and_truth():
    ds = generate_r0(R0Config())
    assert len(ds.atoms) == 15
    assert len(ds.truth["channels"]) == 3
    assert len(ds.truth["planted_edges"]) == 3 * (5 * 4 // 2)  # 30
    assert all(e.loss_amount > 0 for e in ds.events)
    assert all(0 <= e.period < 40 for e in ds.events)
    # atoms carry planted channel tags (ground truth lives in the manifest)
    for atom in ds.atoms:
        assert len(atom.channels) == 1


def test_r0_planted_channels_drive_cooccurrence():
    """Same-channel pairs must co-occur far more than cross-channel pairs —
    otherwise R0 is not a fair unit test of structure recovery."""
    ds = generate_r0(R0Config())
    series = occurrence_series(ds.events, 40)
    counts = cooccurrence_counts(series, [a.risk_id for a in ds.atoms])
    by_channel = {a.risk_id: a.channels[0] for a in ds.atoms}
    totals = defaultdict(lambda: [0, 0])
    ids = sorted(by_channel)
    for i, x in enumerate(ids):
        for y in ids[i + 1 :]:
            key = "same" if by_channel[x] == by_channel[y] else "cross"
            totals[key][0] += counts.get((x, y), 0)
            totals[key][1] += 1
    same_mean = totals["same"][0] / totals["same"][1]
    cross_mean = totals["cross"][0] / totals["cross"][1]
    assert same_mean >= 1.0
    assert same_mean > 3 * cross_mean


def test_aggregate_incident_features_matches_events():
    ds = generate_r0(R0Config())
    rows = aggregate_incident_features(ds.events)
    assert sum(r.count for r in rows) == len(ds.events)
    assert all(r.n_severity == r.count for r in rows)
    assert all(r.plane == "aggregate" for r in rows)


def test_r0_write_load_roundtrip(tmp_path):
    ds = generate_r0(R0Config(seed=11))
    paths = ds.write(tmp_path)
    atoms = load_r0_risks(paths["risks"])
    events = load_r0_events(paths["events"])
    assert [a.risk_id for a in atoms] == [a.risk_id for a in ds.atoms]
    assert [e.to_dict() for e in events] == [e.to_dict() for e in ds.events]
