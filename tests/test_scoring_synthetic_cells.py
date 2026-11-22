"""Synthetic scoring-cell generation, aggregation, and egress checks."""

import json
import math

import pytest

from workbench_atlas.scoring.cli import eval_main, score_main
from workbench_atlas.scoring.egress import EgressGateway
from workbench_atlas.scoring.synthetic_aggregation import (
    SyntheticAggregationReport,
    build_synthetic_features,
    prepare_synthetic_events,
    scoring_group_id,
    severity_from_quantiles,
    write_synthetic_aggregation,
)
from workbench_atlas.scoring.synthetic_events import (
    SCORING_CELL_CODES,
    SEGMENT_CODES,
    SyntheticEventConfig,
    generate_synthetic_events,
    write_synthetic_events_csv,
)


@pytest.fixture()
def generated(tmp_path):
    records, truth = generate_synthetic_events(SyntheticEventConfig())
    csv_path = write_synthetic_events_csv(
        records, tmp_path / "synthetic_events.csv"
    )
    return records, truth, csv_path


def test_generator_is_deterministic():
    first_records, first_truth = generate_synthetic_events(SyntheticEventConfig())
    second_records, second_truth = generate_synthetic_events(SyntheticEventConfig())
    assert first_records == second_records
    assert first_truth == second_truth


def test_generator_uses_neutral_project_native_contract(generated):
    records, truth, _ = generated
    assert any(row["event_id"].startswith("DEMO-EVENT-INVALID") for row in records)
    assert any("narrative" in row for row in records)
    assert len(truth["planted_edges"]) == 30
    assert truth["regime"] == "synthetic-cell-demo"
    assert all(code.startswith("DEMO-CELL-") for code in SCORING_CELL_CODES)
    assert all(code.startswith("DEMO-SEGMENT-") for code in SEGMENT_CODES)
    assert all(atom["risk_id"].startswith("demo-risk-") for atom in truth["atoms"])


def test_preparation_rejects_invalid_rows_and_strips_prose(generated):
    records, _truth, _ = generated
    accepted, report = prepare_synthetic_events(records)
    assert report.n_generated == len(records)
    assert report.n_rejected == 3
    assert report.n_accepted == report.n_generated - 3
    reasons = " ".join(row["reason"] for row in report.rejections)
    assert "missing required field" in reasons
    assert "unparseable date" in reasons
    assert "non-positive impact_amount" in reasons
    assert "narrative" in report.text_columns_dropped
    assert all("narrative" not in row for row in accepted)


def test_scoring_group_id_is_stable_and_segment_sensitive():
    first = scoring_group_id("DEMO-CELL-IDENTITY", "DEMO-SEGMENT-01")
    assert first == scoring_group_id("DEMO-CELL-IDENTITY", "DEMO-SEGMENT-01")
    assert first != scoring_group_id("DEMO-CELL-IDENTITY", "DEMO-SEGMENT-02")
    assert first.startswith("demo-group-")


def test_sparse_group_suppression_uses_neutral_fields():
    records = [
        {
            "event_id": f"event-{index}",
            "scoring_cell_code": "DEMO-CELL-IDENTITY",
            "segment_code": "DEMO-SEGMENT-01",
            "date": "2024-02-15",
            "impact_amount": 100.0,
        }
        for index in range(6)
    ] + [
        {
            "event_id": "event-sparse",
            "scoring_cell_code": "DEMO-CELL-EXTERNAL",
            "segment_code": "DEMO-SEGMENT-02",
            "date": "2024-02-15",
            "impact_amount": 50.0,
        }
    ]
    report = SyntheticAggregationReport()
    features, events = build_synthetic_features(
        records, report, min_group_size=5
    )
    assert len(features) == 1
    assert features[0]["count"] == 6
    assert features[0]["segment_code"] == "DEMO-SEGMENT-01"
    assert features[0]["plane"] == "aggregate"
    assert report.n_suppressed_groups == 1
    assert report.n_suppressed_events == 1
    assert len(events) == 7
    assert all(event["plane"] == "local_detail" for event in events)


def test_aggregation_writes_demo_artifacts_without_prose(generated, tmp_path):
    records, _truth, _ = generated
    result = write_synthetic_aggregation(records, tmp_path / "out")
    features_text = result["features"].read_text()
    assert "narrative" not in features_text
    assert "@example.invalid" not in features_text
    assert result["features"].name == "scoring_features.jsonl"
    assert result["local_events"].name == "synthetic_scoring_events.jsonl"
    assert result["report"].name == "aggregation_report.json"
    report = result["aggregation_report"]
    assert report["n_accepted"] > 0
    assert report["n_suppressed_groups"] >= 0
    assert all(
        json.loads(line)["plane"] == "aggregate"
        for line in features_text.splitlines()
    )
    assert all(
        json.loads(line)["plane"] == "local_detail"
        for line in result["local_events"].read_text().splitlines()
    )


def test_severity_from_quantiles():
    estimate = severity_from_quantiles({"0.5": 100_000.0, "0.9": 400_000.0})
    assert estimate["mu"] == pytest.approx(math.log(100_000.0))
    assert estimate["sigma"] > 0
    fallback = severity_from_quantiles({"0.5": 100.0, "0.9": 100.0})
    assert fallback["sigma"] == 1.0


def test_egress_allows_features_and_denies_event_rows(generated, tmp_path):
    records, _truth, _ = generated
    result = write_synthetic_aggregation(records, tmp_path / "out")
    gateway = EgressGateway(tmp_path / "audit.jsonl")
    decisions = [
        gateway.validate(json.loads(line), "scoring_features", caller="test")
        for line in result["features"].read_text().splitlines()
    ]
    assert decisions and all(decisions)
    event = json.loads(result["local_events"].read_text().splitlines()[0])
    assert not gateway.validate(event, "synthetic_scoring_events", caller="test")
    bad = json.loads(result["features"].read_text().splitlines()[0])
    bad["narrative"] = "synthetic local-detail text"
    assert not gateway.validate(bad, "scoring_features", caller="test")
    wrong_plane = json.loads(result["features"].read_text().splitlines()[0])
    wrong_plane["plane"] = "local_detail"
    decision = gateway.validate(wrong_plane, "scoring_features", caller="test")
    assert not decision
    assert "plane_not_egressible:local_detail" in decision.reasons


def test_synthetic_cell_demo_end_to_end(tmp_path):
    output = tmp_path / "demo"
    result = eval_main(
        ["synthetic-cell-demo", "--out", str(output), "--mc-draws", "500"]
    )
    assert result == 0
    report = json.loads((output / "eval_report.json").read_text())
    assert report["data_regime"] == "synthetic-cell-demo"
    assert report["synthetic_aggregation"]["n_rejected"] == 3
    egress = report["egress_check"]
    assert egress["features_allowed"] > 0
    assert egress["features_denied"] == 0
    assert egress["local_events_denied_sample"] == 20
    assert (output / "synthetic_events.csv").exists()
    assert (output / "aggregation_report.json").exists()


def test_score_help_exposes_only_generation_and_pair_scoring(capsys):
    with pytest.raises(SystemExit) as raised:
        score_main(["--help"])
    assert raised.value.code == 0
    help_text = capsys.readouterr().out
    assert "r0-generate" in help_text
    assert "pairs" in help_text
    assert "--extract" not in help_text
