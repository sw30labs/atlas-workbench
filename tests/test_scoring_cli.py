"""End-to-end CLI tests: atlas-score and atlas-eval entry points."""

import json

from workbench_atlas.scoring.cli import eval_main, score_main


def test_r0_generate_then_eval_demo(tmp_path):
    out = tmp_path / "r0"
    assert eval_main(["demo", "--out", str(out), "--mc-draws", "500"]) == 0
    report = json.loads((out / "eval_report.json").read_text())
    assert report["recommendation"] == "go"
    assert report["task_b"]["pass"] is True
    assert report["task_c"]["pass"] is True
    assert (out / "r0_risks.json").exists()
    assert (out / "r0_events.jsonl").exists()
    assert (out / "r0_truth.json").exists()
    assert (out / "eval_report.md").exists()


def test_score_pairs_writes_realizations_and_telemetry(tmp_path):
    r0 = tmp_path / "r0"
    assert score_main(["r0-generate", "--out", str(r0)]) == 0
    out = tmp_path / "pairs"
    rc = score_main([
        "pairs",
        "--risks", str(r0 / "r0_risks.json"),
        "--events", str(r0 / "r0_events.jsonl"),
        "--out", str(out),
        "--mc-draws", "300",
    ])
    assert rc == 0
    lines = (out / "score_realizations.jsonl").read_text().splitlines()
    assert len(lines) == 15 * 14 // 2  # all pairs of 15 atoms
    first = json.loads(lines[0])
    assert first["components"]["mc"]["M"] == 300
    assert first["algorithm_version"]
    # one telemetry event per scored pair + run report present
    telemetry = (out / "telemetry.jsonl").read_text().splitlines()
    assert len(telemetry) == len(lines)
    run_report = json.loads((out / "run_report.json").read_text())
    assert run_report["n_events"] == len(lines)
    assert run_report["spend_usd"] == 0.0  # det-only path, no LLM cost


def test_eval_run_strict_exit_codes(tmp_path):
    r0 = tmp_path / "r0"
    assert score_main(["r0-generate", "--out", str(r0)]) == 0
    out = tmp_path / "eval"
    argv = [
        "run",
        "--risks", str(r0 / "r0_risks.json"),
        "--events", str(r0 / "r0_events.jsonl"),
        "--truth", str(r0 / "r0_truth.json"),
        "--out", str(out),
        "--mc-draws", "300",
        "--strict",
    ]
    assert eval_main(argv) == 0  # go on the pre-registered R0 dataset
    report = json.loads((out / "eval_report.json").read_text())
    assert report["splits"]["holdout_quarters"] == 8
    assert report["splits"]["train_quarters"] == [0, 31]
    assert report["splits"]["test_quarters"] == [32, 39]
