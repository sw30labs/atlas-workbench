"""Private comparison packs retain provenance and never widen implicit exports."""
import io
import hashlib
import json
import os
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from pypdf import PdfReader

from workbench_atlas import assessment_reports as reports
from workbench_atlas.config import Settings
from workbench_atlas.context import create_baseline, save_record
from workbench_atlas.exports import ExportError, _json_bytes, render_snapshot


@pytest.fixture
def assessment(tmp_path):
    settings = Settings(context_enabled=True, context_db_path=tmp_path / "organization.db",
                        scenario_db_path=tmp_path / "scenarios.db", evidence_db_path=tmp_path / "evidence.db")
    with sqlite3.connect(settings.scenario_db_path) as conn:
        conn.execute("CREATE TABLE scenarios(scenario_id TEXT PRIMARY KEY,title TEXT,status TEXT)")
        conn.execute("INSERT INTO scenarios VALUES('fictional-one','Synthetic outage','active')")
    record = save_record(settings, "services", {"name": "Synthetic service", "owner": "Synthetic owner",
                         "impact_tolerance": "Assumed two hours", "source": "Fictional workshop"}, actor="Fictional reviewer")
    baseline = create_baseline(settings, "Synthetic committee baseline", actor="Fictional reviewer")
    return settings, baseline, record


def test_explicit_private_opt_in_and_no_implicit_scope(assessment):
    settings, baseline, _ = assessment
    for include in (False, None, "true", 1):
        with pytest.raises(ExportError, match="explicitly"):
            reports.build_assessment_snapshot(settings, baseline_id=baseline["id"], include_context=include)


def test_frozen_comparison_renders_real_pdf_and_canonical_json(assessment):
    settings, baseline, record = assessment
    save_record(settings, "services", {"id": record["id"], "name": "Revised synthetic service", "owner": record["owner"],
                "impact_tolerance": record["impact_tolerance"], "source": record["source"]}, actor="Fictional reviewer", expected_revision=record["revision"])
    snapshot = reports.build_assessment_snapshot(settings, baseline_id=baseline["id"], include_context=True)
    assert snapshot["manifest"]["has_changes"]
    assert snapshot["manifest"]["classification"] == "includes_organization_context"
    assert snapshot["manifest"]["tables_sha256"] == hashlib.sha256(_json_bytes(snapshot["tables"])).hexdigest()
    assert snapshot["manifest"]["table_checksums"] == {
        key: hashlib.sha256(_json_bytes(rows)).hexdigest() for key, rows in snapshot["tables"].items()
    }
    changes = snapshot["tables"]["organization_changes"]
    assert any(r["field"] == "name" and r["before"] == "Synthetic service" for r in changes)
    pdf = render_snapshot(snapshot, format="pdf")
    text = "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf.content)).pages)
    assert "Synthetic committee baseline" in text and "Revised synthetic service" in text
    assert json.loads(render_snapshot(snapshot).content) == snapshot


def test_digest_cli_skips_verified_unchanged_and_repeated_changes(assessment, tmp_path, monkeypatch, capsys):
    settings, baseline, record = assessment
    monkeypatch.setattr(reports, "get_settings", lambda: settings)
    output = tmp_path / "private-reports"
    args = ["--baseline-id", baseline["id"], "--include-context", "--output-directory", str(output), "--format", "json", "--only-changes"]
    assert reports.main(args) == 0
    assert not output.exists()
    with sqlite3.connect(settings.scenario_db_path) as conn:
        conn.execute("UPDATE scenarios SET title='Changed fictional hypothesis'")
    assert reports.main(args) == 0
    files = list(output.glob("atlas-digest-*.json"))
    assert len(files) == 1 and files[0].stat().st_mode & 0o777 == 0o600
    receipt = reports._receipt_path(files[0])
    assert receipt.stat().st_mode & 0o777 == 0o600
    content = files[0].read_bytes()
    assert reports.main(args) == 0
    assert files[0].read_bytes() == content
    assert len(list(output.iterdir())) == 2
    assert "already been prepared" in capsys.readouterr().out


def test_incomplete_research_emits_report_instead_of_silent_no_change(assessment, tmp_path, monkeypatch):
    settings, baseline, _ = assessment
    settings.scenario_db_path.unlink()
    monkeypatch.setattr(reports, "get_settings", lambda: settings)
    output = tmp_path / "reports"
    assert reports.main(["--baseline-id", baseline["id"], "--include-context", "--output-directory", str(output), "--only-changes", "--format", "json"]) == 0
    snapshot = json.loads(next(output.glob("atlas-digest-*.json")).read_text())
    assert snapshot["manifest"]["research_status"] == "partial"
    assert "incomplete" in snapshot["tables"]["organization_review_notes"][-1]["note"]


def _publish(output, content=b"Synthetic complete report", *, allow_existing=True):
    return reports._publish_digest(output, content, change_digest="a" * 64,
                                   format="json", allow_existing=allow_existing)


