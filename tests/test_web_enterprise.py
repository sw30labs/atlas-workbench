"""Offline HTTP integration for the enterprise workbench, using fictional stores."""

from __future__ import annotations

import http.client
import io
import json
import re
import shutil
import sqlite3
import subprocess
import threading
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

import pytest
import yaml

from workbench_atlas import imports
from workbench_atlas.web import enterprise, server


ACTOR = "Synthetic integration reviewer"
SOURCE = "Fictional integration fixture, not an actual organization"


@pytest.fixture
def enterprise_settings(settings, tmp_path, monkeypatch):
    configured = settings.model_copy(update={
        "context_enabled": True, "context_db_path": tmp_path / "organization.db",
    })
    with sqlite3.connect(configured.scenario_db_path) as conn:
        conn.executemany(
            "INSERT INTO scenarios (scenario_id, title, description, status, origin, evidence_refs) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [("SYN-SCN-A", "Fictional payments outage", "Synthetic description A", "active",
              "synthetic_fixture", '["SYN-EVT-000"]'),
             ("SYN-SCN-B", "Fictional supplier outage", "Synthetic description B", "active",
              "synthetic_fixture", '["SYN-EVT-001"]')],
        )
    monkeypatch.setattr(server, "get_settings", lambda: configured)
    monkeypatch.setattr(enterprise, "PREVIEWS", enterprise.PreviewCache())
    monkeypatch.setattr(imports, "_PREVIEWS", {})
    return configured


