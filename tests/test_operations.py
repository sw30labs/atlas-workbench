"""Durable jobs and non-destructive backups, using only temporary stores."""

import json
import sqlite3
import threading
from pathlib import Path
from urllib.parse import urlunsplit

import pytest

from contingency_atlas.config import Settings
from contingency_atlas.operations import (
    JobJournal, OperationsError, create_backup, exclusive_file_lock,
    job_journal_path, readiness, restore_backup, settings_fingerprint,
    verify_backup, worker_lock_path,
)
from contingency_atlas.web import runner


@pytest.fixture
def operational_settings(tmp_path, monkeypatch):
    settings = Settings(
        scenario_db_path=tmp_path / "custom-scenarios.db",
        evidence_db_path=tmp_path / "custom-evidence.db",
        emerging_risks_path=tmp_path / "custom-risks.json",
        context_db_path=tmp_path / "custom-context.db",
        omlx_api_key="synthetic-secret-never-back-up",
        # Entirely fictional negative fixture: backup must strip URL credentials.
        omlx_base_url=urlunsplit(("http", ":".join(("synthetic-user", "synthetic-password"))
                                 + "@127.0.0.1:8000", "/v1", "token=synthetic-query-secret", "")),
    )
    for path, table in ((settings.scenario_db_path, "scenarios"),
                        (settings.evidence_db_path, "evidence_events")):
        with sqlite3.connect(path) as connection:
            connection.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY, content TEXT)")
            connection.execute(f"INSERT INTO {table}(content) VALUES ('Synthetic committed record')")
    settings.emerging_risks_path.write_text('[{"issue":"Synthetic risk"}]')
    monkeypatch.setattr(runner, "get_settings", lambda: settings)
    yield settings
    runner.wait_for_jobs(timeout=10)


def test_journal_persists_named_inputs_but_public_reads_hide_text(operational_settings, monkeypatch):
    monkeypatch.setattr(runner, "_ensure_worker", lambda *_args: None)
    job_id, error = runner.start_job("phase1", {
        "actor": "Synthetic Operator", "evidence_texts": "Synthetic private input",
        "source_urls": "https://example.org/public", "api_key": "must-not-persist",
    })
    assert error is None
    journal = JobJournal(job_journal_path(operational_settings))
    restored = journal.get(job_id, include_params=True)
    assert restored["params"] == {"evidence_texts": "Synthetic private input", "source_urls": "https://example.org/public"}
    public = runner.get_job(job_id)
    assert public["state"] == "queued" and public["status"] == "running"
    assert "Synthetic private input" not in json.dumps(public)
    assert "must-not-persist" not in json.dumps(restored)
    assert job_journal_path(operational_settings).stat().st_mode & 0o077 == 0


def test_queue_is_bounded_and_cancelled_jobs_free_capacity(tmp_path):
    journal = JobJournal(tmp_path / "jobs.db")
    first = journal.enqueue("phase4", {}, actor="Operator", settings_hash="x", max_queue=2)
    journal.enqueue("phase4", {}, actor="Operator", settings_hash="x", max_queue=2)
    with pytest.raises(OperationsError, match="queue is full"):
        journal.enqueue("phase4", {}, actor="Operator", settings_hash="x", max_queue=2)
    cancelled = journal.cancel(first, actor="Other Operator")
    assert cancelled["state"] == "cancelled"
    assert "Other Operator" in cancelled["error"]
    journal.enqueue("phase4", {}, actor="Operator", settings_hash="x", max_queue=2)


def test_recovery_interrupts_abandoned_work_without_replaying_it(operational_settings, monkeypatch):
    journal = JobJournal(job_journal_path(operational_settings))
    old = journal.enqueue("phase4", {}, actor="Operator", settings_hash=settings_fingerprint(operational_settings))
    assert journal.claim()["id"] == old
    queued = journal.enqueue("phase5", {}, actor="Operator", settings_hash=settings_fingerprint(operational_settings))
    calls = []
    monkeypatch.setattr(runner, "execute_phase", lambda phase, params: (calls.append(phase) or {"processed": 1}))
    result = runner.recover_jobs(resume_queued=False)
    assert result["interrupted"] == 1
    assert runner.get_job(old)["state"] == "interrupted"
    assert runner.get_job(queued)["state"] == "queued"
    runner.recover_jobs()
    runner.wait_for_jobs(timeout=5)
    assert calls == ["phase5"]
    assert runner.get_job(queued)["status"] == "done"


def test_recovery_does_not_interrupt_a_live_worker(operational_settings):
    journal = JobJournal(job_journal_path(operational_settings))
    job = journal.enqueue("phase4", {}, actor="Operator", settings_hash=settings_fingerprint(operational_settings))
    journal.claim()
    with exclusive_file_lock(worker_lock_path(operational_settings)):
        assert runner.recover_jobs()["worker_active"] is True
    assert runner.get_job(job)["state"] == "running"