@pytest.mark.parametrize("damage", [
    "truncated_report", "modified_report", "missing_report", "missing_receipt", "invalid_receipt",
    "wrong_digest", "wrong_format", "oversize_receipt", "report_directory", "receipt_directory",
    "report_symlink", "receipt_symlink", "dangling_report_symlink", "report_fifo",
])
def test_existing_digest_requires_complete_regular_files_and_valid_integrity(tmp_path, damage):
    output = tmp_path / "report.json"
    assert _publish(output) is True
    receipt = reports._receipt_path(output)
    if damage == "truncated_report":
        output.write_bytes(b"")
    elif damage == "modified_report":
        output.write_bytes(b"x" * output.stat().st_size)
    elif damage.startswith("missing_"):
        (output if damage == "missing_report" else receipt).unlink()
    elif damage == "invalid_receipt":
        receipt.write_bytes(b"invalid JSON")
    elif damage in {"wrong_digest", "wrong_format"}:
        data = json.loads(receipt.read_text())
        data["change_digest" if damage == "wrong_digest" else "format"] = "b" * 64 if damage == "wrong_digest" else "pdf"
        receipt.write_text(json.dumps(data))
    elif damage == "oversize_receipt":
        receipt.write_bytes(b"x" * (reports.MAX_RECEIPT_BYTES + 1))
    elif damage.endswith("directory"):
        target = output if damage == "report_directory" else receipt
        target.unlink()
        target.mkdir()
    elif damage == "report_fifo":
        output.unlink()
        os.mkfifo(output)
    else:
        target = receipt if damage == "receipt_symlink" else output
        underlying = tmp_path / "synthetic-target"
        if damage != "dangling_report_symlink":
            underlying.write_bytes(target.read_bytes())
        target.unlink()
        target.symlink_to(underlying)
    with pytest.raises(ExportError):
        _publish(output)
    assert list(tmp_path.glob(".atlas-digest-stage-*")) == []


def test_existing_verified_digest_is_not_overwritten_without_only_changes(tmp_path):
    output = tmp_path / "report.json"
    _publish(output)
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir()}
    assert _publish(output) is False
    with pytest.raises(ExportError, match="already exists"):
        _publish(output, allow_existing=False)
    assert {path.name: path.read_bytes() for path in tmp_path.iterdir()} == before


@pytest.mark.parametrize("failure", ["sync", "report_publish", "receipt_publish"])
def test_failed_staging_or_publication_cleans_only_new_files(tmp_path, monkeypatch, failure):
    output = tmp_path / "report.json"
    real_link = reports.os.link

    def broken_link(source, destination):
        if failure == "report_publish" or destination == reports._receipt_path(output):
            raise OSError("Synthetic publish failure")
        return real_link(source, destination)

    def broken_sync(descriptor):
        raise OSError("Synthetic sync failure")

    monkeypatch.setattr(reports.os, "fsync", broken_sync) if failure == "sync" else monkeypatch.setattr(reports.os, "link", broken_link)
    with pytest.raises(OSError):
        _publish(output)
    assert list(tmp_path.iterdir()) == []


def test_failure_cleanup_does_not_delete_a_concurrently_replaced_report(tmp_path, monkeypatch):
    output = tmp_path / "report.json"
    real_link = reports.os.link

    def substituted_output(source, destination):
        if destination == reports._receipt_path(output):
            output.unlink()
            output.write_bytes(b"Synthetic unrelated replacement")
            raise OSError("Synthetic receipt failure")
        return real_link(source, destination)

    monkeypatch.setattr(reports.os, "link", substituted_output)
    with pytest.raises(OSError):
        _publish(output)
    assert output.read_bytes() == b"Synthetic unrelated replacement"
    assert list(tmp_path.glob(".atlas-digest-stage-*")) == []


def test_concurrent_partial_publication_never_reports_completion(tmp_path, monkeypatch):
    output = tmp_path / "report.json"
    published, finish = threading.Event(), threading.Event()
    real_link = reports.os.link

    def paused_link(source, destination):
        result = real_link(source, destination)
        if destination == output:
            published.set()
            assert finish.wait(timeout=5)
        return result

    monkeypatch.setattr(reports.os, "link", paused_link)
    with ThreadPoolExecutor(max_workers=1) as pool:
        writer = pool.submit(_publish, output)
        try:
            assert published.wait(timeout=5)
            # The final report is already complete, but there is no durable
            # receipt yet. A second caller must not claim successful completion.
            assert output.read_bytes() == b"Synthetic complete report"
            with pytest.raises(ExportError):
                _publish(output)
        finally:
            finish.set()
        assert writer.result(timeout=5) is True
    assert _publish(output) is False
    assert list(tmp_path.glob(".atlas-digest-stage-*")) == []


def test_oversized_digest_fails_before_staging(tmp_path, monkeypatch):
    monkeypatch.setattr(reports, "MAX_REPORT_BYTES", 4)
    with pytest.raises(ExportError, match="512 MB"):
        _publish(tmp_path / "report.json")
    assert list(tmp_path.iterdir()) == []


def test_cli_corrupt_report_fails_visibly_without_overwriting(assessment, tmp_path, monkeypatch, capsys):
    settings, baseline, _ = assessment
    monkeypatch.setattr(reports, "get_settings", lambda: settings)
    with sqlite3.connect(settings.scenario_db_path) as conn:
        conn.execute("UPDATE scenarios SET title='Synthetic changed title'")
    output = tmp_path / "reports"
    args = ["--baseline-id", baseline["id"], "--include-context", "--output-directory", str(output),
            "--format", "json", "--only-changes"]
    assert reports.main(args) == 0
    report = next(output.glob("atlas-digest-*.json"))
    report.write_bytes(b"")
    assert reports.main(args) == 1
    assert report.read_bytes() == b""
    assert "integrity" in capsys.readouterr().err