@pytest.fixture
def dashboard(enterprise_settings):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.DashboardHandler)
    thread = threading.Thread(target=lambda: httpd.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    try:
        yield httpd.server_address[1]
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def request(port, path, body=None, *, headers=None, raw=None):
    method = "GET" if body is None and raw is None else "POST"
    outgoing = {"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{port}"}
    outgoing.update(headers or {})
    content = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request(method, path, body=content, headers=outgoing)
        response = connection.getresponse()
        data = response.read()
        meta = dict(response.getheaders())
        return response.status, meta, json.loads(data) if "application/json" in meta.get("Content-Type", "") else data
    finally:
        connection.close()


def save(port, kind, record, **extra):
    return request(port, "/api/context/save", {
        "kind": kind, "record": {"source": SOURCE, "classification": "synthetic", **record},
        "actor": ACTOR, **extra,
    })


def service(port, record_id="svc-a"):
    status, _, record = save(port, "services", {
        "id": record_id, "name": "Fictional payment service", "owner": "Fictional owner",
        "impact_tolerance": "Synthetic limit of two hours",
    })
    assert status == 200, record
    return record


def preview(port, **selection):
    status, _, result = request(port, "/api/exports/preview", {"selection": selection})
    assert status == 200, result
    return result


def test_download_is_frozen_and_cannot_widen_after_preview(dashboard, enterprise_settings):
    reviewed = preview(dashboard, scenario_ids=["SYN-SCN-A"])
    with sqlite3.connect(enterprise_settings.scenario_db_path) as conn:
        conn.execute("UPDATE scenarios SET title='Changed after review' WHERE scenario_id='SYN-SCN-A'")
    status, headers, result = request(dashboard, "/api/exports/download", {
        "preview_token": reviewed["preview_token"], "format": "json",
        "selection": {"scenario_ids": ["SYN-SCN-A", "SYN-SCN-B"], "include_context": True},
    })
    assert status == 200
    assert result["manifest"]["snapshot_id"] == reviewed["manifest"]["snapshot_id"]
    assert [r["scenario_id"] for r in result["tables"]["scenarios"]] == ["SYN-SCN-A"]
    assert result["tables"]["scenarios"][0]["title"] == "Fictional payments outage"
    assert not any(name.startswith("organization_") for name in result["tables"])
    assert headers["Cache-Control"] == "no-store"
    assert headers["Content-Disposition"].startswith('attachment; filename="workbench-atlas-')


@pytest.mark.parametrize("format,media_type", [
    ("pdf", "application/pdf"), ("xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    ("csv", "application/zip"),
])
def test_download_serves_valid_binary_formats(dashboard, format, media_type):
    reviewed = preview(dashboard, scenario_ids=["SYN-SCN-A"])
    status, headers, content = request(dashboard, "/api/exports/download", {
        "preview_token": reviewed["preview_token"], "format": format,
    })
    assert status == 200
    assert headers["Content-Type"] == media_type
    assert int(headers["Content-Length"]) == len(content)
    if format == "pdf":
        assert content.startswith(b"%PDF-")
    else:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            assert ("[Content_Types].xml" if format == "xlsx" else "manifest.json") in archive.namelist()


@pytest.mark.parametrize("selection", [
    {"scenario_ids": []}, {"scenario_ids": ["missing"]}, {"scenario_ids": "SYN-SCN-A"},
    {"include_context": True}, {"include_context": "false"}, {"unsupported": True}, [],
])
def test_invalid_export_selection_never_silently_expands(dashboard, selection):
    status, _, result = request(dashboard, "/api/exports/preview", {"selection": selection})
    assert status == 400
    assert "error" in result


@pytest.mark.parametrize("format", [[], {}, None, "html"])
def test_bad_download_format_is_a_json_error(dashboard, format):
    reviewed = preview(dashboard, scenario_ids=["SYN-SCN-A"])
    status, _, result = request(dashboard, "/api/exports/download", {
        "preview_token": reviewed["preview_token"], "format": format,
    })
    assert status == 400
    assert "Choose" in result["error"]


def test_export_preview_tokens_expire_and_are_bound_to_owner(dashboard, monkeypatch):
    reviewed = preview(dashboard, scenario_ids=["SYN-SCN-A"])
    with pytest.raises(ValueError, match="unavailable"):
        enterprise.PREVIEWS.get(reviewed["preview_token"], owner="another-operator")
    now = enterprise.time.monotonic()
    monkeypatch.setattr(enterprise.time, "monotonic", lambda: now + 901)
    status, _, result = request(dashboard, "/api/exports/download", {
        "preview_token": reviewed["preview_token"], "format": "json",
    })
    assert status == 400
    assert "expired" in result["error"]


def test_private_export_requires_opt_in_and_only_contains_linked_services(dashboard):
    for suffix in ("A", "B"):
        record = service(dashboard, f"svc-{suffix}")
        status, _, result = save(dashboard, "exposures", {
            "scenario_id": f"SYN-SCN-{suffix}", "service_id": record["id"],
            "rationale": "Fictional workshop linkage",
        })
        assert status == 200, result
    reviewed = preview(dashboard, scenario_ids=["SYN-SCN-A"], include_context=True)
    assert reviewed["manifest"]["classification"] == "includes_organization_context"
    status, _, result = request(dashboard, "/api/exports/download", {
        "preview_token": reviewed["preview_token"], "format": "json",
    })
    assert status == 200
    assert [r["id"] for r in result["tables"]["organization_services"]] == ["svc-A"]


def test_context_read_create_update_conflict_archive_and_decision_history(dashboard, enterprise_settings):
    public_before = enterprise_settings.scenario_db_path.read_bytes()
    status, _, initial = request(dashboard, "/api/context")
    assert status == 200 and initial["store_revision"] == 0
    assert not enterprise_settings.context_db_path.exists()
    original = service(dashboard)
    changed = {**original, "owner": "Second fictional owner"}
    status, _, updated = save(dashboard, "services", changed, expected_revision=1)
    assert status == 200 and updated["revision"] == 2
    status, _, conflict = save(dashboard, "services", changed, expected_revision=1)
    assert status == 409 and "error" in conflict
    status, _, conflict = request(dashboard, "/api/context/archive", {
        "kind": "services", "record_id": original["id"], "actor": ACTOR, "expected_revision": 1,
    })
    assert status == 409
    status, _, archived = request(dashboard, "/api/context/archive", {
        "kind": "services", "record_id": original["id"], "actor": ACTOR, "expected_revision": 2,
    })
    assert status == 200 and archived["archived"] is True
    assert enterprise_settings.scenario_db_path.read_bytes() == public_before
    with sqlite3.connect(enterprise_settings.context_db_path) as conn:
        events = conn.execute("SELECT actor FROM context_decisions ORDER BY sequence").fetchall()
    assert events == [(ACTOR,), (ACTOR,), (ACTOR,)]


def test_baseline_reports_later_change_and_rejects_stale_revision(dashboard):
    original = service(dashboard)
    status, _, state = request(dashboard, "/api/context")
    assert status == 200
    status, _, baseline = request(dashboard, "/api/context/baseline", {
        "name": "Fictional workshop starting point", "actor": ACTOR,
        "expected_revision": state["store_revision"],
    })
    assert status == 200
    status, _, result = save(dashboard, "services", {**original, "owner": "New fictional owner"}, expected_revision=1)
    assert status == 200, result
    status, _, result = request(dashboard, "/api/context/baseline", {
        "name": "Stale baseline", "actor": ACTOR, "expected_revision": state["store_revision"],
    })
    assert status == 409
    status, _, diff = request(dashboard, "/api/context/compare?baseline_id=" + quote(baseline["id"]))
    assert status == 200
    assert diff["counts"] == {"added": 0, "changed": 1, "removed": 0}
    assert diff["changed"][0]["before"]["owner"] == "Fictional owner"
    assert diff["changed"][0]["after"]["owner"] == "New fictional owner"


def test_baseline_review_pack_downloads_the_same_private_frozen_snapshot(dashboard):
    original = service(dashboard)
    status, _, baseline = request(dashboard, "/api/context/baseline", {"actor": ACTOR, "name": "Synthetic baseline"})
    assert status == 200
    status, _, updated = save(dashboard, "services", {**original, "owner": "Reviewed fictional owner"}, expected_revision=1)
    assert status == 200
    reviewed = preview(dashboard, baseline_id=baseline["id"], include_context=True)
    assert reviewed["manifest"]["classification"] == "includes_organization_context"
    status, _, result = save(dashboard, "services", {**updated, "owner": "Changed after frozen preview"}, expected_revision=2)
    assert status == 200, result
    status, _, frozen = request(dashboard, "/api/exports/download", {
        "preview_token": reviewed["preview_token"], "format": "json",
    })
    assert status == 200 and frozen["manifest"]["snapshot_id"] == reviewed["manifest"]["snapshot_id"]
    assert "organization_review_summary" in frozen["tables"]
    assert "Reviewed fictional owner" in json.dumps(frozen["tables"])
    assert "Changed after frozen preview" not in json.dumps(frozen["tables"])
    status, headers, pdf = request(dashboard, "/api/exports/download", {
        "preview_token": reviewed["preview_token"], "format": "pdf",
    })
    assert status == 200 and headers["Content-Type"] == "application/pdf" and pdf.startswith(b"%PDF-")


def test_baseline_pack_selection_requires_private_opt_in_and_rejects_mixed_roots(dashboard):
    service(dashboard)
    status, _, baseline = request(dashboard, "/api/context/baseline", {"actor": ACTOR, "name": "Synthetic baseline"})
    assert status == 200
    for selection in ({"baseline_id": baseline["id"]},
                      {"baseline_id": baseline["id"], "include_context": False},
                      {"baseline_id": baseline["id"], "include_context": True, "scenario_ids": ["SYN-SCN-A"]}):
        status, _, result = request(dashboard, "/api/exports/preview", {"selection": selection})
        assert status == 400 and "error" in result


def test_service_links_require_current_records_and_coverage_has_explicit_denominator(dashboard):
    record = service(dashboard)
    status, _, mechanism = save(dashboard, "mechanisms", {
        "name": "Fictional network isolation", "description": "Synthetic mechanism",
    })
    assert status == 200
    status, _, rejected = save(dashboard, "exposures", {
        "scenario_id": "missing-scenario", "service_id": record["id"], "rationale": "Fictional link",
    })
    assert status == 400 and "unknown scenario" in rejected["error"]
    status, _, exposure = save(dashboard, "exposures", {
        "scenario_id": "SYN-SCN-A", "service_id": record["id"],
        "mechanism_ids": [mechanism["id"]], "rationale": "Fictional reviewed linkage", "status": "confirmed",
    })
    assert status == 200
    status, _, state = request(dashboard, "/api/context")
    assert status == 200 and state["coverage"]["denominator"] == 1
    # A confirmed exposure alone does not assert that an assessment was performed.
    assert state["coverage"]["counts"] == {"unassessed": 1, "mapped": 0, "tested": 0}
    status, _, assessment = save(dashboard, "assessments", {
        "service_id": record["id"], "mechanism_id": mechanism["id"],
        "exposure_ids": [exposure["id"]], "status": "mapped", "assessed_on": "2026-01-01",
    })
    assert status == 200, assessment
    status, _, state = request(dashboard, "/api/context")
    assert state["coverage"]["counts"] == {"unassessed": 0, "mapped": 1, "tested": 0}
    status, _, withdrawn = save(dashboard, "exposures", {**exposure, "status": "dismissed"}, expected_revision=1)
    assert status == 200, withdrawn
    status, _, state = request(dashboard, "/api/context")
    assert state["coverage"]["counts"] == {"unassessed": 1, "mapped": 0, "tested": 0}
    assert state["coverage"]["rows"][0]["needs_reassessment"] is True


def test_activation_preserves_unrelated_configuration(dashboard, enterprise_settings, tmp_path, monkeypatch):
    from workbench_atlas.web import context_setup

    config_path = tmp_path / "configuration.yaml"
    original = "# Operator comment\nscoring:\n  simulation_seed: 17\norganization_context:\n  enabled: false\n"
    config_path.write_text(original)
    monkeypatch.setattr(context_setup, "yaml_config_path", lambda: config_path)
    monkeypatch.delenv("ATLAS_CONTEXT_ENABLED", raising=False)
    status, _, result = request(dashboard, "/api/context/enable", {"actor": ACTOR})
    assert status == 200 and result["enabled"] is True
    updated = config_path.read_text()
    assert updated.startswith("# Operator comment\nscoring:\n  simulation_seed: 17\n")
    assert yaml.safe_load(updated)["organization_context"] == {
        "enabled": True, "db_path": str(enterprise_settings.context_db_path),
    }
    assert not enterprise_settings.context_db_path.exists()


@pytest.mark.parametrize("failure", ["deployment-disabled", "invalid-yaml"])
def test_activation_failures_leave_configuration_unchanged(dashboard, tmp_path, monkeypatch, failure):
    from workbench_atlas.web import context_setup

    config_path = tmp_path / "configuration.yaml"
    original = "scoring: [broken" if failure == "invalid-yaml" else "# Fictional config\nscoring: {}\n"
    config_path.write_text(original)
    monkeypatch.setattr(context_setup, "yaml_config_path", lambda: config_path)
    monkeypatch.delenv("ATLAS_CONTEXT_ENABLED", raising=False)
    if failure == "deployment-disabled":
        monkeypatch.setenv("ATLAS_CONTEXT_ENABLED", "false")
    status, _, result = request(dashboard, "/api/context/enable", {"actor": ACTOR})
    assert status == 400 and "error" in result
    assert config_path.read_text() == original


def test_import_mapping_preview_actor_binding_selected_commit_and_replay(dashboard, enterprise_settings):
    public_before = enterprise_settings.scenario_db_path.read_bytes()
    status, _, result = request(dashboard, "/api/imports/preview", {
        "format": "csv", "kind": "services", "actor": ACTOR, "source": SOURCE,
        "content": "Service,Team,Tolerance\nFictional A,Example A,Two hours\nFictional B,Example B,Four hours\n",
        "field_mapping": {"Service": "name", "Team": "owner", "Tolerance": "impact_tolerance"},
    })
    assert status == 200 and result["counts"] == {"total": 2, "valid": 2, "invalid": 0}
    assert not enterprise_settings.context_db_path.exists()
    selected = result["candidates"][0]
    submission = {"preview_id": result["preview_id"], "selected_ids": [selected["candidate_id"]], "actor": ACTOR}
    status, _, rejected = request(dashboard, "/api/imports/submit", {**submission, "actor": "Different operator"})
    assert status == 400 and "error" in rejected
    assert not enterprise_settings.context_db_path.exists()
    status, _, committed = request(dashboard, "/api/imports/submit", submission)
    assert status == 200 and committed["imported_count"] == 1
    assert committed["imported"][0]["name"] == "Fictional A"
    assert committed["imported"][0]["classification"] == "organization"
    status, _, rejected = request(dashboard, "/api/imports/submit", submission)
    assert status == 400 and "error" in rejected
    assert enterprise_settings.scenario_db_path.read_bytes() == public_before


@pytest.mark.parametrize("path", sorted(enterprise.POST_PATHS))
def test_enterprise_mutations_reject_cross_origin_before_dispatch(dashboard, path):
    status, _, result = request(dashboard, path, {}, headers={"Origin": "https://untrusted.invalid"})
    assert status == 403 and "error" in result


@pytest.mark.parametrize("actor", [None, "", [], {"name": "fictional"}])
def test_context_actor_must_be_a_named_string(dashboard, actor):
    status, _, result = save(dashboard, "services", {
        "name": "Fictional", "owner": "Fictional", "impact_tolerance": "Two hours",
    }, actor=actor)
    assert status == 400 and "error" in result


@pytest.mark.parametrize("payload", [
    {"kind": [], "record": {}}, {"kind": "services", "record": []},
    {"kind": {}, "record": {}}, {"kind": "unknown", "record": {}},
])
def test_malformed_context_payloads_return_json_error(dashboard, payload):
    status, _, result = request(dashboard, "/api/context/save", {"actor": ACTOR, **payload})
    assert status == 400 and "error" in result


def test_enterprise_request_validation_happens_before_dispatch(dashboard):
    path = "/api/exports/preview"
    status, _, result = request(dashboard, path, {}, headers={"Content-Type": "text/plain"})
    assert status == 415 and "error" in result
    status, _, result = request(dashboard, path, raw=b"[]")
    assert status == 400 and "object" in result["error"]
    status, _, result = request(dashboard, path, raw=b"{broken")
    assert status == 400 and "JSON" in result["error"]
    # Oversized declared length is rejected before the server tries reading it.
    status, _, result = request(dashboard, path, raw=b"{}", headers={"Content-Length": str(server.MAX_REQUEST_BYTES + 1)})
    assert status == 413 and "too large" in result["error"]
    status, _, result = request(dashboard, "/api/imports/preview", raw=b"{}",
                                headers={"Content-Length": str(server.MAX_INGEST_BYTES + 1)})
    assert status == 413 and "too large" in result["error"]
    status, _, result = request(dashboard, "/api/context", headers={"Host": "untrusted.invalid"})
    assert status == 403 and "error" in result


def test_import_content_limit_is_distinct_from_http_envelope_limit(dashboard):
    status, _, result = request(dashboard, "/api/imports/preview", {
        "content": "x" * (imports.MAX_IMPORT_BYTES + 1), "format": "csv", "kind": "services",
        "actor": ACTOR, "source": SOURCE,
    })
    assert status == 400 and "2 MB" in result["error"]


def test_staged_integration_download_ack_and_explicit_retry(dashboard):
    status, _, package = request(dashboard, "/api/integrations/preview", {
        "actor": ACTOR, "adapter": "servicenow", "selection": {"scenario_ids": ["SYN-SCN-A"]},
    })
    assert status == 200 and package["counts"]["pending"] == 2
    status, headers, content = request(dashboard, "/api/integrations/download", {
        "actor": ACTOR, "package_id": package["package_id"],
    })
    assert status == 200 and headers["Content-Type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        assert "acknowledgments-template.json" in archive.namelist()
    accepted, rejected = package["rows"]
    status, _, result = request(dashboard, "/api/integrations/ack", {
        "actor": ACTOR, "package_id": package["package_id"], "acknowledgments": [
            {"external_id": accepted["external_id"], "revision_hash": accepted["revision_hash"],
             "status": "accepted", "target_id": "fictional-destination-id"},
            {"external_id": rejected["external_id"], "revision_hash": rejected["revision_hash"],
             "status": "rejected", "message": "Fictional mapping mismatch"},
        ],
    })
    assert status == 200 and result["counts"] == {"pending": 0, "accepted": 1, "rejected": 1}
    status, _, retry = request(dashboard, "/api/integrations/retry", {
        "actor": ACTOR, "package_id": package["package_id"], "selected_ids": [rejected["external_id"]],
    })
    assert status == 200 and retry["parent_id"] == package["package_id"]
    assert len(retry["rows"]) == 1 and retry["rows"][0]["revision_hash"] == rejected["revision_hash"]
    status, _, listing = request(dashboard, "/api/integrations")
    assert status == 200 and len(listing["packages"]) == 2
    assert all("records" not in p for p in listing["packages"])


def test_staged_integration_owner_binding_and_selection_errors(dashboard):
    status, _, result = request(dashboard, "/api/integrations/preview", {
        "actor": ACTOR, "selection": {"scenario_ids": []},
    })
    assert status == 400 and "error" in result
    status, _, package = request(dashboard, "/api/integrations/preview", {
        "actor": ACTOR, "selection": {"scenario_ids": ["SYN-SCN-A"]}, "adapter": "generic_rest",
    })
    assert status == 200
    status, _, result = request(dashboard, "/api/integrations/download", {
        "actor": "Another operator", "package_id": package["package_id"],
    })
    assert status == 400 and "unavailable" in result["error"]


@pytest.mark.parametrize("store,failure", [
    ("scenarios", "corrupt"), ("evidence", "corrupt"), ("risks", "corrupt"),
    ("scenarios", "missing"), ("evidence", "missing"), ("risks", "missing"),
    ("risks", "invalid-utf8"),
])
def test_data_health_is_available_when_a_research_store_fails(dashboard, enterprise_settings, store, failure):
    path = {"scenarios": enterprise_settings.scenario_db_path,
            "evidence": enterprise_settings.evidence_db_path,
            "risks": enterprise_settings.emerging_risks_path}[store]
    if failure == "missing":
        path.unlink()
    else:
        path.write_bytes(b"\xff\xfe" if failure == "invalid-utf8" else b"corrupt synthetic store")
    before = path.read_bytes() if path.exists() else None
    status, _, result = request(dashboard, "/api/data")
    assert status == 200
    health = next(row for row in result["meta"]["data_health"]["stores"] if row["store"] == store)
    assert health["status"] == ("uninitialized" if failure == "missing" else "unavailable")
    assert (path.read_bytes() if path.exists() else None) == before


def test_static_workbench_assets_are_served_locally(dashboard):
    status, headers, page = request(dashboard, "/")
    assert status == 200 and "text/html" in headers["Content-Type"]
    html = page.decode()
    resources = re.findall(r'<(?:script|link)\b[^>]*(?:src|href)="([^"]+)"', html)
    assert "/experience.js" in resources and "/experience.css" in resources
    assert all(url.startswith("/") and not url.startswith("//") for url in resources)
    for path, content_type in [("/experience.js", "text/javascript"), ("/experience.css", "text/css")]:
        status, headers, content = request(dashboard, path)
        assert status == 200 and content_type in headers["Content-Type"]
        assert content == (server.STATIC_DIR / path[1:]).read_bytes()
        assert headers["X-Content-Type-Options"] == "nosniff"


def test_external_workbench_javascript_parses():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is optional; syntax check runs where Node is installed")
    script = Path(server.STATIC_DIR) / "experience.js"
    result = subprocess.run([node, "--check", str(script)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_handoff_dialogs_preserve_scope_escape_records_and_serialize_receipts():
    """Run the actual handoff functions with deferred requests and a small DOM double."""
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is optional; browser behavior regression runs where Node is installed")
    script = Path(server.STATIC_DIR) / "experience.js"
    harness = r'''
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync(process.argv[1],'utf8');
const block=source.slice(source.indexOf('  async function integrationDialog('),source.indexOf('  async function loadOperations('));
let dialog,opened=[],calls=[],responses=[],downloads=[];
const h=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const label=value=>String(value??'').replaceAll('_',' '),pretty=value=>typeof value==='object'?JSON.stringify(value):String(value??'');
class Element {
  constructor(tag,attrs,owner){this.tag=tag;this.attrs=attrs;this.owner=owner;this.dataset={};this.value=attrs.value||'';this.disabled='disabled'in attrs;this.checked='checked'in attrs;this.textContent='';for(const [k,v] of Object.entries(attrs))if(k.startsWith('data-'))this.dataset[k.slice(5).replace(/-([a-z])/g,(_,c)=>c.toUpperCase())]=v;}
  set innerHTML(value){this.html=value;this.owner.add(value);}
}
class View {
  constructor(title,html){this.title=title;this.html=html;this.nodes=[];this.add(html);}
  add(html){for(const match of html.matchAll(/<([a-z][a-z0-9-]*)\b([^>]*)>/gi)){const attrs={};for(const a of match[2].matchAll(/([\w-]+)(?:="([^"]*)")?/g))attrs[a[1]]=a[2]??'';this.nodes.push(new Element(match[1],attrs,this));}}
  querySelectorAll(selector){return this.nodes.filter(el=>selector.split(',').some(s=>{s=s.trim();if(s.startsWith('#'))return el.attrs.id===s.slice(1);const attr=s.match(/^\[([^=\]]+)(?:="([^"]*)")?\]$/);if(attr)return attr[1]in el.attrs&&(attr[2]===undefined||el.attrs[attr[1]]===attr[2]);if(s==='button:not([data-close])')return el.tag==='button'&&!('data-close'in el.attrs);return el.tag===s;}));}
  querySelector(selector){return this.querySelectorAll(selector)[0]||null;}
}
function open(title,html){dialog=new View(title,html);opened.push(dialog);return dialog;}
const q=s=>dialog.querySelector(s),qa=s=>dialog.querySelectorAll(s);
const actorField=()=>'<input id="ex-actor" value="Fictional operator">',getActor=()=>q('#ex-actor')?.value||'Fictional operator';
const error=message=>{if(q('#ex-error'))q('#ex-error').textContent=message;};
async function api(path,body){calls.push({path,body});assert.ok(responses.length,'A response must be explicitly queued');return await responses.shift();}
function deferred(){let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return{promise,resolve,reject};}
const context={open,q,qa,h,label,pretty,actorField,getActor,error,api,JSON,Object,Number,String,Array,console};
Object.defineProperty(context,'dialog',{get:()=>dialog,set:v=>dialog=v});
vm.createContext(context);vm.runInContext(block,context);
context.downloadPackage=async(id,actor)=>{downloads.push({id,actor});};
const manifest={classification:'includes_organization_context',record_count:1,snapshot_id:'snapshot-a',source_generated_at:'2026-09-08',selection:{scenario_ids:['scn-a'],include_context:true},mapping:{source_system:'fictional-atlas',staging_table:'u_fictional_staging',fields:{record_json:'u_frozen_data'}}};
const packageData={package_id:'package-a',adapter:'servicenow',actor:'Fictional operator',notice:'No transmission',manifest,counts:{pending:1,accepted:0,rejected:0},records:[{display_name:'Fictional readable service',external_id:'opaque-id',record_type:'organization_services',classification:'organization',source_status:'proposed',revision_hash:'hash-a',record_json:JSON.stringify({name:'Fictional readable service',notes:'<img src=x onerror=alert(1)>'})}],rows:[{external_id:'opaque-id',revision_hash:'hash-a',status:'pending'}]};
(async()=>{
  // Preparation freezes the actual scope and cannot reopen after navigation.
  await context.integrationDialog(['scn-a']);q('#ex-adapter').value='servicenow';q('#ex-handoff-private').checked=true;
  const original=dialog,prepared=deferred();responses.push(prepared.promise);
  const preparing=q('#ex-stage-handoff').onclick();
  assert.equal(q('#ex-handoff-private').disabled,true);assert.equal(q('#ex-staging-table').disabled,true);
  assert.equal(calls.at(-1).body.selection.include_context,true);
  open('Another task','<div id="ex-error"></div>');const next=dialog;
  prepared.resolve(packageData);await preparing;assert.equal(dialog,next);assert.notEqual(dialog,original);

  // Review displays real names, frozen scope/mapping and escaped record content.
  context.showIntegrationPackage(packageData,'Fictional operator');
  assert.ok(dialog.html.includes('Fictional readable service'));
  assert.ok(dialog.html.includes('Includes private organization context'));
  assert.ok(dialog.html.includes('u_fictional_staging'));assert.ok(dialog.html.includes('u_frozen_data'));
  assert.ok(dialog.html.includes('&lt;img src=x onerror=alert(1)&gt;'));assert.ok(!dialog.html.includes('<img src=x'));

  // Existing packages remain downloadable from persistent history.
  responses.push({packages:[packageData]});await context.outboxDialog();
  await qa('[data-download-package]')[0].onclick();
  assert.equal(downloads.at(-1).id,'package-a');assert.equal(downloads.at(-1).actor,'Fictional operator');

  // Late history responses cannot replace another open workflow.
  const late=deferred();responses.push(late.promise);const loading=context.outboxDialog();
  open('Different workflow','<div id="ex-error"></div>');const different=dialog;
  late.resolve({packages:[packageData]});await loading;assert.equal(dialog,different);

  const receiptPack={...packageData,rows:[{external_id:'accepted-id',revision_hash:'a',status:'accepted',target_id:'destination-123',acknowledged_at:'2026-09-08'},{external_id:'rejected-id',revision_hash:'b',status:'rejected',message:'Missing destination mapping',acknowledged_at:'2026-09-08'},{external_id:'pending-id',revision_hash:'c',status:'pending'}]};
  context.reconcileDialog(receiptPack,'Fictional operator');
  assert.ok(dialog.html.includes('destination-123'));assert.ok(dialog.html.includes('Missing destination mapping'));
  assert.equal(qa('[data-receipt-status]').length,1);
  assert.equal(q('#ex-record-receipt').disabled,true);assert.equal(q('#ex-retry-package').disabled,false);
  const status=qa('[data-receipt-status]')[0],detail=qa('[data-receipt-detail]')[0];
  status.value='accepted';detail.value='new-destination';status.onchange();
  assert.equal(q('#ex-retry-package').disabled,true);
  const before=calls.length;await q('#ex-retry-package').onclick();assert.equal(calls.length,before);

  // Mutations serialize; retries cannot race a pending receipt write.
  const saved=deferred();responses.push(saved.promise);const saving=q('#ex-record-receipt').onclick();
  assert.equal(q('#ex-record-receipt').disabled,true);assert.equal(q('#ex-retry-package').disabled,true);
  assert.equal(status.disabled,true);await q('#ex-retry-package').onclick();assert.equal(calls.length,before+1);
  assert.equal(calls.at(-1).body.acknowledgments[0].external_id,'pending-id');
  open('Closed receipt','<div id="ex-error"></div>');const closed=dialog;
  saved.resolve({});await saving;assert.equal(dialog,closed);

  // A clean explicit retry retains only outstanding rows and honors dialog identity.
  context.reconcileDialog(receiptPack,'Fictional operator');const retry=deferred();responses.push(retry.promise);
  const retrying=q('#ex-retry-package').onclick();
  assert.deepEqual(Array.from(calls.at(-1).body.selected_ids),['rejected-id','pending-id']);
  assert.equal(q('#ex-record-receipt').disabled,true);
  open('After retry','<div id="ex-error"></div>');const after=dialog;
  retry.resolve(packageData);await retrying;assert.equal(dialog,after);
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
    result = subprocess.run([node, "-e", harness, str(script)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
