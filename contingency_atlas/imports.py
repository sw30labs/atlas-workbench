"""Preview and confirm structured imports into the private organization store.

Uploaded content is untrusted data, never an instruction or public evidence.
Previews are bounded, expiring, actor/workspace-bound memory snapshots. The
context module owns typed validation, references, revisions and atomic writes.
"""

from __future__ import annotations

import copy
import csv
import hashlib
import io
import json
import re
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import ValidationError

from contingency_atlas.config import Settings

MAX_IMPORT_BYTES = 2 * 1024 * 1024
MAX_IMPORT_ROWS = 500
PREVIEW_TTL_SECONDS = 30 * 60
MAX_PREVIEWS = 20
_SERVER_FIELDS = {"kind", "schema_version", "revision", "archived", "created_at", "updated_at"}


class ImportError(ValueError):
    """Safe validation or stale-preview error for the local import workflow."""


@dataclass
class _Preview:
    actor: str
    workspace: str
    created_at: float
    candidates: dict[str, dict]
    submitted: set[str]
    inflight: bool = False


_PREVIEWS: dict[str, _Preview] = {}
_LOCK = threading.Lock()


def _actor(actor: str) -> str:
    if not isinstance(actor, str) or not actor.strip() or len(actor.strip()) > 160:
        raise ImportError("A reviewer name of 1 to 160 characters is required.")
    return actor.strip()


def _workspace(settings: Settings) -> str:
    if not settings.context_enabled:
        raise ImportError("Enable the separate organization workspace before importing organization data.")
    target = settings.context_db_path.expanduser().resolve()
    public_paths = (settings.scenario_db_path, settings.evidence_db_path, settings.emerging_risks_path,
                    settings.scenario_export_path)
    for path in public_paths:
        path = path.expanduser().resolve()
        if path == target or (path.exists() and target.exists() and target.samefile(path)):
            raise ImportError("Organization imports require a separate private database.")
    return hashlib.sha256(str(target).encode()).hexdigest()


def _purge(now: float) -> None:
    for key in list(_PREVIEWS):
        preview = _PREVIEWS[key]
        if not preview.inflight and now - preview.created_at >= PREVIEW_TTL_SECONDS:
            del _PREVIEWS[key]


