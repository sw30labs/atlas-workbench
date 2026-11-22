"""Small HTTP adapter for governed enterprise workflows.

The main server retains loopback and same-origin checks. Preview tokens freeze
an export so a changed store cannot silently change a reviewed download.
"""
from __future__ import annotations

import json
import secrets
import sqlite3
import threading
import time
from collections import OrderedDict
from urllib.parse import parse_qs

from workbench_atlas.exports import ExportError, build_snapshot, render_snapshot

POST_PATHS = {
    "/api/exports/preview", "/api/exports/download",
    "/api/context/save", "/api/context/archive", "/api/context/baseline",
    "/api/context/enable", "/api/jobs/cancel", "/api/jobs/retry",
    "/api/imports/preview", "/api/imports/submit",
    "/api/integrations/preview", "/api/integrations/download",
    "/api/integrations/ack", "/api/integrations/retry",
}


class PreviewCache:
    """Bounded, expiring, in-memory snapshots; never a public URL or disk file."""
    def __init__(self, capacity=16, ttl=900, max_bytes=64_000_000):
        self.capacity, self.ttl, self.max_bytes = capacity, ttl, max_bytes
        self._items = OrderedDict()
        self._lock = threading.Lock()

    def put(self, snapshot, *, owner="local-operator"):
        encoded = json.dumps(snapshot, ensure_ascii=False).encode()
        if len(encoded) > self.max_bytes:
            raise ExportError("This preview is too large. Select fewer records or use the export command.")
        with self._lock:
            now = time.monotonic()
            self._items = OrderedDict((k, v) for k, v in self._items.items() if v[0] > now)
            while self._items and (len(self._items) >= self.capacity or
                    sum(len(v[2]) for v in self._items.values()) + len(encoded) > self.max_bytes):
                self._items.popitem(last=False)
            token = secrets.token_urlsafe(32)
            self._items[token] = (now + self.ttl, owner, encoded)
            return token

    def get(self, token, *, owner="local-operator"):
        if not isinstance(token, str):
            raise ExportError("Preview this selection before downloading.")
        with self._lock:
            item = self._items.get(token)
            if not item or item[0] <= time.monotonic() or item[1] != owner:
                raise ExportError("This preview expired or is unavailable. Preview the selection again.")
            return json.loads(item[2])


PREVIEWS = PreviewCache()


