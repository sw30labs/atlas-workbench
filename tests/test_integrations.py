"""Fictional offline downstream staging and durable row reconciliation."""

import csv
import hashlib
import io
import json
import sqlite3
import zipfile

import pytest

from workbench_atlas import integrations as integration
from workbench_atlas.context import save_record


ACTOR = "Synthetic integration operator"


@pytest.fixture
def integration_settings(settings, tmp_path):
    settings = settings.model_copy(update={"context_enabled": True, "context_db_path": tmp_path / "private.db"})
    with sqlite3.connect(settings.scenario_db_path) as conn:
        conn.executemany("INSERT INTO scenarios(scenario_id,title,status,origin,evidence_refs) VALUES(?,?,?,?,?)", [
            ("scn-a", "Fictional outage", "active", "synthetic_fixture", '["SYN-EVT-000"]'),
            ("scn-b", "Unselected fictional outage", "active", "synthetic_fixture", '["SYN-EVT-001"]'),
        ])
    for suffix in ("a", "b"):
        save_record(settings, "services", {"id": "svc-" + suffix, "name": "Fictional service " + suffix,
            "owner": "Fictional owner", "impact_tolerance": "Two fictional hours",
            "classification": "synthetic", "source": "Synthetic inventory"}, actor=ACTOR)
        save_record(settings, "exposures", {"id": "exposure-" + suffix, "scenario_id": "scn-" + suffix,
            "service_id": "svc-" + suffix, "rationale": "Fictional linkage", "source": "Synthetic workshop"}, actor=ACTOR)
    return settings


def preview(settings, **kwargs):
    return integration.preview_integration(settings, actor=ACTOR,
        selection={"scenario_ids": ["scn-a"], "include_context": True}, **kwargs)


def acknowledgment(row, status="accepted"):
    return {"external_id": row["external_id"], "revision_hash": row["revision_hash"], "status": status,
            "target_id": "fictional-target-id" if status == "accepted" else "",
            "message": "Fictional transform rejected this row" if status == "rejected" else ""}


def test_preview_freezes_scoped_records_without_public_writes_or_network(integration_settings, monkeypatch):
    import httpx

    def forbidden(*args, **kwargs):
        raise AssertionError("No network requests are permitted for a staging package")

    monkeypatch.setattr(httpx.Client, "request", forbidden)
    paths = (integration_settings.scenario_db_path, integration_settings.evidence_db_path,
             integration_settings.emerging_risks_path, integration_settings.context_db_path)
    before = {path: path.read_bytes() for path in paths}
    result = preview(integration_settings)
    assert result["counts"] == {"pending": 4, "accepted": 0, "rejected": 0}
    assert result["manifest"]["classification"] == "includes_organization_context"
    records = result["records"]
    assert {r["record_id"] for r in records} == {"scn-a", "SYN-EVT-000", "svc-a", "exposure-a"}
    assert all(r["revision_hash"] == hashlib.sha256(integration._json(json.loads(r["record_json"])).encode()).hexdigest() for r in records)
    assert all(path.read_bytes() == before[path] for path in paths)
    assert integration._path(integration_settings).stat().st_mode & 0o777 == 0o600


def test_default_public_scope_does_not_read_or_include_organization(integration_settings):
    integration_settings.context_db_path.write_bytes(b"unreadable private fixture")
    result = integration.preview_integration(integration_settings, actor=ACTOR, selection={"scenario_ids": ["scn-a"]})
    assert len(result["records"]) == 2
    assert all(not row["record_type"].startswith("organization_") for row in result["records"])


