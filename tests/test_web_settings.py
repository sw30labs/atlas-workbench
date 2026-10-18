"""Settings view: local catalog ingest, YAML model writes, no downloads."""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import threading
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path

import yaml

from contingency_atlas.config import Settings, load_yaml_settings
from contingency_atlas.schema import migrate, migrate_evidence_events
from contingency_atlas.web import server
from contingency_atlas.web.server import DashboardHandler
from contingency_atlas.web.settings import (
    PINNED_ATLAS_RELEASE,
    apply_model_settings,
    update_yaml_model_settings,
)
from tests.test_mitre_atlas_import import _document, _write


@contextmanager
def _dashboard():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), DashboardHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd.server_address[1]
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def _request(port: int, method: str, path: str, body=None, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=8)
    try:
        payload = json.dumps(body).encode() if body is not None else None
        base = {"Content-Type": "application/json"} if payload is not None else {}
        connection.request(
            method,
            path,
            body=payload,
            headers={**base, **(headers or {})},
        )
        response = connection.getresponse()
        return response.status, json.loads(response.read() or b"{}")
    finally:
        connection.close()


def _same_origin(port: int) -> dict[str, str]:
    return {"Origin": f"http://127.0.0.1:{port}"}


def _operator_settings(tmp_path, monkeypatch):
    scenario_db = tmp_path / "atlas.db"
    evidence_db = tmp_path / "event_evidence.db"
    risks_path = tmp_path / "emerging_risks.json"
    migrate(str(scenario_db))
    migrate_evidence_events(str(evidence_db))
    risks_path.write_text("[]")
    cfg = tmp_path / "contingency-atlas.yaml"
    cfg.write_text(
        "# keep this comment\n"
        "provider: omlx\n"
        "\n"
        "omlx:\n"
        "  # generation\n"
        "  model: keep-me-model  # default\n"
        "  model_judge: keep-me-judge\n"
        "  model_final_judge: \"\"\n"
        "dgx:\n"
        "  model: Qwen/leave-alone\n"
    )
    monkeypatch.setenv("ATLAS_CONFIG", str(cfg))
    for name in (
        "ATLAS_LLM_PROVIDER",
        "LLM_PROVIDER",
        "OMLX_MODEL",
        "OMLX_MODEL_JUDGE",
        "OMLX_MODEL_FINAL_JUDGE",
    ):
        monkeypatch.delenv(name, raising=False)
    settings = Settings(
        scenario_db_path=scenario_db,
        evidence_db_path=evidence_db,
        emerging_risks_path=risks_path,
        llm_provider="omlx",
        omlx_model="keep-me-model",
        omlx_model_judge="keep-me-judge",
        omlx_model_final_judge="",
    )
    monkeypatch.setattr(server, "get_settings", lambda: settings)
    monkeypatch.setattr(
        "contingency_atlas.web.settings._served_models",
        lambda _settings: (False, [], "offline in tests", False, "offline in tests"),
    )
    return settings, cfg


def test_settings_payload_reports_empty_catalog_and_planned_slots(
    tmp_path, monkeypatch
):
    settings, _cfg = _operator_settings(tmp_path, monkeypatch)
    with _dashboard() as port:
        code, payload = _request(port, "GET", "/api/settings")

    assert code == 200
    atlas = payload["frameworks"]["mitre_atlas"]
    assert atlas["installed"] is False
    assert atlas["active_release"] is None
    planned_ids = {item["id"] for item in payload["frameworks"]["planned"]}
    assert planned_ids == {"mitre-attack", "nist-ai-rmf"}
    feed_ids = {item["id"] for item in payload["feeds"]["planned"]}
    assert feed_ids == {"automatic-refresh"}
    assert payload["frameworks"]["import"]["downloads"] is False
    assert (
        payload["frameworks"]["import"]["recommended"]["expected_sha256"]
        == PINNED_ATLAS_RELEASE["expected_sha256"]
    )
    scout = payload["feeds"]["scout_registry"]
    assert scout["available"] is True
    assert scout["source_count"] >= 1
    assert payload["inference"]["restart_required_for_changes"] is True
    assert payload["inference"]["configured"]["model"] == settings.omlx_model
    assert payload["inference"]["configured"]["use_responses_api"] is False
    assert payload["inference"]["responses_api"]["omlx_only"] is True
    assert payload["inference"]["responses_api"]["supported"] is False
    assert "Neo4j" in payload["stores"]["note"]
    assert payload["stores"]["scenario_db"]["exists"] is True


def test_settings_import_from_local_path_and_activate(tmp_path, monkeypatch):
    settings, _cfg = _operator_settings(tmp_path, monkeypatch)
    source = tmp_path / "ATLAS-2026.07.yaml"
    raw = _write(source, _document())
    digest = hashlib.sha256(raw).hexdigest()

    with _dashboard() as port:
        missing = _request(
            port,
            "POST",
            "/api/settings/frameworks/mitre-atlas/import",
            {
                "source_url": "https://example.test/ATLAS-2026.07.yaml",
                "expected_sha256": digest,
                "activate": True,
            },
            _same_origin(port),
        )
        created = _request(
            port,
            "POST",
            "/api/settings/frameworks/mitre-atlas/import",
            {
                "local_path": str(source),
                "source_url": "https://example.test/ATLAS-2026.07.yaml",
                "expected_sha256": digest,
                "activate": True,
            },
            _same_origin(port),
        )
        status_code, status = _request(
            port, "GET", "/api/frameworks/mitre-atlas/status"
        )

    assert missing[0] == 400
    assert "exactly one" in missing[1]["error"]
    assert created[0] == 201
    assert created[1]["import"]["content_version"] == "2026.07"
    assert created[1]["import"]["activated"] is True
    assert created[1]["import"]["already_imported"] is False
    assert status_code == 200
    assert status["active_release"] == "2026.07"
    assert status["counts"]["techniques"] == 2