class EnterpriseHandlerMixin:
    def _enterprise_settings(self):
        # Resolve through the server's existing seam for isolated tests/runs.
        from workbench_atlas.web.server import get_settings
        return get_settings()

    def _enterprise_get(self, path):
        if path not in {"/api/context", "/api/context/compare", "/api/jobs", "/api/readiness", "/api/diagnostics", "/api/integrations"}:
            return False
        try:
            settings = self._enterprise_settings()
            if path == "/api/context":
                from workbench_atlas.context import context_payload
                payload = context_payload(settings)
            elif path == "/api/context/compare":
                from workbench_atlas.context import compare_baseline
                query = parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
                payload = compare_baseline(settings, query.get("baseline_id", [""])[0])
            elif path == "/api/integrations":
                from workbench_atlas.integrations import list_integrations
                payload = list_integrations(settings)
            elif path == "/api/diagnostics":
                from workbench_atlas.diagnostics import evidence_quality
                payload = evidence_quality(settings)
            elif path == "/api/jobs":
                from workbench_atlas.web.runner import list_jobs
                payload = {"jobs": list_jobs()}
            else:
                from workbench_atlas.operations import readiness
                payload = readiness(settings)
            self._send_json(payload)
        except (ValueError, OSError, sqlite3.Error) as exc:
            self._send_json({"error": str(exc)}, status=503)
        return True

    def _enterprise_post(self, path, body):
        if path not in POST_PATHS:
            return False
        try:
            settings = self._enterprise_settings()
            raw_actor = body.get("actor", "")
            if not isinstance(raw_actor, str):
                raise ValueError("The operator name must be text.")
            actor = raw_actor.strip()
            if path == "/api/exports/preview":
                selection = body.get("selection", {})
                allowed = {"scenario_ids", "risk_ids", "evidence_ids", "compound_ids", "status", "origin", "include_context", "baseline_id"}
                if not isinstance(selection, dict) or set(selection) - allowed:
                    raise ExportError("The export selection contains unsupported fields.")
                if "baseline_id" in selection:
                    if set(selection) - {"baseline_id", "include_context"}:
                        raise ExportError("Export a baseline comparison separately from a scenario selection.")
                    from workbench_atlas.assessment_reports import build_assessment_snapshot
                    snapshot = build_assessment_snapshot(settings, **selection)
                else:
                    snapshot = build_snapshot(settings, **selection)
                token = PREVIEWS.put(snapshot, owner=self._enterprise_owner())
                self._send_json({"preview_token": token, "manifest": snapshot["manifest"], "expires_in": 900})
            elif path == "/api/exports/download":
                snapshot = PREVIEWS.get(body.get("preview_token"), owner=self._enterprise_owner())
                artifact = render_snapshot(snapshot, format=body.get("format", "pdf"))
                self.send_response(200)
                self.send_header("Content-Type", artifact.media_type)
                self.send_header("Content-Disposition", f'attachment; filename="{artifact.filename}"')
                self.send_header("Content-Length", str(len(artifact.content)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(artifact.content)
            elif path.startswith("/api/integrations/"):
                from workbench_atlas import integrations
                if path.endswith("/preview"):
                    payload = integrations.preview_integration(settings, actor=actor,
                        selection=body.get("selection"), adapter=body.get("adapter", "servicenow"),
                        mapping=body.get("mapping"))
                elif path.endswith("/download"):
                    artifact = integrations.download_integration(settings,
                        package_id=body.get("package_id"), actor=actor)
                    self.send_response(200)
                    self.send_header("Content-Type", artifact.media_type)
                    self.send_header("Content-Disposition", f'attachment; filename="{artifact.filename}"')
                    self.send_header("Content-Length", str(len(artifact.content)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(artifact.content)
                    return True
                elif path.endswith("/ack"):
                    payload = integrations.acknowledge_integration(settings,
                        package_id=body.get("package_id"), acknowledgments=body.get("acknowledgments"), actor=actor)
                else:
                    payload = integrations.retry_integration(settings,
                        package_id=body.get("package_id"), selected_ids=body.get("selected_ids"), actor=actor)
                self._send_json(payload)
            elif path.startswith("/api/imports/"):
                from workbench_atlas.imports import preview_import, submit_import
                if path == "/api/imports/preview":
                    payload = preview_import(settings, content=body.get("content", ""),
                        format=body.get("format", "csv"), actor=actor, source=body.get("source", ""),
                        kind=body.get("kind"), field_mapping=body.get("field_mapping"))
                else:
                    payload = submit_import(settings, preview_id=body.get("preview_id", ""),
                        selected_ids=body.get("selected_ids", []), actor=actor)
                self._send_json(payload)
            elif path.startswith("/api/context/"):
                from workbench_atlas import context
                if not actor:
                    raise ValueError("Enter your name to record this change.")
                if path in {"/api/context/save", "/api/context/archive"} and not isinstance(body.get("kind"), str):
                    raise ValueError("Choose a supported organization record type.")
                if path == "/api/context/save" and not isinstance(body.get("record"), dict):
                    raise ValueError("The organization record must be an object.")
                if path == "/api/context/save":
                    payload = context.save_record(settings, body.get("kind"), body.get("record", {}),
                        actor=actor, expected_revision=body.get("expected_revision"))
                elif path == "/api/context/archive":
                    payload = context.archive_record(settings, body.get("kind"), body.get("record_id"),
                        actor=actor, expected_revision=body.get("expected_revision"))
                elif path == "/api/context/baseline":
                    payload = context.create_baseline(settings, body.get("name", ""), actor=actor,
                        expected_revision=body.get("expected_revision"), scenario_ids=body.get("scenario_ids"))
                else:
                    from workbench_atlas.web.context_setup import enable_context
                    payload = enable_context(settings, actor=actor)
                self._send_json(payload)
            else:
                from workbench_atlas.web import runner
                if path == "/api/jobs/cancel":
                    payload = runner.cancel_job(body.get("job_id", ""), actor=actor)
                else:
                    job_id, error = runner.retry_job(body.get("job_id", ""), actor=actor)
                    if error:
                        raise ValueError(error)
                    payload = {"id": job_id}
                self._send_json({"job": payload})
        except (ValueError, OSError, sqlite3.Error) as exc:
            status = 409 if "revision" in str(exc).lower() or "changed" in str(exc).lower() else 400
            self._send_json({"error": str(exc)}, status=status)
        return True

    def _enterprise_owner(self):
        return getattr(self, "atlas_actor", "local-operator")