def test_duplicate_preview_and_download_are_idempotent_and_durable(integration_settings):
    first = preview(integration_settings)
    again = preview(integration_settings)
    assert again["package_id"] == first["package_id"]
    assert len(integration.list_integrations(integration_settings)["packages"]) == 1
    artifact = integration.download_integration(integration_settings, package_id=first["package_id"], actor=ACTOR)
    with sqlite3.connect(integration_settings.scenario_db_path) as conn:
        conn.execute("UPDATE scenarios SET title='Changed after preview' WHERE scenario_id='scn-a'")
    frozen = integration.download_integration(integration_settings, package_id=first["package_id"], actor=ACTOR)
    assert frozen.content == artifact.content
    with zipfile.ZipFile(io.BytesIO(frozen.content)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        for filename, expected in manifest["file_checksums"].items():
            assert hashlib.sha256(archive.read(filename)).hexdigest() == expected
        exact = json.loads(archive.read("canonical-records.json"))["records"]
        assert next(r["record"]["title"] for r in exact if r["record_type"] == "scenarios") == "Fictional outage"
        mapped = json.loads(archive.read("records.json"))["records"]
        assert all("u_external_id" in row for row in mapped)
        assert not any(name.endswith((".sh", ".py")) for name in archive.namelist())


def test_changed_source_revision_keeps_external_identity(integration_settings):
    original = preview(integration_settings)
    with sqlite3.connect(integration_settings.scenario_db_path) as conn:
        conn.execute("UPDATE scenarios SET title='Revised fictional scenario' WHERE scenario_id='scn-a'")
    revised = preview(integration_settings)
    assert original["package_id"] != revised["package_id"]
    old = next(r for r in original["records"] if r["record_type"] == "scenarios")
    new = next(r for r in revised["records"] if r["record_type"] == "scenarios")
    assert old["external_id"] == new["external_id"]
    assert old["revision_hash"] != new["revision_hash"]


@pytest.mark.parametrize("mapping", [
    {"endpoint": "https://example.invalid"}, {"staging_table": "https://example.invalid"},
    {"source_system": "unstable system name"}, {"fields": {"approval": "u_approval"}},
    {"fields": {key: "same_column" for key in integration.REQUIRED_FIELDS}},
    {"fields": {key: ("review_status" if key == "record_json" else "u_" + key) for key in integration.REQUIRED_FIELDS}},
    {"fields": []}, [],
])
def test_mapping_rejects_unsupported_or_unsafe_fields_without_outbox_write(integration_settings, mapping):
    with pytest.raises(integration.IntegrationError):
        preview(integration_settings, mapping=mapping)
    assert not integration._path(integration_settings).exists()


def test_generic_json_and_customer_column_mapping(integration_settings):
    mapping = {"source_system": "fictional-atlas-prod", "staging_table": "fictional_risk_staging",
               "fields": {key: "stage_" + key for key in integration.REQUIRED_FIELDS}}
    result = preview(integration_settings, adapter="generic_rest", mapping=mapping)
    artifact = integration.download_integration(integration_settings, package_id=result["package_id"], actor=ACTOR)
    with zipfile.ZipFile(io.BytesIO(artifact.content)) as archive:
        rows = json.loads(archive.read("records.json"))["records"]
    assert set(rows[0]) == set(mapping["fields"].values())
    assert all(row["stage_source_system"] == "fictional-atlas-prod" for row in rows)


def test_csv_dangerous_text_is_protected_json_retains_exact_text(integration_settings):
    with sqlite3.connect(integration_settings.scenario_db_path) as conn:
        conn.execute("UPDATE scenarios SET title=? WHERE scenario_id='scn-a'", ('=HYPERLINK("https://example.invalid")',))
    result = preview(integration_settings)
    artifact = integration.download_integration(integration_settings, package_id=result["package_id"], actor=ACTOR)
    with zipfile.ZipFile(io.BytesIO(artifact.content)) as archive:
        rows = list(csv.DictReader(io.StringIO(archive.read("records.csv").decode("utf-8-sig"))))
        scenario = next(r for r in rows if r["u_record_type"] == "scenarios")
        assert scenario["u_display_name"].startswith("'=HYPERLINK")
        exact = json.loads(archive.read("records.json"))["records"]
        assert next(r["u_display_name"] for r in exact if r["u_record_type"] == "scenarios").startswith("=HYPERLINK")


def test_acknowledgment_is_actor_bound_atomic_and_does_not_approve_sources(integration_settings):
    result = preview(integration_settings)
    before = integration_settings.evidence_db_path.read_bytes()
    acks = [acknowledgment(result["rows"][0]), acknowledgment(result["rows"][1], "rejected")]
    with pytest.raises(integration.IntegrationError, match="unavailable"):
        integration.acknowledge_integration(integration_settings, package_id=result["package_id"],
                                           actor="Another operator", acknowledgments=acks)
    bad = [acks[0], {**acks[1], "revision_hash": "wrong"}]
    with pytest.raises(integration.IntegrationError, match="revision hash"):
        integration.acknowledge_integration(integration_settings, package_id=result["package_id"], actor=ACTOR, acknowledgments=bad)
    assert integration.list_integrations(integration_settings)["packages"][0]["counts"]["pending"] == 4
    outcome = integration.acknowledge_integration(integration_settings, package_id=result["package_id"], actor=ACTOR, acknowledgments=acks)
    assert outcome["counts"] == {"pending": 2, "accepted": 1, "rejected": 1}
    assert integration_settings.evidence_db_path.read_bytes() == before
    repeated = integration.acknowledge_integration(integration_settings, package_id=result["package_id"], actor=ACTOR, acknowledgments=acks)
    assert repeated["counts"] == outcome["counts"]
    with sqlite3.connect(integration._path(integration_settings)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM integration_receipts").fetchone()[0] == 1
        assert conn.execute("SELECT actor FROM integration_receipts").fetchone()[0] == ACTOR
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("DELETE FROM integration_receipts")


@pytest.mark.parametrize("alteration", [
    {"status": "approved"}, {"status": []}, {"target_id": ""}, {"review_status": "approved"},
    {"revision_hash": "stale"}, {"external_id": "missing"},
])
def test_malformed_acknowledgments_fail_without_changing_status(integration_settings, alteration):
    result = preview(integration_settings)
    with pytest.raises(integration.IntegrationError):
        integration.acknowledge_integration(integration_settings, package_id=result["package_id"], actor=ACTOR,
                                           acknowledgments=[{**acknowledgment(result["rows"][0]), **alteration}])
    assert integration.list_integrations(integration_settings)["packages"][0]["counts"]["pending"] == 4


def test_retry_is_explicit_preserves_revision_and_excludes_accepted_records(integration_settings):
    original = preview(integration_settings)
    accepted, rejected = original["rows"][:2]
    integration.acknowledge_integration(integration_settings, package_id=original["package_id"], actor=ACTOR,
        acknowledgments=[acknowledgment(accepted), acknowledgment(rejected, "rejected")])
    for selected in ([], ["unknown"], [accepted["external_id"]], [rejected["external_id"]] * 2):
        with pytest.raises(integration.IntegrationError):
            integration.retry_integration(integration_settings, package_id=original["package_id"], actor=ACTOR, selected_ids=selected)
    retried = integration.retry_integration(integration_settings, package_id=original["package_id"], actor=ACTOR,
                                           selected_ids=[rejected["external_id"]])
    assert retried["parent_id"] == original["package_id"]
    assert retried["rows"][0]["revision_hash"] == rejected["revision_hash"]
    again = integration.retry_integration(integration_settings, package_id=original["package_id"], actor=ACTOR,
                                         selected_ids=[rejected["external_id"]])
    assert again["package_id"] == retried["package_id"]
    artifact = integration.download_integration(integration_settings, package_id=retried["package_id"], actor=ACTOR)
    with zipfile.ZipFile(io.BytesIO(artifact.content)) as archive:
        assert len(json.loads(archive.read("records.json"))["records"]) == 1
        canonical = json.loads(archive.read("canonical-records.json"))["records"]
        assert [r["external_id"] for r in canonical] == [rejected["external_id"]]


def test_acknowledged_result_cannot_be_rewritten(integration_settings):
    result = preview(integration_settings)
    ack = acknowledgment(result["rows"][0])
    integration.acknowledge_integration(integration_settings, package_id=result["package_id"], actor=ACTOR, acknowledgments=[ack])
    with pytest.raises(integration.IntegrationError, match="immutable"):
        integration.acknowledge_integration(integration_settings, package_id=result["package_id"], actor=ACTOR,
                                           acknowledgments=[{**ack, "target_id": "different-target"}])


def test_read_and_invalid_ack_do_not_create_outbox(integration_settings):
    assert integration.list_integrations(integration_settings)["packages"] == []
    with pytest.raises(integration.IntegrationError, match="unavailable"):
        integration.acknowledge_integration(integration_settings, package_id="missing", actor=ACTOR, acknowledgments=[{}])
    assert not integration._path(integration_settings).exists()


def test_outbox_integrity_and_unrelated_database_fail_closed(integration_settings):
    path = integration._path(integration_settings)
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE unrelated(id TEXT)")
    before = path.read_bytes()
    with pytest.raises(integration.IntegrationError, match="another application"):
        preview(integration_settings)
    assert path.read_bytes() == before
    path.unlink()
    result = preview(integration_settings)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE integration_packages SET payload_json='{}'")
    with pytest.raises(integration.IntegrationError, match="integrity"):
        integration.download_integration(integration_settings, package_id=result["package_id"], actor=ACTOR)


def test_outbox_cannot_alias_source_store(integration_settings):
    path = integration._path(integration_settings)
    path.hardlink_to(integration_settings.context_db_path)
    with pytest.raises(integration.IntegrationError, match="separate"):
        preview(integration_settings)


def test_package_and_row_limits_fail_before_partial_package(integration_settings, monkeypatch):
    monkeypatch.setattr(integration, "MAX_ROWS", 1)
    with pytest.raises(integration.IntegrationError, match="at most"):
        preview(integration_settings)
    assert not integration._path(integration_settings).exists()
    monkeypatch.setattr(integration, "MAX_ROWS", 2000)
    monkeypatch.setattr(integration, "MAX_PACKAGE_BYTES", 50)
    with pytest.raises(integration.IntegrationError, match="20 MB"):
        preview(integration_settings)
    # Schema creation and package writes roll back together.
    with sqlite3.connect(integration._path(integration_settings)) as conn:
        assert conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == []
