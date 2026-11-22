"""Tests for the friendly `atlas` command line."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from workbench_atlas import cli
from workbench_atlas.config import Settings
from workbench_atlas.demo import DemoError, fixtures_dir, seed_into

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _restore_atlas_home():
    """`atlas` sets ATLAS_HOME for the process; never leak it into other tests."""
    before = os.environ.get("ATLAS_HOME")
    yield
    if before is None:
        os.environ.pop("ATLAS_HOME", None)
    else:
        os.environ["ATLAS_HOME"] = before


def run_cli(capsys, *argv: str) -> tuple[int, str]:
    code = cli.main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out + captured.err


def test_help_lists_every_command(capsys):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--help"])
    assert excinfo.value.code == 0
    output = capsys.readouterr().out
    for command in ("init", "demo", "up", "serve", "run", "status", "doctor",
                    "review", "export", "config", "clean"):
        assert command in output


def test_demo_seeds_a_fresh_home(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("ATLAS_HOME", raising=False)
    home = tmp_path / "data"
    code, output = run_cli(capsys, "demo", "--home", str(home))
    assert code == 0, output
    assert (home / "atlas.db").is_file()
    assert (home / "event_evidence.db").is_file()
    assert (home / "emerging_risks.json").is_file()
    assert (home / "scenario_export.json").is_file()
    # The demo is synthetic and must say so.
    assert "fictional" in output.lower()


def test_demo_is_idempotent(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("ATLAS_HOME", raising=False)
    home = tmp_path / "data"
    assert run_cli(capsys, "demo", "--home", str(home))[0] == 0
    code, output = run_cli(capsys, "demo", "--home", str(home))
    assert code == 0, output
    with sqlite3.connect(home / "event_evidence.db") as conn:
        count = conn.execute("SELECT COUNT(*) FROM evidence_events").fetchone()[0]
    assert count == 8  # no duplicate rows on a second seed


def test_status_reports_counts_as_json(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("ATLAS_HOME", raising=False)
    home = tmp_path / "data"
    assert run_cli(capsys, "demo", "--home", str(home))[0] == 0
    code, output = run_cli(capsys, "status", "--home", str(home), "--json")
    assert code == 0, output
    report = json.loads(output)
    assert report["initialized"] is True
    assert report["evidence"]["total"] == 8
    assert report["scenarios"]["total"] == 6
    assert report["risks"]["total"] == 6
    assert report["pending_review"]["evidence"] == 0


def test_status_on_empty_home_is_honest(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("ATLAS_HOME", raising=False)
    home = tmp_path / "empty"
    code, output = run_cli(capsys, "status", "--home", str(home), "--json")
    assert code == 0, output
    report = json.loads(output)
    assert report["initialized"] is False
    assert report["evidence"]["total"] == 0


def test_atlas_home_drives_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_HOME", str(tmp_path))
    for key in ("ATLAS_DB", "ATLAS_EVIDENCE_DB", "ATLAS_EMERGING_RISKS",
                "ATLAS_SCENARIO_EXPORT", "ATLAS_CONTEXT_DB", "ATLAS_WORKSPACE"):
        monkeypatch.delenv(key, raising=False)
    settings = Settings()
    assert settings.scenario_db_path == tmp_path / "atlas.db"
    assert settings.evidence_db_path == tmp_path / "event_evidence.db"
    assert settings.emerging_risks_path == tmp_path / "emerging_risks.json"
    assert settings.scenario_export_path == tmp_path / "scenario_export.json"
    assert settings.context_db_path == tmp_path / "organization-context.db"
    assert settings.workspace == tmp_path


def test_single_store_override_beats_atlas_home(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("ATLAS_DB", str(tmp_path / "custom.db"))
    settings = Settings()
    assert settings.scenario_db_path == tmp_path / "custom.db"
    assert settings.evidence_db_path == tmp_path / "home" / "event_evidence.db"


def test_explicit_argument_beats_atlas_home(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("ATLAS_DB", raising=False)
    settings = Settings(scenario_db_path=tmp_path / "explicit.db")
    assert settings.scenario_db_path == tmp_path / "explicit.db"


def test_home_config_file_is_used(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text("omlx:\n  model: from-home-config\n")
    monkeypatch.setenv("ATLAS_HOME", str(home))
    monkeypatch.delenv("OMLX_MODEL", raising=False)
    monkeypatch.delenv("ATLAS_CONFIG", raising=False)
    assert Settings().omlx_model == "from-home-config"


def test_config_command_redacts_secrets(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("ATLAS_HOME", raising=False)
    monkeypatch.setenv("OMLX_API_KEY", "super-secret-value")
    home = tmp_path / "data"
    code, output = run_cli(capsys, "config", "--home", str(home), "--json")
    assert code == 0, output
    payload = json.loads(output)
    assert payload["home"] == str(home.resolve())
    assert payload["scenario_db_path"] == str((home / "atlas.db").resolve())
    assert "super-secret-value" not in output


def test_doctor_runs_offline(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("ATLAS_HOME", raising=False)
    home = tmp_path / "data"
    assert run_cli(capsys, "demo", "--home", str(home))[0] == 0
    code, output = run_cli(capsys, "doctor", "--home", str(home), "--no-network", "--json")
    assert code == 0, output
    checks = {item["check"]: item["status"] for item in json.loads(output)}
    assert checks["python"] == "OK"
    assert checks["scenario store"] == "OK"
    assert checks["evidence store"] == "OK"
    assert checks["model endpoint"] == "SKIP"


@pytest.mark.parametrize(
    ("body", "generation", "judge", "status", "needle"),
    [
        (
            '{"data":[{"id":"served-model"}]}',
            "served-model",
            "served-model",
            "OK",
            "configured models present",
        ),
        (
            '{"models":["served-model"]}',
            "served-model",
            "",
            "OK",
            "1 served model",
        ),
        (
            '[{"id":"served-model"}]',
            "missing-model",
            "served-model",
            "WARN",
            "generation missing-model",
        ),
        (
            '{"data":[{"id":"served-model"}]}',
            "ThisModelDoesNotExist",
            "ThisModelDoesNotExist",
            "WARN",
            "ThisModelDoesNotExist",
        ),
        ("not json", "m", "m", "OK", "model list unreadable"),
        ('{"oops": 1}', "m", "m", "OK", "model list unreadable"),
        ("[1, null]", "m", "m", "WARN", "Served: (none)"),
    ],
)
def test_classify_served_models(body, generation, judge, status, needle):
    from workbench_atlas.cli.main import classify_served_models

    got_status, detail = classify_served_models(
        body, generation_model=generation, judge_model=judge
    )
    assert got_status == status
    assert needle in detail
    if status == "WARN":
        assert "Offline analysis still works." in detail


def test_doctor_warns_when_configured_model_is_not_served(tmp_path, capsys, monkeypatch):
    import httpx

    class _Response:
        status_code = 200
        text = '{"data":[{"id":"served-model"}]}'

    class _Client:
        def __init__(self, *args, **kwargs):
            del args, kwargs

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, headers=None):
            del url, headers
            return _Response()

    monkeypatch.delenv("ATLAS_HOME", raising=False)
    monkeypatch.setenv("OMLX_MODEL", "ThisModelDoesNotExist")
    monkeypatch.setenv("OMLX_MODEL_JUDGE", "ThisModelDoesNotExist")
    monkeypatch.setattr(httpx, "Client", _Client)
    home = tmp_path / "data"
    code, output = run_cli(capsys, "doctor", "--home", str(home), "--json")
    assert code == 0, output
    checks = {item["check"]: item for item in json.loads(output)}
    detail = checks["model endpoint"]["detail"]
    assert checks["model endpoint"]["status"] == "WARN"
    assert "ThisModelDoesNotExist" in detail
    assert "served-model" in detail


def test_clean_requires_confirmation(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("ATLAS_HOME", raising=False)
    home = tmp_path / "data"
    assert run_cli(capsys, "demo", "--home", str(home))[0] == 0
    code, _ = run_cli(capsys, "clean", "--home", str(home))
    assert code == 1
    assert (home / "atlas.db").is_file()

    code, _ = run_cli(capsys, "clean", "--home", str(home), "--yes")
    assert code == 0
    assert not (home / "atlas.db").exists()


def test_review_help_uses_the_shared_parser(capsys):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["review", "--help"])
    assert excinfo.value.code == 0
    output = capsys.readouterr().out
    assert "approve-event" in output
    assert "approve-signpost" in output


def test_run_offline_on_demo_data(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("ATLAS_HOME", raising=False)
    home = tmp_path / "data"
    assert run_cli(capsys, "demo", "--home", str(home))[0] == 0
    code, output = run_cli(capsys, "run", "--home", str(home), "--scenarios", "2")
    assert code == 0, output
    with sqlite3.connect(home / "atlas.db") as conn:
        runs = conn.execute("SELECT COUNT(*) FROM pipeline_runs").fetchone()[0]
    assert runs > 0


def test_run_without_data_explains_the_next_step(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("ATLAS_HOME", raising=False)
    code, output = run_cli(capsys, "run", "--home", str(tmp_path / "empty"))
    assert code == 1
    assert "atlas demo" in output


def test_demo_refuses_the_checkout_root():
    with pytest.raises(DemoError):
        seed_into(ROOT)


def test_packaged_fixtures_match_examples():
    """The wheel copy must not drift from the reviewed fixture corpus."""
    packaged = ROOT / "workbench_atlas" / "fixtures"
    for name in ("demo_evidence.json", "demo_emerging_risks.json", "demo_scenarios.json"):
        assert (packaged / name).read_bytes() == (ROOT / "examples" / name).read_bytes()


def test_fixtures_dir_prefers_examples(monkeypatch):
    monkeypatch.delenv("ATLAS_FIXTURES", raising=False)
    assert fixtures_dir() == ROOT / "examples"