def test_running_jobs_are_not_unsafely_cancelled(operational_settings):
    journal = JobJournal(job_journal_path(operational_settings))
    job = journal.enqueue("phase4", {}, actor="Operator", settings_hash=settings_fingerprint(operational_settings))
    journal.claim()
    with pytest.raises(OperationsError, match="cannot be safely preempted"):
        runner.cancel_job(job, actor="Operator")
    assert runner.get_job(job)["state"] == "running"


def test_dispatcher_executes_one_job_at_a_time(operational_settings, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls = []
    def execute(phase, params):
        calls.append(phase)
        if len(calls) == 1:
            entered.set()
            assert release.wait(timeout=5)
        return {"processed": 1}
    monkeypatch.setattr(runner, "execute_phase", execute)
    first, error = runner.start_job("phase4", {})
    assert error is None and entered.wait(timeout=5)
    second, error = runner.start_job("phase5", {})
    assert error is None
    assert runner.get_job(second)["state"] == "queued"
    release.set()
    runner.wait_for_jobs(timeout=5)
    assert calls == ["phase4", "phase5"]
    assert runner.get_job(first)["state"] == runner.get_job(second)["state"] == "completed"


def test_reported_errors_are_failed_and_explicit_retry_is_linked(operational_settings, monkeypatch):
    monkeypatch.setattr(runner, "execute_phase", lambda *_args: {"processed": 1, "errors": ["Synthetic failure"]})
    first, error = runner.start_job("phase4", {"top_n": 3})
    assert error is None
    runner.wait_for_jobs(timeout=5)
    failed = runner.get_job(first)
    assert failed["state"] == "failed" and failed["result"]["processed"] == 1
    seen = []
    monkeypatch.setattr(runner, "execute_phase", lambda phase, params: (seen.append(params) or {"processed": 2}))
    second, error = runner.retry_job(first, actor="Retry Operator")
    assert error is None
    runner.wait_for_jobs(timeout=5)
    assert second != first and runner.get_job(second)["retry_of"] == first
    assert runner.get_job(second)["actor"] == "Retry Operator"
    assert seen == [{"top_n": 3}]


def test_config_change_requires_operator_retry(operational_settings, monkeypatch):
    journal = JobJournal(job_journal_path(operational_settings))
    job = journal.enqueue("phase4", {}, actor="Operator", settings_hash="old-settings")
    def forbidden_execute(*_args):
        pytest.fail("changed configuration must not be applied to an old submission")
    monkeypatch.setattr(runner, "execute_phase", forbidden_execute)
    runner.recover_jobs()
    runner.wait_for_jobs(timeout=5)
    assert runner.get_job(job)["state"] == "interrupted"
    assert "Configuration changed" in runner.get_job(job)["error"]


def test_backup_restore_includes_committed_wal_and_configured_custom_stores(operational_settings, tmp_path):
    source = sqlite3.connect(operational_settings.scenario_db_path)
    try:
        source.execute("PRAGMA journal_mode=WAL")
        source.execute("PRAGMA wal_autocheckpoint=0")
        source.execute("INSERT INTO scenarios(content) VALUES ('Synthetic WAL record')")
        source.commit()
        assert Path(str(operational_settings.scenario_db_path) + "-wal").stat().st_size > 0
        result = create_backup(tmp_path / "bundle", actor="Backup Operator", settings=operational_settings)
    finally:
        source.close()
    assert len(result["files"]) == 4
    assert verify_backup(tmp_path / "bundle")["verified"] is True
    restored = restore_backup(tmp_path / "bundle", tmp_path / "restored", actor="Restore Operator", confirmed=True)
    assert restored["live_stores_modified"] is False
    with sqlite3.connect(tmp_path / "restored" / "atlas.db") as connection:
        assert connection.execute("SELECT COUNT(*) FROM scenarios").fetchone()[0] == 2
    assert (tmp_path / "restored" / "emerging_risks.json").read_bytes() == operational_settings.emerging_risks_path.read_bytes()
    configuration = (tmp_path / "bundle" / "configuration.json").read_text()
    for secret in ("synthetic-secret-never-back-up", "synthetic-password", "synthetic-query-secret"):
        assert secret not in configuration
    assert json.loads(configuration)["omlx_base_url"] == "http://127.0.0.1:8000/v1"


def test_restore_requires_actor_and_confirmation_and_preserves_existing_directory(operational_settings, tmp_path):
    create_backup(tmp_path / "bundle", actor="Operator", settings=operational_settings)
    with pytest.raises(OperationsError, match="confirmation"):
        restore_backup(tmp_path / "bundle", tmp_path / "new", actor="Operator")
    with pytest.raises(OperationsError, match="operator name"):
        restore_backup(tmp_path / "bundle", tmp_path / "new", actor="", confirmed=True)
    existing = tmp_path / "existing"
    existing.mkdir()
    (existing / "keep.txt").write_text("untouched")
    with pytest.raises(OperationsError, match="new directory"):
        restore_backup(tmp_path / "bundle", existing, actor="Operator", confirmed=True)
    assert (existing / "keep.txt").read_text() == "untouched"


def test_backup_restores_enabled_private_context_without_activating_it(operational_settings, tmp_path):
    settings = operational_settings.model_copy(update={
        "context_enabled": True, "context_db_path": tmp_path / "private-context.db",
    })
    with sqlite3.connect(settings.context_db_path) as connection:
        connection.execute("CREATE TABLE context_records (id TEXT)")
        connection.execute("INSERT INTO context_records VALUES ('Synthetic service')")
    with sqlite3.connect(settings.context_db_path.with_name("integration-outbox.db")) as connection:
        connection.execute("CREATE TABLE packages (id TEXT)")
        connection.execute("INSERT INTO packages VALUES ('Synthetic frozen package')")
    create_backup(tmp_path / "bundle", actor="Operator", settings=settings)
    restored = restore_backup(tmp_path / "bundle", tmp_path / "restored", actor="Operator", confirmed=True)
    assert restored["environment"]["ATLAS_CONTEXT_DB"] == str(tmp_path / "restored/organization-context.db")
    assert "ATLAS_CONTEXT_ENABLED" not in restored["environment"]
    with sqlite3.connect(restored["environment"]["ATLAS_CONTEXT_DB"]) as connection:
        assert connection.execute("SELECT id FROM context_records").fetchone()[0] == "Synthetic service"
    with sqlite3.connect(tmp_path / "restored/integration-outbox.db") as connection:
        assert connection.execute("SELECT id FROM packages").fetchone()[0] == "Synthetic frozen package"


def test_backup_preserves_journal_and_history_without_live_wal_sidecars(operational_settings, tmp_path):
    journal = JobJournal(job_journal_path(operational_settings))
    job = journal.enqueue("phase4", {"top_n": 3}, actor="Operator", settings_hash=settings_fingerprint(operational_settings))
    create_backup(tmp_path / "bundle", actor="Operator", settings=operational_settings)
    manifest = verify_backup(tmp_path / "bundle")["manifest"]
    assert any(entry["path"] == "generated/operations/jobs.sqlite3" for entry in manifest["files"])
    assert not list((tmp_path / "bundle").rglob("*-wal"))
    restored = restore_backup(tmp_path / "bundle", tmp_path / "restored", actor="Operator", confirmed=True)
    copied = JobJournal(Path(restored["destination"]) / "generated/operations/jobs.sqlite3")
    assert copied.get(job, include_params=True)["params"] == {"top_n": 3}


def test_restore_relocates_outbox_even_when_context_has_not_been_enabled(operational_settings, tmp_path):
    with sqlite3.connect(operational_settings.context_db_path.with_name("integration-outbox.db")) as connection:
        connection.execute("CREATE TABLE packages (id TEXT)")
    create_backup(tmp_path / "bundle", actor="Operator", settings=operational_settings)
    restored = restore_backup(tmp_path / "bundle", tmp_path / "restored", actor="Operator", confirmed=True)
    assert restored["environment"]["ATLAS_CONTEXT_DB"] == str(tmp_path / "restored/organization-context.db")
    assert not (tmp_path / "restored/organization-context.db").exists()
    assert (tmp_path / "restored/integration-outbox.db").is_file()


def test_checksum_failure_blocks_restore_before_destination_creation(operational_settings, tmp_path):
    create_backup(tmp_path / "bundle", actor="Operator", settings=operational_settings)
    (tmp_path / "bundle" / "emerging_risks.json").write_text("[]")
    with pytest.raises(OperationsError, match="checksum mismatch"):
        restore_backup(tmp_path / "bundle", tmp_path / "restored", actor="Operator", confirmed=True)
    assert not (tmp_path / "restored").exists()


def test_manifest_traversal_is_refused(operational_settings, tmp_path):
    create_backup(tmp_path / "bundle", actor="Operator", settings=operational_settings)
    path = tmp_path / "bundle" / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["files"][0]["path"] = "../outside.db"
    path.write_text(json.dumps(manifest))
    with pytest.raises(OperationsError, match="unexpected"):
        verify_backup(tmp_path / "bundle")


def test_backup_refuses_running_worker_and_leaves_no_partial_bundle(operational_settings, tmp_path):
    with exclusive_file_lock(worker_lock_path(operational_settings)):
        with pytest.raises(OperationsError, match="already running"):
            create_backup(tmp_path / "bundle", actor="Operator", settings=operational_settings)
    assert not (tmp_path / "bundle").exists()
    assert not list(tmp_path.glob(".atlas-backup-*"))


def test_readiness_is_read_only_and_reports_corrupt_journal(operational_settings):
    assert readiness(operational_settings)["ready_for_local_use"] is True
    path = job_journal_path(operational_settings)
    assert not path.exists()
    path.parent.mkdir(parents=True)
    path.write_text("not a SQLite database")
    result = readiness(operational_settings)
    assert result["ready_for_local_use"] is False
    assert any(check["name"] == "job_journal" and check["status"] == "unavailable" for check in result["checks"])