def _unique_object(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ImportError("JSON contains duplicate field names; remove the ambiguity before importing.")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ImportError(f"JSON numeric constant {value} is not supported.")


def _parse(content: str, format: str) -> tuple[list[dict], list[str]]:
    if not isinstance(content, str) or not content.strip():
        raise ImportError("Provide CSV or JSON text to preview.")
    try:
        content_size = len(content.encode("utf-8"))
    except UnicodeError as exc:
        raise ImportError("The import contains invalid Unicode text.") from exc
    if content_size > MAX_IMPORT_BYTES:
        raise ImportError("The import exceeds the 2 MB limit.")
    content = content.lstrip("\ufeff")
    if format == "json":
        try:
            value = json.loads(content, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
        except (json.JSONDecodeError, RecursionError) as exc:
            raise ImportError("The JSON could not be parsed.") from exc
        if isinstance(value, dict):
            if set(value) == {"records"}:
                value = value["records"]
            elif "tables" in value or "manifest" in value:
                raise ImportError("Import an explicit organization-record array, not a public research export or whole snapshot.")
            else:
                value = [value]
        if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
            raise ImportError("JSON must contain an array of organization record objects.")
        columns = sorted({key for row in value for key in (row.get("data") if isinstance(row.get("data"), dict) else row)})
        rows = value
    elif format == "csv":
        try:
            # Only conventional text table delimiters; no formula evaluation.
            try:
                dialect = csv.Sniffer().sniff(content[:8192], delimiters=",;\t")
            except csv.Error:
                dialect = csv.excel
            reader = csv.DictReader(io.StringIO(content, newline=""), dialect=dialect, strict=True)
            columns = reader.fieldnames or []
            if not columns or any(not column.strip() for column in columns) or len(set(columns)) != len(columns):
                raise ImportError("CSV requires unique, non-empty column headers.")
            rows = []
            for row in reader:
                if len(rows) >= MAX_IMPORT_ROWS:
                    raise ImportError(f"An import can contain at most {MAX_IMPORT_ROWS} records.")
                if None in row or any(value is None for value in row.values()):
                    raise ImportError("A CSV row has a different number of values than its header.")
                if any(value.strip() for value in row.values()):
                    rows.append(row)
        except (csv.Error, UnicodeError) as exc:
            raise ImportError("The CSV could not be parsed; check quoting and delimiters.") from exc
    else:
        raise ImportError("Choose csv or json for an organization import.")
    if not rows or len(rows) > MAX_IMPORT_ROWS:
        raise ImportError(f"An import requires 1 to {MAX_IMPORT_ROWS} records.")
    if len(columns) > 100:
        raise ImportError("The import contains more than 100 columns.")
    return rows, columns


def _mapping(mapping: dict[str, str] | None, columns: list[str]) -> dict[str, str] | None:
    if mapping is None:
        return None
    if not isinstance(mapping, dict) or not mapping or len(mapping) > 100:
        raise ImportError("field_mapping must map source column names to target field names.")
    targets = []
    for source, target in mapping.items():
        if not isinstance(source, str) or source not in columns or not isinstance(target, str):
            raise ImportError("The field mapping contains an unknown source column or invalid target.")
        if target:
            targets.append(target)
    if not targets or len(set(targets)) != len(targets):
        raise ImportError("Map at least one field and give each target field a unique source.")
    return mapping


def _array_field(schema: dict) -> bool:
    return schema.get("type") == "array" or any(item.get("type") == "array" for item in schema.get("anyOf", []))


def _normalize(row: dict, *, kind: str | None, source: str, mapping: dict[str, str] | None) -> tuple[dict, list[str]]:
    from contingency_atlas.context import RECORD_MODELS, validate_record

    envelope = "data" in row
    if envelope and (not isinstance(row["data"], dict) or set(row) - {"kind", "data", "expected_revision"}):
        raise ImportError("Record envelopes may contain only kind, data and expected_revision.")
    data = dict(row["data"] if envelope else row)
    if mapping is not None:
        data = {target: data[column] for column, target in mapping.items() if target and column in data}
    incoming_kind = row.get("kind") if envelope else data.pop("kind", None)
    if kind and incoming_kind and kind != incoming_kind:
        raise ImportError("The row kind differs from the selected import kind.")
    record_kind = kind or incoming_kind
    if record_kind not in RECORD_MODELS:
        raise ImportError("Choose a supported organization record kind for this row.")
    expected_revision = row.get("expected_revision") if envelope else data.pop("expected_revision", None)
    if expected_revision in (None, ""):
        expected_revision = None
    elif isinstance(expected_revision, bool) or not re.fullmatch(r"[0-9]+", str(expected_revision)):
        raise ImportError("expected_revision must be a non-negative integer.")
    else:
        expected_revision = int(expected_revision)
    record_id = data.pop("id", None)
    if record_id is None or record_id == "":
        if expected_revision not in (None, 0):
            raise ImportError("An update requires an existing record id.")
        record_id = f"ctx-{record_kind}-{uuid.uuid4().hex[:16]}"
    if not isinstance(record_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", record_id):
        raise ImportError("id must be a stable identifier of at most 128 permitted characters.")
    server_fields = set(data) & _SERVER_FIELDS
    if server_fields:
        raise ImportError("Do not import server-owned version, archive or timestamp fields. Use expected_revision explicitly for updates.")
    model = RECORD_MODELS[record_kind]
    fields = model.model_json_schema().get("properties", {})
    for key, value in list(data.items()):
        if isinstance(value, str) and _array_field(fields.get(key, {})):
            if not value.strip():
                data[key] = []
            elif value.lstrip().startswith("["):
                try:
                    data[key] = json.loads(value, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
                except json.JSONDecodeError as exc:
                    raise ImportError(f"{key} must be a JSON array or semicolon-separated values.") from exc
            else:
                data[key] = [part.strip() for part in value.split(";") if part.strip()]
        elif value == "" and key in model.model_fields and not model.model_fields[key].is_required():
            data.pop(key)
    if data.get("source") in (None, ""):
        data["source"] = source
    if data.get("classification") in (None, ""):
        data["classification"] = "organization"
    normalized = validate_record(record_kind, {"id": record_id, **data})
    warnings = ["Relationships and current record revisions are checked atomically when you confirm."]
    if expected_revision is not None:
        warnings.append("This row requests an update at the explicitly supplied revision.")
    return {"kind": record_kind, "data": normalized,
            "expected_revision": expected_revision}, warnings


def preview_import(
    settings: Settings, *, content: str, format: str, actor: str, source: str,
    kind: str | None = None, field_mapping: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Validate and freeze organization candidates; never persist uploaded content.

    ``field_mapping`` maps source columns to target fields. When supplied,
    unmapped source columns are ignored. No incoming approval is accepted into
    the public research stores. This workflow only writes private context.
    """
    actor = _actor(actor)
    workspace = _workspace(settings)
    if not isinstance(source, str) or not source.strip() or len(source.strip()) > 10000:
        raise ImportError("Declare the organizational source of these records.")
    if kind is not None and not isinstance(kind, str):
        raise ImportError("kind must name a supported organization record type.")
    rows, columns = _parse(content, format)
    mapping = _mapping(field_mapping, columns)
    candidates = []
    for index, row in enumerate(rows):
        candidate = {"candidate_id": f"row-{index + 1}", "row_index": index,
                     "row_number": index + (2 if format == "csv" else 1),
                     "record": None, "errors": [], "warnings": []}
        try:
            record, warnings = _normalize(row, kind=kind, source=source.strip(), mapping=mapping)
            candidate.update(record=record, warnings=warnings,
                             label=record["data"].get("name") or record["data"].get("title")
                             or record["data"].get("statement") or record["data"]["id"])
        except ValidationError as exc:
            candidate["errors"] = [f"{'.'.join(map(str, error['loc'])) or 'record'}: {error['msg']}"
                                   for error in exc.errors(include_url=False, include_input=False)]
        except (ImportError, ValueError, TypeError) as exc:
            candidate["errors"] = [str(exc)]
        candidates.append(candidate)
    ids: dict[str, list[dict]] = {}
    for candidate in candidates:
        if candidate["record"]:
            ids.setdefault(candidate["record"]["data"]["id"], []).append(candidate)
    for duplicates in ids.values():
        if len(duplicates) > 1:
            for candidate in duplicates:
                candidate["errors"].append("The same record id appears more than once in this import.")
    now = time.monotonic()
    token = "import-" + uuid.uuid4().hex
    with _LOCK:
        _purge(now)
        if len(_PREVIEWS) >= MAX_PREVIEWS:
            raise ImportError("Too many import previews are open; finish one or wait for an older preview to expire.")
        _PREVIEWS[token] = _Preview(actor, workspace, now,
                                    {row["candidate_id"]: copy.deepcopy(row) for row in candidates}, set())
    return {
        "preview_id": token, "actor": actor, "source": source.strip(), "format": format,
        "kind": kind, "columns": columns, "field_mapping": mapping,
        "classification": "organization_workspace", "candidates": candidates,
        "counts": {"total": len(candidates), "valid": sum(not row["errors"] for row in candidates),
                   "invalid": sum(bool(row["errors"]) for row in candidates)},
        "expires_at": (datetime.now(UTC) + timedelta(seconds=PREVIEW_TTL_SECONDS)).isoformat(),
        "notice": "Syntax preview only. Confirmed selections enter the separate organization workspace with the named actor; relationships and revisions must still pass validation.",
    }


def submit_import(settings: Settings, *, preview_id: str, selected_ids: list[str], actor: str) -> dict[str, Any]:
    """Write selected frozen candidates atomically through the context boundary."""
    from contingency_atlas.context import import_records

    actor = _actor(actor)
    workspace = _workspace(settings)
    if not isinstance(preview_id, str) or not isinstance(selected_ids, list) or not selected_ids:
        raise ImportError("Choose one or more valid candidates from an import preview.")
    if len(selected_ids) > MAX_IMPORT_ROWS or any(not isinstance(value, str) for value in selected_ids):
        raise ImportError("The import selection is invalid.")
    selected_ids = list(dict.fromkeys(selected_ids))
    with _LOCK:
        _purge(time.monotonic())
        preview = _PREVIEWS.get(preview_id)
        if preview is None or preview.actor != actor or preview.workspace != workspace:
            raise ImportError("The import preview is unavailable for this actor or workspace; prepare it again.")
        if preview.inflight:
            raise ImportError("This import preview is already being submitted.")
        if any(value not in preview.candidates or value in preview.submitted for value in selected_ids):
            raise ImportError("The selection contains unknown or already submitted candidates.")
        candidates = [preview.candidates[value] for value in selected_ids]
        if any(row["errors"] or row["record"] is None for row in candidates):
            raise ImportError("Resolve the selected rows' validation errors before importing.")
        records = [copy.deepcopy(row["record"]) for row in candidates]
        preview.inflight = True
    succeeded = False
    try:
        imported = import_records(settings, records, actor=actor)
        succeeded = True
    except (ValueError, OSError) as exc:
        raise ImportError(f"No records were imported: {exc}") from exc
    finally:
        with _LOCK:
            if succeeded:
                preview.submitted.update(selected_ids)
            preview.inflight = False
    return {"ok": True, "preview_id": preview_id, "actor": actor, "imported_count": len(imported),
            "imported": imported, "submitted_ids": selected_ids,
            "notice": "Records were saved only in the separate organization workspace."}
