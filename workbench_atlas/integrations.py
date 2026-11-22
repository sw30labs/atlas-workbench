"""Offline staging packages and durable acknowledgments for downstream systems.

This module never opens a network connection or changes source records. A
customer configures and executes the destination import separately. Local
acknowledgments are attributed operator reports, not verified remote receipts.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import sqlite3
import uuid
import zipfile
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from workbench_atlas.exports import ExportArtifact, build_snapshot

MAX_ROWS = 2_000
MAX_PACKAGE_BYTES = 20 * 1024 * 1024
MAX_PACKAGES = 300
SCHEMA_VERSION = 1
FIELDS = ("source_system", "external_id", "record_type", "record_id", "revision_hash",
          "display_name", "classification", "source_status", "record_json")
REQUIRED_FIELDS = {"source_system", "external_id", "record_type", "revision_hash", "record_json"}
NOTICE = (
    "Offline staging only. No data has been transmitted. Configure and review the destination "
    "staging table and transform mapping before external execution. Acknowledgments are "
    "operator-reported outcomes and never change Atlas records or public evidence approvals."
)


class IntegrationError(ValueError):
    """An actionable staging, mapping or acknowledgment error."""


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _now():
    return datetime.now(UTC).isoformat()


def _text(value, name, limit=160):
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > limit:
        raise IntegrationError(f"{name} must be nonempty text, up to {limit} characters.")
    return value.strip()


def _path(settings):
    path = Path(settings.context_db_path).expanduser().with_name("integration-outbox.db").resolve()
    for source in (settings.context_db_path, settings.scenario_db_path, settings.evidence_db_path,
                   settings.emerging_risks_path, settings.scenario_export_path):
        source = Path(source).expanduser().resolve()
        if path == source or (path.exists() and source.exists() and path.samefile(source)):
            raise IntegrationError("The integration outbox must be separate from every source store.")
    return path


def _check_schema(conn):
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "integration_meta" not in tables:
        raise IntegrationError("The integration outbox belongs to another application or is incomplete.")
    versions = conn.execute("SELECT schema_version FROM integration_meta").fetchall()
    if len(versions) != 1 or versions[0][0] != SCHEMA_VERSION:
        raise IntegrationError("Unsupported integration outbox schema version.")
    if not {"integration_packages", "integration_rows", "integration_receipts"} <= tables:
        raise IntegrationError("The integration outbox is incomplete.")


@contextmanager
def _store(settings, *, write=False, require_existing=False):
    path = _path(settings)
    if require_existing and not path.exists():
        raise IntegrationError("This integration package is unavailable to the current operator.")
    if not path.exists() and not write:
        yield None
        return
    if write and not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        except FileExistsError:
            pass
    conn = sqlite3.connect(path.as_uri() + ("?mode=rw" if write else "?mode=ro"), uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        if write:
            conn.execute("BEGIN IMMEDIATE")
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not tables:
                statements = [
                    "CREATE TABLE integration_meta(schema_version INTEGER NOT NULL)",
                    "INSERT INTO integration_meta VALUES(1)",
                    "CREATE TABLE integration_packages(id TEXT PRIMARY KEY, operation_key TEXT UNIQUE NOT NULL, "
                    "owner TEXT NOT NULL, created_at TEXT NOT NULL, adapter TEXT NOT NULL, parent_id TEXT, "
                    "payload_json TEXT NOT NULL, payload_hash TEXT NOT NULL)",
                    "CREATE TABLE integration_rows(package_id TEXT NOT NULL, external_id TEXT NOT NULL, "
                    "revision_hash TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', "
                    "target_id TEXT, message TEXT, acknowledged_at TEXT, "
                    "PRIMARY KEY(package_id,external_id))",
                    "CREATE TABLE integration_receipts(sequence INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "package_id TEXT NOT NULL, actor TEXT NOT NULL, recorded_at TEXT NOT NULL, receipt_json TEXT NOT NULL)",
                    "CREATE TRIGGER integration_receipts_no_update BEFORE UPDATE ON integration_receipts "
                    "BEGIN SELECT RAISE(ABORT, 'integration receipts are append-only'); END",
                    "CREATE TRIGGER integration_receipts_no_delete BEFORE DELETE ON integration_receipts "
                    "BEGIN SELECT RAISE(ABORT, 'integration receipts are append-only'); END",
                ]
                for statement in statements:
                    conn.execute(statement)
            _check_schema(conn)
            os.chmod(path, 0o600)
        else:
            conn.execute("PRAGMA query_only=ON")
            conn.execute("BEGIN")
            _check_schema(conn)
        yield conn
        if write:
            conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def _mapping(adapter, mapping):
    if not isinstance(adapter, str) or adapter not in {"servicenow", "generic_rest"}:
        raise IntegrationError("Choose servicenow or generic_rest for the staging adapter.")
    if mapping is not None and (not isinstance(mapping, dict) or
            set(mapping) - {"source_system", "staging_table", "fields"}):
        raise IntegrationError("Mapping supports only source_system, staging_table and fields.")
    mapping = mapping or {}
    source = mapping.get("source_system", "workbench-atlas")
    if not isinstance(source, str) or not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_.-]{0,79}", source):
        raise IntegrationError("source_system must be a stable identifier of 1 to 80 characters.")
    table = mapping.get("staging_table", "u_workbench_atlas_import" if adapter == "servicenow" else "atlas_records")
    if not isinstance(table, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", table):
        raise IntegrationError("staging_table must be a plain table identifier, not a URL.")
    fields = mapping.get("fields", {key: ("u_" + key if adapter == "servicenow" else key) for key in FIELDS})
    if not isinstance(fields, dict) or set(fields) - set(FIELDS) or not REQUIRED_FIELDS <= set(fields):
        raise IntegrationError("Field mapping must preserve source_system, external_id, record_type, revision_hash and record_json.")
    if any(not isinstance(v, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", v) for v in fields.values()):
        raise IntegrationError("Destination field names must be plain column identifiers.")
    if len(set(fields.values())) != len(fields):
        raise IntegrationError("Destination field names must be unique.")
    if any(v in {"sys_id", "approval", "approved", "review_status", "state"} for v in fields.values()):
        raise IntegrationError("Map into staging columns; workflow approval and target identity fields are not permitted.")
    return {"source_system": source, "staging_table": table, "fields": fields}


def _rows(snapshot, source_system):
    rows = []
    for kind, records in snapshot["tables"].items():
        id_field = {"scenarios": "scenario_id", "evidence_events": "event_id"}.get(kind)
        if kind.startswith("organization_"):
            id_field = "id"
        if not id_field:
            continue
        for record in records:
            record_id = _text(record.get(id_field), "Source record ID", 300)
            external_id = source_system + ":" + kind + ":" + record_id
            rows.append({"source_system": source_system, "external_id": external_id,
                         "record_type": kind, "record_id": record_id, "revision_hash": _hash(record),
                         "display_name": str(record.get("name") or record.get("title") or record.get("headline")
                                             or record.get("statement") or record_id),
                         "classification": record.get("classification") or record.get("provenance_class") or record.get("origin") or "unclassified",
                         "source_status": record.get("review_status") or record.get("status") or "not_recorded",
                         "record_json": _json(record)})
    rows.sort(key=lambda row: row["external_id"])
    if not rows or len(rows) > MAX_ROWS:
        raise IntegrationError(f"Choose a nonempty scope with at most {MAX_ROWS:,} staged records.")
    if len({row["external_id"] for row in rows}) != len(rows):
        raise IntegrationError("Source identity is ambiguous; repair duplicate IDs before staging.")
    return rows


def _get(conn, package_id, actor):
    package_id = _text(package_id, "Package ID")
    row = conn.execute("SELECT * FROM integration_packages WHERE id=? AND owner=?", (package_id, actor)).fetchone() if conn else None
    if row is None:
        raise IntegrationError("This integration package is unavailable to the current operator.")
    payload = json.loads(row["payload_json"])
    if _hash(payload) != row["payload_hash"]:
        raise IntegrationError("Integration package integrity check failed.")
    return dict(row), payload


def _summary(conn, row, payload, *, include_records=False):
    states = [dict(item) for item in conn.execute(
        "SELECT external_id,revision_hash,status,target_id,message,acknowledged_at FROM integration_rows "
        "WHERE package_id=? ORDER BY external_id", (row["id"],))]
    counts = {state: sum(item["status"] == state for item in states) for state in ("pending", "accepted", "rejected")}
    result = {"package_id": row["id"], "adapter": row["adapter"], "created_at": row["created_at"],
              "parent_id": row["parent_id"], "actor": row["owner"], "manifest": payload["manifest"],
              "counts": counts, "rows": states, "notice": NOTICE}
    if include_records:
        result["records"] = payload["records"]
    return result


def _put(conn, *, actor, adapter, mapping, snapshot, rows, parent_id=None):
    key = _hash({"actor": actor, "adapter": adapter, "mapping": mapping, "parent_id": parent_id,
                 "rows": rows, "selection": snapshot["manifest"]["selection"]})
    existing = conn.execute("SELECT * FROM integration_packages WHERE operation_key=?", (key,)).fetchone()
    if existing:
        row, payload = _get(conn, existing["id"], actor)
        return _summary(conn, row, payload, include_records=True)
    if conn.execute("SELECT COUNT(*) FROM integration_packages").fetchone()[0] >= MAX_PACKAGES:
        raise IntegrationError("The integration outbox is full. Preserve its audit history and ask the operator to rotate the outbox.")
    package_id, created = "atlas-package-" + uuid.uuid4().hex, _now()
    manifest = {"schema_version": 1, "package_id": package_id, "created_at": created, "adapter": adapter,
                "parent_id": parent_id, "snapshot_id": snapshot["manifest"]["snapshot_id"],
                "source_generated_at": snapshot["manifest"]["generated_at"],
                "selection": snapshot["manifest"]["selection"], "classification": snapshot["manifest"]["classification"],
                "mapping": mapping, "records_sha256": _hash(rows), "record_count": len(rows),
                "coalesce_fields": [mapping["fields"]["source_system"], mapping["fields"]["external_id"]],
                "revision_field": mapping["fields"]["revision_hash"], "notice": NOTICE}
    payload = {"manifest": manifest, "records": rows, "snapshot": snapshot}
    encoded = _json(payload)
    if len(encoded.encode()) > MAX_PACKAGE_BYTES:
        raise IntegrationError("This staging package exceeds 20 MB. Select fewer scenarios.")
    conn.execute("INSERT INTO integration_packages VALUES(?,?,?,?,?,?,?,?)",
                 (package_id, key, actor, created, adapter, parent_id, encoded, _hash(payload)))
    conn.executemany("INSERT INTO integration_rows(package_id,external_id,revision_hash) VALUES(?,?,?)",
                     [(package_id, row["external_id"], row["revision_hash"]) for row in rows])
    row, payload = _get(conn, package_id, actor)
    return _summary(conn, row, payload, include_records=True)


def preview_integration(settings, *, actor, selection, adapter="servicenow", mapping=None):
    """Freeze a selected source snapshot into an attributed durable outbox package."""
    actor = _text(actor, "Operator name")
    mapping = _mapping(adapter, mapping)
    if not isinstance(selection, dict) or set(selection) - {"scenario_ids", "include_context"}:
        raise IntegrationError("Integration selection supports explicit scenario_ids and include_context only.")
    if not isinstance(selection.get("scenario_ids"), list) or not selection["scenario_ids"]:
        raise IntegrationError("Select explicit nonempty scenario IDs before staging.")
    snapshot = build_snapshot(settings, **selection)
    rows = _rows(snapshot, mapping["source_system"])
    with _store(settings, write=True) as conn:
        return _put(conn, actor=actor, adapter=adapter, mapping=mapping, snapshot=snapshot, rows=rows)


def list_integrations(settings):
    """Return bounded package metadata and row statuses, without staged record content."""
    with _store(settings) as conn:
        if conn is None:
            return {"packages": [], "notice": NOTICE}
        packages = []
        for row in conn.execute("SELECT * FROM integration_packages ORDER BY created_at DESC,id LIMIT ?", (MAX_PACKAGES,)):
            item, payload = _get(conn, row["id"], row["owner"])
            packages.append(_summary(conn, item, payload))
        return {"packages": packages, "notice": NOTICE}


def _csv(rows, fields):
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\r\n")
    writer.writerow(fields.values())
    for row in rows:
        values = []
        for field in fields:
            value = str(row[field])
            if value.lstrip().startswith(("=", "+", "-", "@", "\t", "\r", "\n")) or value.startswith(("\t", "\r", "\n")):
                value = "'" + value
            values.append(value)
        writer.writerow(values)
    return output.getvalue().encode("utf-8-sig")


def download_integration(settings, *, package_id, actor):
    actor = _text(actor, "Operator name")
    with _store(settings) as conn:
        _, payload = _get(conn, package_id, actor)
    rows, manifest = payload["records"], payload["manifest"]
    mapped = [{target: row[source] for source, target in manifest["mapping"]["fields"].items()} for row in rows]
    documents = {
        "records.json": (_json({"records": mapped}) + "\n").encode(),
        "records.csv": _csv(rows, manifest["mapping"]["fields"]),
        "canonical-records.json": (_json({"records": [
            {"record_type": row["record_type"], "external_id": row["external_id"],
             "revision_hash": row["revision_hash"], "record": json.loads(row["record_json"])}
            for row in rows]}) + "\n").encode(),
        "source-snapshot-manifest.json": (_json(payload["snapshot"]["manifest"]) + "\n").encode(),
        "acknowledgments-template.json": (_json({"package_id": package_id, "acknowledgments": [
            {"external_id": row["external_id"], "revision_hash": row["revision_hash"],
             "status": "", "target_id": "", "message": ""} for row in rows]}) + "\n").encode(),
        "README.txt": (NOTICE + "\n\nUse records.json for exact text. CSV formula-like cells carry a protective apostrophe.\n"
            "Create the destination staging table and transform mapping before import. Coalesce on source_system + external_id; "
            "use revision_hash to recognize a previously applied revision. These are customer transform responsibilities.\n"
            "Do not use a generic HTTP success as proof of successful row transformation. Review the destination outcome for each row.\n"
            "Fill acknowledgment status with accepted or rejected; accepted requires target_id, rejected requires message.\n"
            "No credentials, destination URL or executable transmission script is included.\n").encode(),
    }
    checksums = {name: hashlib.sha256(content).hexdigest() for name, content in documents.items()}
    documents["manifest.json"] = (_json({**manifest, "file_checksums": checksums}) + "\n").encode()
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in documents.items():
            info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, content)
    return ExportArtifact(package_id + ".zip", "application/zip", out.getvalue(), {"records": len(rows)}, manifest["snapshot_id"])


def acknowledge_integration(settings, *, package_id, acknowledgments, actor):
    actor = _text(actor, "Operator name")
    if not isinstance(acknowledgments, list) or not 1 <= len(acknowledgments) <= MAX_ROWS:
        raise IntegrationError("Provide a nonempty list of per-record acknowledgments.")
    with _store(settings, write=True, require_existing=True) as conn:
        row, payload = _get(conn, package_id, actor)
        states = {item["external_id"]: dict(item) for item in conn.execute(
            "SELECT * FROM integration_rows WHERE package_id=?", (package_id,))}
        normalized, seen = [], set()
        for item in acknowledgments:
            if not isinstance(item, dict) or set(item) - {"external_id", "revision_hash", "status", "target_id", "message"}:
                raise IntegrationError("Acknowledgments support external_id, revision_hash, status, target_id and message only.")
            external_id = _text(item.get("external_id"), "External ID", 500)
            if external_id in seen or external_id not in states:
                raise IntegrationError("Acknowledgment contains an unknown or duplicate external ID.")
            seen.add(external_id)
            prior = states[external_id]
            if item.get("revision_hash") != prior["revision_hash"]:
                raise IntegrationError("Acknowledgment revision hash does not match the frozen source revision.")
            if not isinstance(item.get("status"), str) or item["status"] not in {"accepted", "rejected"}:
                raise IntegrationError("Acknowledgment status must be accepted or rejected; it is never a research approval.")
            target = _text(item.get("target_id"), "Destination record ID", 300) if item["status"] == "accepted" else ""
            message = _text(item.get("message"), "Rejection reason", 1000) if item["status"] == "rejected" else ""
            cleaned = {"external_id": external_id, "revision_hash": prior["revision_hash"],
                       "status": item["status"], "target_id": target, "message": message}
            if prior["status"] != "pending":
                if any(prior[key] != cleaned[key] for key in ("status", "target_id", "message")):
                    raise IntegrationError("An acknowledged outcome is immutable. Create an explicit retry package for rejected rows.")
            else:
                normalized.append(cleaned)
        when = _now()
        for item in normalized:
            conn.execute("UPDATE integration_rows SET status=?,target_id=?,message=?,acknowledged_at=? "
                         "WHERE package_id=? AND external_id=?",
                         (item["status"], item["target_id"], item["message"], when, package_id, item["external_id"]))
        if normalized:
            conn.execute("INSERT INTO integration_receipts(package_id,actor,recorded_at,receipt_json) VALUES(?,?,?,?)",
                         (package_id, actor, when, _json(normalized)))
        return _summary(conn, row, payload)


def retry_integration(settings, *, package_id, selected_ids, actor):
    actor = _text(actor, "Operator name")
    if not isinstance(selected_ids, list) or not selected_ids or any(not isinstance(v, str) for v in selected_ids):
        raise IntegrationError("Select explicit unacknowledged or rejected external IDs to retry.")
    if len(selected_ids) != len(set(selected_ids)):
        raise IntegrationError("Retry selection contains duplicate external IDs.")
    with _store(settings, write=True, require_existing=True) as conn:
        row, payload = _get(conn, package_id, actor)
        states = {r["external_id"]: r["status"] for r in conn.execute(
            "SELECT external_id,status FROM integration_rows WHERE package_id=?", (package_id,))}
        if any(states.get(key) not in {"pending", "rejected"} for key in selected_ids):
            raise IntegrationError("Retry selection includes unknown or already accepted rows.")
        records = [r for r in payload["records"] if r["external_id"] in set(selected_ids)]
        # Original snapshot metadata retains provenance internally. Downloads
        # include only retry rows, plus the source manifest without record bodies.
        return _put(conn, actor=actor, adapter=row["adapter"], mapping=payload["manifest"]["mapping"],
                    snapshot=payload["snapshot"], rows=records, parent_id=package_id)
