"""Source quality and pilot metrics must expose missingness and uncertainty."""

import json
import sqlite3

import pytest

from workbench_atlas.config import Settings
from workbench_atlas.diagnostics import assess_pilot, evidence_quality, main


@pytest.fixture
def settings(tmp_path):
    evidence = tmp_path / "evidence.db"
    with sqlite3.connect(evidence) as conn:
        conn.execute("CREATE TABLE evidence_events(event_id TEXT, source_publisher TEXT, source_url TEXT, "
                     "source_date TEXT, retrieved_at TEXT, content_fingerprint TEXT, review_status TEXT, "
                     "reviewed_at TEXT, region TEXT)")
        conn.executemany("INSERT INTO evidence_events VALUES(?,?,?,?,?,?,?,?,?)", [
            ("E1", "Fictional Wire", "https://wire.example/report", "2020-01-01", "2026-09-01", "same-content", "approved", "2020-01-01", "Europe"),
            ("E2", "Fictional Republisher", "https://copy.example/report", "2026-08-01", "2026-08-02", "same-content", "approved", None, "Europe"),
            ("E3", "fictional wire", "https://wire.example/other", "bad-date", "2026-08-03", "different", "pending", None, ""),
        ])
    scenarios = tmp_path / "scenarios.db"
    with sqlite3.connect(scenarios) as conn:
        conn.execute("CREATE TABLE scenarios(scenario_id TEXT, evidence_refs TEXT, evidence_strength TEXT)")
        conn.execute("INSERT INTO scenarios VALUES(?,?,?)", ("S1", '["E1", "E3", "missing"]', "strong"))
    risks = tmp_path / "risks.json"
    risks.write_text(json.dumps([{"risk_id": "R1", "evidence_refs": ["https://wire.example/report"],
                                  "review_status": "approved", "evidence_strength": "limited"}]))
    return Settings(evidence_db_path=evidence, scenario_db_path=scenarios, emerging_risks_path=risks,
                    context_db_path=tmp_path / "private.db", context_enabled=True)


def test_diagnostics_read_only_and_do_not_open_organization_context(settings):
    settings.context_db_path.write_text("private marker that is not a database")
    paths = [settings.evidence_db_path, settings.scenario_db_path, settings.emerging_risks_path, settings.context_db_path]
    before = [path.read_bytes() for path in paths]
    report = evidence_quality(settings, as_of="2026-09-08")
    assert report["status"] == "available"
    assert report["summary"]["evidence_records"] == 3
    assert [path.read_bytes() for path in paths] == before
    assert "private marker" not in json.dumps(report)


def test_republished_content_does_not_become_independent_confirmation(settings):
    report = evidence_quality(settings, as_of="2026-09-08")
    assert report["independence"]["status"] == "not_validated"
    assert report["independence"]["publishers"]["distinct_labels"] == 2
    assert report["independence"]["domains"]["largest_share_of_known"] == pytest.approx(2 / 3)
    group = report["duplicates"]["fingerprint_groups"][0]
    assert group["event_ids"] == ["E1", "E2"]
    assert report["citation_breadth"]["records"][1]["citation_breadth"] == "strong"
    assert "not independent corroboration" in report["citation_breadth"]["interpretation"]


def test_publication_retrieval_and_event_recency_are_distinct(settings):
    report = evidence_quality(settings, as_of="2026-09-08")
    publication = report["recency"]["publication"]["records"][0]
    retrieval = report["recency"]["retrieval"]["records"][0]
    assert publication["status"] == "older_than_threshold"
    assert retrieval["status"] == "within_threshold"
    assert report["recency"]["event"]["status"] == "not_recorded"
    assert report["recency"]["event"]["counts"]["missing"] == 3
    assert report["recency"]["publication"]["counts"]["invalid"] == 1


def test_regions_require_explicit_declared_scope(settings):
    no_scope = evidence_quality(settings, as_of="2026-09-08")["region_coverage"]
    assert no_scope["scope_status"] == "not_declared" and no_scope["missing_declared_regions"] == []
    declared = evidence_quality(settings, as_of="2026-09-08", declared_regions=["Europe", "Africa"])["region_coverage"]
    assert declared["missing_declared_regions"] == ["africa"]
    assert declared["missing_records"] == 1


def test_stale_approvals_and_broken_references_are_actionable(settings):
    report = evidence_quality(settings, as_of="2026-09-08")
    stale = {r["record_id"]: r["reason"] for r in report["stale_approvals"]["records"]}
    assert stale == {"E1": "older_than_threshold", "E2": "missing_review_date", "R1": "missing_review_date"}
    broken = {r["reference"]: r["status"] for r in report["broken_refs"]["records"]}
    assert broken == {"E3": "unapproved_evidence", "missing": "missing_evidence"}
    assert report["broken_refs"]["url_reachability"] == "not_checked"


def test_missing_and_corrupt_stores_do_not_report_validated_empty_coverage(settings):
    settings.evidence_db_path.write_text("not a database")
    report = evidence_quality(settings, as_of="2026-09-08")
    assert report["availability"]["evidence"] == "unreadable_store"
    assert report["status"] == "partial"
    assert all(r["status"] == "not_checked_evidence_unavailable" for r in report["broken_refs"]["records"])
    settings.evidence_db_path.unlink()
    report = evidence_quality(settings, as_of="2026-09-08")
    assert report["availability"]["evidence"] == "missing_store"
    assert not settings.evidence_db_path.exists()


def _rating(arm="atlas", candidate="candidate-one", reviewer="reviewer-one", **values):
    return {"case_id": "case-one", "candidate_id": candidate, "arm": arm,
            "reviewer_id": reviewer, "blinded": True, **values}