def test_settings_import_from_base64_rejects_wrong_digest(tmp_path, monkeypatch):
    _operator_settings(tmp_path, monkeypatch)
    raw = yaml.safe_dump(_document(), sort_keys=False).encode("utf-8")
    encoded = base64.b64encode(raw).decode("ascii")
    with _dashboard() as port:
        code, payload = _request(
            port,
            "POST",
            "/api/settings/frameworks/mitre-atlas/import",
            {
                "filename": "ATLAS-2026.07.yaml",
                "content_b64": encoded,
                "source_url": "https://example.test/ATLAS-2026.07.yaml",
                "expected_sha256": "ab" * 32,
                "activate": False,
            },
            _same_origin(port),
        )
    assert code == 400
    assert "SHA-256" in payload["error"]


def test_settings_import_is_loopback_origin_gated(tmp_path, monkeypatch):
    settings, _cfg = _operator_settings(tmp_path, monkeypatch)
    source = tmp_path / "ATLAS-2026.07.yaml"
    _write(source, _document())
    with _dashboard() as port:
        code, payload = _request(
            port,
            "POST",
            "/api/settings/frameworks/mitre-atlas/import",
            {
                "local_path": str(source),
                "source_url": "https://example.test/ATLAS-2026.07.yaml",
            },
            {"Origin": "http://attacker.invalid"},
        )
    assert code == 403
    assert "restricted" in payload["error"]


def test_settings_activate_switches_imported_release(tmp_path, monkeypatch):
    settings, _cfg = _operator_settings(tmp_path, monkeypatch)
    first = tmp_path / "ATLAS-2026.06.yaml"
    second = tmp_path / "ATLAS-2026.07.yaml"
    _write(first, _document("2026.06"))
    _write(second, _document("2026.07"))
    with _dashboard() as port:
        _request(
            port,
            "POST",
            "/api/settings/frameworks/mitre-atlas/import",
            {
                "local_path": str(first),
                "source_url": "https://example.test/first.yaml",
                "activate": True,
            },
            _same_origin(port),
        )
        _request(
            port,
            "POST",
            "/api/settings/frameworks/mitre-atlas/import",
            {
                "local_path": str(second),
                "source_url": "https://example.test/second.yaml",
                "activate": False,
            },
            _same_origin(port),
        )
        code, payload = _request(
            port,
            "POST",
            "/api/settings/frameworks/mitre-atlas/activate",
            {"content_version": "2026.07"},
            _same_origin(port),
        )
    assert code == 200
    assert payload["status"]["active_release"] == "2026.07"
    assert {row["content_version"] for row in payload["status"]["releases"]} == {
        "2026.06",
        "2026.07",
    }


def test_yaml_model_write_keeps_comments_and_requires_restart(tmp_path, monkeypatch):
    settings, cfg = _operator_settings(tmp_path, monkeypatch)
    result = apply_model_settings(
        settings,
        {
            "model": "new-gen",
            "model_judge": "new-judge",
            "model_final_judge": "",
            "use_responses_api": True,
        },
    )
    text = cfg.read_text()
    assert result["restart_required"] is True
    assert "# keep this comment" in text
    assert "model: new-gen  # default" in text
    assert "model_judge: new-judge" in text
    parsed = load_yaml_settings(cfg)
    assert parsed["omlx_model"] == "new-gen"
    assert parsed["omlx_model_judge"] == "new-judge"
    assert parsed["omlx_use_responses_api"] is True
    assert parsed["dgx_model"] == "Qwen/leave-alone"
    assert "use_responses_api: true" in text
    # The running Settings object is not live-mutated.
    assert settings.omlx_model == "keep-me-model"


def test_yaml_model_write_blocked_by_env(tmp_path, monkeypatch):
    settings, cfg = _operator_settings(tmp_path, monkeypatch)
    monkeypatch.setenv("OMLX_MODEL", "env-wins")
    with _dashboard() as port:
        code, payload = _request(
            port,
            "POST",
            "/api/settings/models",
            {"model": "should-fail"},
            _same_origin(port),
        )
    assert code == 409
    assert "OMLX_MODEL" in payload["error"]
    assert "keep-me-model" in cfg.read_text()


def test_settings_view_is_wired_in_the_deck():
    html = (
        Path(__file__).resolve().parents[1]
        / "contingency_atlas"
        / "web"
        / "static"
        / "index.html"
    ).read_text(encoding="utf-8")
    assert 'data-view="settings"' in html
    assert 'id="view-settings"' in html
    assert '"settings"' in html
    assert "/api/settings/frameworks/mitre-atlas/import" in html
    assert "set-use-responses" in html
    assert "Use Responses API instead of chat completions" in html


def test_update_yaml_inserts_missing_keys(tmp_path):
    cfg = tmp_path / "thin.yaml"
    cfg.write_text("provider: omlx\nomlx:\n  timeout: 30\n")
    update_yaml_model_settings(cfg, model="inserted-model")
    parsed = load_yaml_settings(cfg)
    assert parsed["omlx_model"] == "inserted-model"
    assert parsed["omlx_timeout"] == 30.0