def test_empty_pilot_and_missing_ratings_remain_not_validated():
    report = assess_pilot([])
    assert report["validation_status"] == "not_validated"
    for arm in report["arms"].values():
        assert arm["useful_novel_exposure_rate"]["value"] is None
        assert arm["calibration"]["brier"]["status"] == "not_validated"
    report = assess_pilot([_rating()])
    assert report["arms"]["atlas"]["actionability_rate"]["missing_or_tied_candidates"] == 1


def test_matched_blinded_comparisons_use_case_units_and_unique_times():
    rows = []
    for arm in ("baseline", "atlas"):
        for reviewer in ("one", "two"):
            rows.append(_rating(arm, reviewer=reviewer, useful=True, novel=arm == "atlas",
                                actionable=arm == "atlas", duplicate=False,
                                time_to_brief_minutes=60 if arm == "baseline" else 30))
    report = assess_pilot(rows)
    comparison = report["comparisons"]["useful_novel_exposure_rate"]
    assert comparison["mean_paired_case_delta_atlas_minus_baseline"] == 1
    assert comparison["matched_resolved_cases"] == 1
    assert report["comparisons"]["time_to_brief"]["median_paired_minutes_saved"] == 30
    assert report["arms"]["atlas"]["time_to_brief"]["case_count"] == 1
    assert report["reviewer_agreement"]["useful"]["pairwise_agreement"] == 1
    assert report["validation_status"] == "not_validated"


def test_unblinded_or_unpaired_data_cannot_produce_blinded_comparison():
    rows = [_rating("baseline", useful=True, novel=False),
            _rating("atlas", useful=True, novel=True, blinded=False)]
    report = assess_pilot(rows)
    assert report["comparisons"]["useful_novel_exposure_rate"]["status"] == "not_validated"
    rows[1]["blinded"] = True
    rows[1]["case_id"] = "other-case"
    assert assess_pilot(rows)["design"]["matched_cases"] == []


def test_tied_ratings_are_missing_not_automatic_rejections_or_acceptances():
    report = assess_pilot([_rating(reviewer="one", useful=True, novel=True),
                           _rating(reviewer="two", useful=True, novel=False)])
    metric = report["arms"]["atlas"]["useful_novel_exposure_rate"]
    assert metric["value"] is None and metric["missing_or_tied_candidates"] == 1
    assert report["reviewer_agreement"]["novel"]["pairwise_agreement"] == 0


@pytest.mark.parametrize("change", [{"forecast_probability": float("nan")},
                                     {"forecast_probability": 1.1}, {"useful": "yes"},
                                     {"prediction_interval": [10, 5]}, {"time_to_brief_minutes": -1}])
def test_invalid_pilot_measurements_are_rejected(change):
    with pytest.raises(ValueError):
        assess_pilot([_rating(**change)])


def test_duplicate_ratings_and_conflicting_candidate_facts_are_rejected():
    row = _rating(useful=True)
    with pytest.raises(ValueError, match="duplicate rating"):
        assess_pilot([row, row])
    with pytest.raises(ValueError, match="consistent"):
        assess_pilot([_rating(reviewer="one", time_to_brief_minutes=1),
                      _rating(reviewer="two", time_to_brief_minutes=2)])
    with pytest.raises(ValueError, match="conflicting"):
        assess_pilot([_rating(reviewer="one", forecast_probability=0.1),
                      _rating(reviewer="two", forecast_probability=0.2)])


def test_brier_requires_actual_sourced_outcomes_after_forecast_and_counts_candidates_once():
    facts = {"forecast_probability": 0.8, "forecast_at": "2025-01-01", "outcome": 1,
             "outcome_at": "2025-02-01", "outcome_status": "observed", "outcome_source": "operator-controlled outcome ledger",
             "target": "defined operational event"}
    report = assess_pilot([_rating(reviewer="one", **facts), _rating(reviewer="two", **facts)], as_of="2026-01-01")
    metric = report["arms"]["atlas"]["calibration"]["brier"]["by_target"][0]
    assert metric["n_outcomes"] == 1 and metric["value"] == pytest.approx(0.04)
    for override in ({"outcome_status": "synthetic"}, {"outcome_source": None},
                     {"forecast_at": "2025-03-01"}, {"outcome_at": "2027-01-01"}):
        report = assess_pilot([_rating(**{**facts, **override})], as_of="2026-01-01")
        assert report["arms"]["atlas"]["calibration"]["brier"]["status"] == "not_validated"


def test_numeric_interval_coverage_is_not_probability_confidence_coverage():
    record = _rating(forecast_at="2025-01-01", outcome_at="2025-02-01", outcome_status="observed",
                     target="recovery time", prediction_interval=[1, 3], interval_level=0.9,
                     observed_value=2, units="hours", observed_value_source="measured exercise log")
    report = assess_pilot([record], as_of="2026-01-01")
    calibration = report["arms"]["atlas"]["calibration"]
    assert calibration["brier"]["status"] == "not_validated"
    assert calibration["numeric_prediction_interval_coverage"]["groups"][0]["empirical_coverage"] == 1
    assert "unknown true probability" in calibration["numeric_prediction_interval_coverage"]["interpretation"]


def test_pilot_cli_reads_supplied_observations_without_claiming_a_pass(tmp_path, capsys):
    path = tmp_path / "pilot.json"
    path.write_text(json.dumps([_rating(useful=True, novel=True)]))
    assert main(["pilot", "--input", str(path)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["validation_status"] == "not_validated"
    assert report["arms"]["atlas"]["useful_novel_exposure_rate"]["value"] == 1
