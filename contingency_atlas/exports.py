"""Read-only, provenance-preserving exports of a selected research portfolio.

No exporter invokes inference, migrates a store, or changes a review decision.
The canonical JSON package is the lossless interchange format; the other formats
are views of the same snapshot. Each SQLite store is read in a transaction.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import sqlite3
import sys
import zipfile
from contextlib import ExitStack, closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape, quoteattr

from contingency_atlas import __version__
from contingency_atlas.config import Settings, get_settings
from contingency_atlas.provenance import is_public_http_url

EXPORT_SCHEMA_VERSION = "1.0"
MAX_RECORDS = 100_000
MAX_REGISTER_BYTES = 32 * 1024 * 1024
FORMATS = ("json", "csv", "xlsx", "pdf", "docx", "pptx")

# Deliberately enumerated, rather than exporting every table in a database.
# New enterprise context tables can be added here without opening arbitrary SQL.
OPTIONAL_TABLES = {
    "compound_scenarios": {"compound_id", "risk_ids", "status"},
    "compound_graduations": {"compound_id", "scenario_id"},
    "score_realizations": {"realization_id", "compound_id", "pair_id"},
    "compound_join_runs": {"run_id"},
    "qa_logs": {"scenario_id"},
    "signpost_assessments": {"scenario_id", "review_status"},
    "framework_mapping_proposals": {"scenario_id", "framework", "content_version"},
    "framework_releases": {"framework", "content_version", "source_sha256"},
    "scenario_mapping_revisions": {"scenario_id"},
    "analogues": {"pair_id"},
    "review_suggestions": {"record_kind", "record_id"},
    "review_research": {"record_kind", "record_id"},
}

NOTICE = (
    "Scenarios are research hypotheses, not observed incidents or enterprise loss "
    "forecasts. Public-series compound scores measure public-evidence co-activation. "
    "Review, lifecycle and provenance labels are retained; absence of a label is "
    "not approval. Synthetic material remains fictional."
)


class ExportError(ValueError):
    """A safe, operator-facing validation or read error."""


@dataclass(frozen=True)
class ExportArtifact:
    filename: str
    media_type: str
    content: bytes
    record_counts: dict[str, int]
    snapshot_id: str


def _json_bytes(value: Any) -> bytes:
    try:
        return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2,
                           allow_nan=False) + "\n").encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ExportError("Export data contains an unsupported or non-finite value.") from exc


def _hash(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _read_table(conn: sqlite3.Connection, name: str, required: set[str],
                *, optional: bool = False) -> list[dict[str, Any]] | None:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    if not exists:
        if optional:
            return None
        raise ExportError(f"Required export table '{name}' is missing; initialize the stores first.")
    columns = {row[1] for row in conn.execute(f'PRAGMA table_info("{name}")')}
    if not required <= columns:
        raise ExportError(f"Export table '{name}' has an incomplete schema; migrate it first.")
    rows = conn.execute(f'SELECT * FROM "{name}" LIMIT ?', (MAX_RECORDS + 1,)).fetchall()
    if len(rows) > MAX_RECORDS:
        raise ExportError(f"Table '{name}' exceeds the {MAX_RECORDS:,}-record export limit.")
    return [dict(row) for row in rows]


def _read_stores(settings: Settings) -> tuple[dict[str, list[dict]], list[str]]:
    tables: dict[str, list[dict]] = {}
    absent: list[str] = []
    try:
        path = settings.emerging_risks_path
        if not path.is_file():
            raise ExportError("The risk register is missing; initialize the stores first.")
        with path.open("rb") as source:
            raw = source.read(MAX_REGISTER_BYTES + 1)
        if len(raw) > MAX_REGISTER_BYTES:
            raise ExportError("The risk register exceeds the 32 MB export limit.")
        risks = json.loads(raw)
        if not isinstance(risks, list) or any(not isinstance(row, dict) for row in risks):
            raise ExportError("The risk register must contain a JSON array of records.")
        if len(risks) > MAX_RECORDS:
            raise ExportError("The risk register exceeds the export record limit.")
        tables["risks"] = risks
        with ExitStack() as stack:
            connections = []
            for label, db_path in (("scenario", settings.scenario_db_path),
                                   ("evidence", settings.evidence_db_path)):
                if not db_path.is_file():
                    raise ExportError(f"The {label} store is missing; initialize the stores first.")
                conn = stack.enter_context(closing(sqlite3.connect(
                    db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5
                )))
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA query_only=ON")
                conn.execute("BEGIN")
                connections.append(conn)
            scenario, evidence = connections
            tables["scenarios"] = _read_table(
                scenario, "scenarios", {"scenario_id", "origin", "status", "evidence_refs"}
            ) or []
            tables["evidence_events"] = _read_table(
                evidence, "evidence_events", {"event_id", "review_status", "provenance_class", "source_url"}
            ) or []
            for name, required in OPTIONAL_TABLES.items():
                rows = _read_table(scenario, name, required, optional=True)
                if rows is None:
                    absent.append(name)
                else:
                    tables[name] = rows
            # Decision histories live beside their objects in both databases.
            decisions = []
            for label, conn in (("scenario", scenario), ("evidence", evidence)):
                rows = _read_table(conn, "review_decisions", {"decision_id", "kind", "record_id"}, optional=True)
                if rows is None:
                    absent.append(label + "_review_decisions")
                else:
                    decisions.extend({**row, "export_source_store": label} for row in rows)
            tables["review_decisions"] = decisions
            # A register replacement during the read must not silently mix versions.
            with path.open("rb") as source:
                after = source.read(MAX_REGISTER_BYTES + 1)
            if raw != after:
                raise ExportError("The risk register changed during export; try again.")
    except ExportError:
        raise
    except (OSError, sqlite3.Error, UnicodeError, json.JSONDecodeError) as exc:
        raise ExportError("A research store could not be read. Check its integrity and try again.") from exc
    return tables, absent


def _ids(values: list[str] | None, label: str) -> set[str] | None:
    if values is None:
        return None
    if not isinstance(values, (list, tuple)) or not values:
        raise ExportError(f"{label} must be a non-empty list, or null for all records.")
    if len(values) > MAX_RECORDS or any(not isinstance(v, str) or not v.strip() for v in values):
        raise ExportError(f"{label} contains invalid identifiers.")
    return set(values)


def _index(rows: list[dict], field: str, *, legacy_risk: bool = False) -> dict[str, dict]:
    out = {}
    for row in rows:
        value = row.get(field)
        if not value and legacy_risk:
            from contingency_atlas.guard_hook import mint_risk_id
            title = row.get("issue") or row.get("title")
            if not isinstance(title, str) or not title.strip():
                raise ExportError("A risk has no stable identifier or title.")
            value = mint_risk_id(title)
            row["risk_id"] = value
            row["export_identifier_origin"] = "derived_from_legacy_title"
        if not isinstance(value, str) or not value.strip() or value in out:
            raise ExportError(f"Records contain a missing or duplicate {field}; repair identifiers first.")
        out[value] = row
    return out


def _refs(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            parsed = [value]
        value = parsed if isinstance(parsed, list) else [parsed]
    if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
        raise ExportError("A relationship field contains invalid references.")
    return list(dict.fromkeys(value))


def build_snapshot(
    settings: Settings, *, scenario_ids: list[str] | None = None,
    risk_ids: list[str] | None = None, evidence_ids: list[str] | None = None,
    compound_ids: list[str] | None = None, status: str | None = None,
    origin: str | None = None, include_context: bool = False,
) -> dict[str, Any]:
    """Return ``{manifest, tables}`` with canonical names and related records.

    All selectors null exports the whole catalog. Any explicit identifier list
    selects only those roots plus their dependencies. Empty lists are errors.
    ``status`` and ``origin`` restrict scenario roots only; all statuses on
    linked evidence and review history remain visible. Optional missing tables
    are declared in the manifest, never confused with a failed table read.
    """
    started = datetime.now(UTC).isoformat()
    selectors = {
        "scenario_ids": _ids(scenario_ids, "scenario_ids"),
        "risk_ids": _ids(risk_ids, "risk_ids"),
        "evidence_ids": _ids(evidence_ids, "evidence_ids"),
        "compound_ids": _ids(compound_ids, "compound_ids"),
    }
    if not isinstance(include_context, bool):
        raise ExportError("include_context must be true or false.")
    if include_context and not selectors["scenario_ids"]:
        raise ExportError("Organization context requires an explicit non-empty scenario selection.")
    for name, value in (("status", status), ("origin", origin)):
        if value is not None and (not isinstance(value, str) or not value.strip() or len(value) > 100):
            raise ExportError(f"{name} must be a non-empty label.")
    raw, absent = _read_stores(settings)
    raw["compound_scenarios"] = [row for row in raw.get("compound_scenarios", [])
                                   if row.get("status") != "superseded"]
    indexes = {
        "scenarios": _index(raw["scenarios"], "scenario_id"),
        "risks": _index(raw["risks"], "risk_id", legacy_risk=True),
        "evidence_events": _index(raw["evidence_events"], "event_id"),
        "compound_scenarios": _index(raw["compound_scenarios"], "compound_id"),
    }
    root_map = dict(zip(selectors, indexes))
    full = all(value is None for value in selectors.values()) and status is None and origin is None
    selected = {name: set(rows) if full else set() for name, rows in indexes.items()}
    for key, ids in selectors.items():
        if ids is not None:
            unknown = ids - indexes[root_map[key]].keys()
            if unknown:
                raise ExportError(f"{key} contains unknown or unavailable identifiers.")
            selected[root_map[key]].update(ids)
    if status is not None or origin is not None:
        candidates = selected["scenarios"] if scenario_ids is not None else set(indexes["scenarios"])
        selected["scenarios"] = {sid for sid in candidates
            if (status is None or indexes["scenarios"][sid].get("status") == status)
            and (origin is None or indexes["scenarios"][sid].get("origin") == origin)}
    # Graduated scenarios carry their compound and its constituent risks.
    for row in raw.get("compound_graduations", []):
        if row["scenario_id"] in selected["scenarios"] and row["compound_id"] in indexes["compound_scenarios"]:
            selected["compound_scenarios"].add(row["compound_id"])
    for cid in tuple(selected["compound_scenarios"]):
        selected["risks"].update(rid for rid in _refs(indexes["compound_scenarios"][cid].get("risk_ids"))
                                  if rid in indexes["risks"])
    links: list[dict] = []
    warnings: list[str] = []
    urls: dict[str, list[str]] = {}
    for eid, row in indexes["evidence_events"].items():
        if row.get("source_url"):
            urls.setdefault(row["source_url"], []).append(eid)

    def add_evidence(kind: str, identifier: str, reference: str, relation: str) -> None:
        matches = [reference] if reference in indexes["evidence_events"] else urls.get(reference, [])
        resolution = "resolved" if len(matches) == 1 else "ambiguous" if matches else "unresolved"
        if resolution != "resolved":
            warnings.append(f"{kind} {identifier}: {relation} has an {resolution} reference.")
        for target in matches or [None]:
            if target:
                selected["evidence_events"].add(target)
            links.append({"source_type": kind, "source_id": identifier, "relation": relation,
                          "target_type": "evidence_event", "target_id": target,
                          "reference": reference, "resolution": resolution})

    for table, kind, fields in (
        ("scenarios", "scenario", ("evidence_refs", "signpost_evidence_refs", "signpost_checked_evidence_refs")),
        ("risks", "risk", ("evidence_refs",)),
    ):
        for identifier in sorted(selected[table]):
            for field in fields:
                for ref in _refs(indexes[table][identifier].get(field)):
                    add_evidence(kind, identifier, ref, field)
    for cid in sorted(selected["compound_scenarios"]):
        for rid in _refs(indexes["compound_scenarios"][cid].get("risk_ids")):
            links.append({"source_type": "compound", "source_id": cid, "relation": "member",
                          "target_type": "risk", "target_id": rid, "reference": rid,
                          "resolution": "resolved" if rid in indexes["risks"] else "unresolved"})

    sids, cids = selected["scenarios"], selected["compound_scenarios"]
    pairs = {indexes["compound_scenarios"][cid].get("pair_id") for cid in cids} - {None, ""}
    tables: dict[str, list[dict]] = {}
    for name, rows in raw.items():
        if name in indexes or name in ("framework_releases", "compound_join_runs"):
            continue
        if name == "compound_graduations":
            keep = lambda row: row.get("scenario_id") in sids or row.get("compound_id") in cids
        elif name in ("score_realizations", "analogues"):
            keep = lambda row: row.get("compound_id") in cids or row.get("pair_id") in pairs
        elif name in ("review_suggestions", "review_research", "review_decisions"):
            kind_ids = {"event": selected["evidence_events"], "evidence": selected["evidence_events"],
                        "risk": selected["risks"], "signpost": sids, "scenario": sids}
            keep = lambda row, known=kind_ids: row.get("record_id") in known.get(row.get("record_kind") or row.get("kind"), set())
        else:
            keep = lambda row: row.get("scenario_id") in sids
        tables[name] = rows if full else [row for row in rows if keep(row)]
        if name == "framework_mapping_proposals":
            from contingency_atlas.frameworks.mappings import mapping_state
            for row in tables[name]:
                row["export_mapping_state"] = mapping_state(
                    row, indexes["scenarios"].get(row.get("scenario_id"), {})
                )
                if row.get("status") == "approved" and row["export_mapping_state"] != "current":
                    warnings.append("An approved framework mapping is stale for its scenario's current content or revision.")
        if name in ("signpost_assessments", "framework_mapping_proposals"):
            for row in tables[name]:
                for field in ("evidence_refs", "considered_refs", "evidence_refs_json"):
                    for ref in _refs(row.get(field)):
                        add_evidence(name, str(row.get("id") or row.get("proposal_id")), ref, field)
    release_keys = {(r.get("framework"), r.get("content_version"))
                    for r in tables.get("framework_mapping_proposals", [])}
    if "framework_releases" in raw:
        tables["framework_releases"] = [r for r in raw["framework_releases"]
                                         if (r["framework"], r["content_version"]) in release_keys]
    present_releases = {(r["framework"], r["content_version"]) for r in tables.get("framework_releases", [])}
    if release_keys - present_releases:
        warnings.append("A framework mapping's pinned release provenance is unavailable in this snapshot.")
    run_ids = {r.get("run_id") for r in tables.get("score_realizations", [])}
    run_ids.update(indexes["compound_scenarios"][cid].get("run_id") for cid in cids)
    if "compound_join_runs" in raw:
        tables["compound_join_runs"] = [r for r in raw["compound_join_runs"] if r["run_id"] in run_ids]
    if include_context:
        if not getattr(settings, "context_enabled", False):
            raise ExportError("Organization context is disabled for this workspace.")
        if not sids:
            raise ExportError("No selected scenarios remain for the organization-context export.")
        try:
            from contingency_atlas.context import export_context
            context = export_context(settings, scenario_ids=sorted(sids))
        except (ImportError, ValueError, OSError, sqlite3.Error) as exc:
            raise ExportError("Organization context could not be exported; check its configuration and integrity.") from exc
        if not isinstance(context, dict) or any(
            not isinstance(name, str) or not re.fullmatch(r"[a-z_]+", name)
            or not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows)
            for name, rows in context.items()
        ):
            raise ExportError("Organization context returned an invalid export snapshot.")
        tables.update({"organization_" + name: rows for name, rows in context.items()})
        for row in context.get("evidence_assessments", []):
            if isinstance(row.get("evidence_id"), str) and row["evidence_id"]:
                add_evidence("organization_evidence_assessments", str(row.get("id") or ""),
                             row["evidence_id"], "evidence_id")
    for name, ids in selected.items():
        tables[name] = [indexes[name][identifier] for identifier in sorted(ids)]
    # All optional-review and explicitly scoped private references have now
    # joined the evidence closure. Retain their histories in the same snapshot.
    for name in ("review_suggestions", "review_research", "review_decisions"):
        if name in raw:
            kind_ids = {"event": selected["evidence_events"], "evidence": selected["evidence_events"],
                        "risk": selected["risks"], "signpost": sids, "scenario": sids}
            tables[name] = raw[name] if full else [row for row in raw[name]
                if row.get("record_id") in kind_ids.get(row.get("record_kind") or row.get("kind"), set())]
    tables["relationships"] = links
    tables = {name: sorted(rows, key=lambda r: _json_bytes(r)) for name, rows in sorted(tables.items())}
    checksum = _hash(_json_bytes(tables))
    manifest = {
        "schema_version": EXPORT_SCHEMA_VERSION, "application_version": __version__,
        "snapshot_id": "export-" + checksum[:16], "generated_at": datetime.now(UTC).isoformat(),
        "read_started_at": started,
        "consistency": "Per-store read transactions; no cross-store atomic transaction. Register checked for concurrent replacement.",
        "selection": {**{k: sorted(v) if v is not None else None for k, v in selectors.items()},
                      "status": status, "origin": origin, "include_context": include_context},
        "record_counts": {name: len(rows) for name, rows in tables.items()},
        "tables_sha256": checksum,
        "table_checksums": {name: _hash(_json_bytes(rows)) for name, rows in tables.items()},
        "missing_optional_tables": sorted(absent), "warnings": sorted(set(warnings)),
        "notice": NOTICE,
        "classification": "includes_organization_context" if include_context else "public_research_and_synthetic",
        "taxonomy_versions": sorted({str(r["taxonomy_version"]) for rows in tables.values()
                                     for r in rows if r.get("taxonomy_version")}),
        "algorithm_versions": sorted({str(r["algorithm_version"]) for rows in tables.values()
                                      for r in rows if r.get("algorithm_version")}),
    }
    return {"manifest": manifest, "tables": tables}


def _columns(rows: list[dict]) -> list[str]:
    columns = {str(key) for row in rows for key in row}
    preferred = ("scenario_id", "risk_id", "event_id", "compound_id", "title", "issue", "headline",
                 "description", "risk_category", "status", "origin", "review_status", "provenance_class",
                 "source_type", "source_url", "evidence_refs")
    return [key for key in preferred if key in columns] + sorted(columns - set(preferred)) or ["no_records"]


def _cell(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return _json_bytes(value).decode().strip()
    if value is not None and not isinstance(value, (str, bool, int, float)):
        raise ExportError("Export contains a value that cannot be represented in a spreadsheet.")
    return value


def _csv_cell(value: Any) -> str:
    value = _cell(value)
    text = "" if value is None else str(value)
    # Human-view CSV deliberately changes suspicious text. JSON preserves its
    # exact canonical value. Quoting alone does not prevent formula execution.
    if isinstance(value, str) and (text[:1] in "=+-@\t\r\n＝＋－＠" or text.lstrip()[:1] in "=+-@＝＋－＠") and text:
        return "'" + text
    return text


def _csv_zip(snapshot: dict) -> bytes:
    output = io.BytesIO()
    members: dict[str, bytes] = {}
    for name, rows in snapshot["tables"].items():
        text = io.StringIO(newline="")
        writer = csv.writer(text, quoting=csv.QUOTE_ALL, lineterminator="\r\n")
        columns = _columns(rows)
        writer.writerow([_csv_cell(key) for key in columns])
        writer.writerows([_csv_cell(row.get(key)) for key in columns] for row in rows)
        members[name + ".csv"] = text.getvalue().encode("utf-8-sig")
    manifest = {**snapshot["manifest"], "format": "csv-zip",
                "csv_text_policy": "Formula-like text is prefixed with an apostrophe. Use canonical JSON for exact machine interchange. Re-saving CSV in spreadsheet software can change its safety properties.",
                "file_checksums": {name: _hash(data) for name, data in members.items()}}
    members["manifest.json"] = _json_bytes(manifest)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(members.items()):
            entry = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(entry, data)
    return output.getvalue()


def _xlsx(snapshot: dict) -> bytes:
    from openpyxl import Workbook
    from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    book = Workbook()
    book.remove(book.active)
    sheets = {"Read me": [{"field": key, "value": value} for key, value in snapshot["manifest"].items()]}
    priority = ("scenarios", "risks", "compound_scenarios", "evidence_events", "relationships")
    for name in (*priority, *(name for name in snapshot["tables"] if name not in priority)):
        if snapshot["tables"].get(name):
            sheets[name] = snapshot["tables"][name]
    for name, rows in sheets.items():
        sheet = book.create_sheet(name[:31])
        columns = _columns(rows)
        for number, key in enumerate(columns, 1):
            if len(key) > 32767 or ILLEGAL_CHARACTERS_RE.search(key):
                raise ExportError("A field name cannot fit safely in Excel. Export canonical JSON instead.")
            cell = sheet.cell(1, number, key)
            cell.data_type = "s"
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="183D4A")
            cell.alignment = Alignment(wrap_text=True, vertical="top")
            sheet.column_dimensions[get_column_letter(number)].width = (
                100 if name == "Read me" and key == "value" else
                52 if key in ("title", "issue", "headline", "description") else min(44, max(22, len(key) + 3))
            )
        for row_number, row in enumerate(rows, 2):
            height = 32
            for column_number, key in enumerate(columns, 1):
                value = _cell(row.get(key))
                if isinstance(value, str):
                    if len(value) > 32767 or ILLEGAL_CHARACTERS_RE.search(value):
                        raise ExportError("A field cannot fit safely in Excel. Export canonical JSON instead.")
                    cell = sheet.cell(row_number, column_number)
                    cell.value = value
                    cell.data_type = "s"  # Never turn untrusted strings into formulas.
                else:
                    cell = sheet.cell(row_number, column_number, value)
                cell.alignment = Alignment(vertical="top", wrap_text=True)
                cell.font = Font(name="Calibri", size=11, color="243746")
                width = sheet.column_dimensions[get_column_letter(column_number)].width
                lines = sum(max(1, (len(line) + int(width) - 1) // int(width))
                            for line in str(value or "").splitlines())
                height = max(height, 15 * lines + 8)
            sheet.row_dimensions[row_number].height = min(409, height)
        sheet.row_dimensions[1].height = 32
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = sheet.dimensions
        sheet.sheet_view.showGridLines = False
    output = io.BytesIO()
    book.save(output)
    return output.getvalue()


def _pdf(snapshot: dict) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer

    output = io.BytesIO()
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle("AtlasBody", fontName="Helvetica", fontSize=10, leading=15,
                             textColor=colors.HexColor("#233642"), spaceAfter=8,
                             splitLongWords=True, alignment=TA_LEFT))
    styles.add(ParagraphStyle("AtlasMeta", parent=styles["AtlasBody"], fontSize=8, leading=12,
                             textColor=colors.HexColor("#506572")))
    styles["Heading1"].textColor = colors.HexColor("#153F4B")
    styles["Heading1"].fontSize = 22
    styles["Heading1"].leading = 27
    styles["Heading2"].textColor = colors.HexColor("#146C72")
    styles["Heading2"].fontSize = 12
    styles["Heading2"].leading = 17
    story = []

    def para(value: Any, style: str = "AtlasBody") -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                para(f"{key.replace('_', ' ')}: {_cell(item) if item is not None else 'Not recorded'}", style)
            return
        text = str(_cell(value) if value is not None else "Not recorded")
        # ReportLab's base fonts cannot represent arbitrary Unicode. Replace
        # unsupported glyphs explicitly rather than silently drawing black boxes.
        text = text.replace("\u2011", "-").replace("\u2013", "-").replace("\u2014", "-")
        text = text.encode("cp1252", "backslashreplace").decode("cp1252")
        text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", " ", text)
        # Bounded paragraphs allow arbitrarily long evidence and narratives to
        # flow across pages without a giant unbreakable table row.
        for paragraph in text.splitlines() or [""]:
            story.append(Paragraph(escape(paragraph) or "&#160;", styles[style]))

    def section(label: str, value: Any) -> None:
        para(label, "Heading2")
        if value in (None, "", []):
            para("Not recorded")
        elif isinstance(value, list):
            for item in value:
                para(item)
        else:
            para(value)

    manifest, tables = snapshot["manifest"], snapshot["tables"]
    para("CONTINGENCY ATLAS", "AtlasMeta")
    para("Operational risk review pack", "Heading1")
    para("Evidence, assumptions and preparedness", "AtlasBody")
    para(f"Prepared {manifest['generated_at'][:10]} | {manifest['snapshot_id']}", "AtlasMeta")
    story.append(Spacer(1, 0.15 * inch))
    para(NOTICE)
    section("Included in this pack", ", ".join(
        f"{len(tables.get(table, []))} {label[:-1] if len(tables.get(table, [])) == 1 else label}" for table, label in
        (("scenarios", "scenarios"), ("risks", "signals"), ("compound_scenarios", "compounds"),
         ("evidence_events", "evidence records"))))
    scope = {key.replace("_ids", " selection").replace("_", " "): ", ".join(value) if isinstance(value, list) else value
             for key, value in manifest["selection"].items() if value not in (None, False)}
    section("Selection", scope or "All catalog records, retaining their recorded review and lifecycle states.")
    if manifest.get("classification") == "includes_organization_context":
        para("INCLUDES ORGANIZATION CONTEXT", "Heading2")
        para("This selected pack includes private organization context. Use the organization's sharing and retention policy.")
    if manifest["warnings"]:
        section("References requiring attention", manifest["warnings"])
    para("This pack preserves recorded review states. It does not approve records, create control decisions, or certify an organization's exposure.", "AtlasMeta")
    story.append(Spacer(1, 12))
    para("Snapshot integrity", "Heading2")
    para("Canonical tables SHA-256: " + manifest["tables_sha256"], "AtlasMeta")
    para("Use the JSON package for complete machine-readable scores, relationships and review history.", "AtlasMeta")
    for scenario in tables["scenarios"]:
        story.append(PageBreak())
        para("SCENARIO BRIEF", "AtlasMeta")
        para(scenario.get("title") or scenario["scenario_id"], "Heading1")
        para(f"ID: {scenario['scenario_id']} | Origin: {scenario.get('origin') or 'unknown'} | Lifecycle: {scenario.get('status') or 'not recorded'}", "AtlasMeta")
        if scenario.get("origin") == "synthetic_fixture":
            para("FICTIONAL SYNTHETIC SCENARIO", "Heading2")
        for label, field in (("The hypothesis", "description"), ("Business relevance", "operational_impact"),
                             ("Financial impact as recorded", "financial_impact_detail"),
                             ("Transmission and dependencies", "transmission_mechanism"),
                             ("Affected capabilities", "affected_capabilities"),
                             ("Assumptions to challenge", "challenged_assumptions"),
                             ("Strengthening signposts", "strengthening_signposts"),
                             ("Weakening signposts", "weakening_signposts"),
                             ("Defensive options", "defensive_options"),
                             ("Resilience opportunities", "resilience_opportunities"),
                             ("Preparation strategy", "preparation_strategy"),
                             ("Discussion prompts", "discussion_prompts"),
                             ("Evidence references", "evidence_refs")):
            value = scenario.get(field)
            if isinstance(value, str) and value.startswith("["):
                try:
                    value = json.loads(value)
                except json.JSONDecodeError:
                    pass
            section(label, value)
        section("Recorded scoring and review", {key: scenario.get(key) for key in
                ("urgency_score", "last_scored", "judge_score", "signpost_status", "signpost_review_status")})
    for table, title, id_field in (("risks", "Signals", "risk_id"),
                                    ("compound_scenarios", "Compound candidates", "compound_id")):
        if not tables.get(table):
            continue
        story.append(PageBreak())
        para(title, "Heading1")
        for row in tables[table]:
            para(row.get("issue") or row.get("title") or row[id_field], "Heading2")
            for key, value in row.items():
                para(f"{key.replace('_', ' ')}: {_cell(value)}", "AtlasMeta" if key.endswith("id") else "AtlasBody")
    if tables["evidence_events"]:
        story.append(PageBreak())
        para("Evidence and provenance", "Heading1")
        for event in tables["evidence_events"]:
            para(event.get("headline") or event["event_id"], "Heading2")
            for key in ("event_id", "provenance_class", "review_status", "source_publisher", "source_title",
                        "source_date", "instrument_status", "retrieved_at", "source_locator",
                        "independently_authored_summary", "research_inference", "rights_review",
                        "reviewed_at", "taxonomy_version", "content_fingerprint", "provenance_note"):
                if event.get(key) not in (None, ""):
                    para(f"{key.replace('_', ' ')}: {event[key]}")
            url = event.get("source_url")
            if url:
                if is_public_http_url(url):
                    story.append(Paragraph(f'<link href={quoteattr(url)} color="#146C72">Source link</link>', styles["AtlasBody"]))
                para(url, "AtlasMeta")
    for table, title in (("framework_mapping_proposals", "Framework mapping review"),
                         ("signpost_assessments", "Signpost review history"),
                         ("review_decisions", "Recorded review decisions")):
        if tables.get(table):
            story.append(PageBreak())
            para(title, "Heading1")
            for row in tables[table]:
                para({key: value for key, value in row.items()
                      if key not in ("before_json", "after_json")}, "AtlasMeta")
                story.append(Spacer(1, 8))
    for table, rows in tables.items():
        if table.startswith("organization_") and rows:
            story.append(PageBreak())
            para("Organization context: " + table.removeprefix("organization_").replace("_", " "), "Heading1")
            for index, row in enumerate(rows, 1):
                para(_record_title(row, index), "Heading2")
                for key, value in row.items():
                    for line in _readable_lines(value):
                        para(f"{_field_label(key)}: {line}")
                story.append(Spacer(1, 10))

    def page_header(canvas, doc) -> None:
        canvas.saveState()
        width, height = doc.pagesize
        canvas.setStrokeColor(colors.HexColor("#D4E1E5"))
        canvas.line(doc.leftMargin, height - 42, width - doc.rightMargin, height - 42)
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(colors.HexColor("#506572"))
        canvas.drawString(doc.leftMargin, height - 32, "CONTINGENCY ATLAS  |  RESEARCH REVIEW")
        canvas.drawString(doc.leftMargin, 30, manifest["snapshot_id"])
        canvas.drawRightString(width - doc.rightMargin, 30, f"Page {doc.page}")
        canvas.restoreState()

    doc = SimpleDocTemplate(output, pagesize=(595.28, 841.89), rightMargin=48, leftMargin=48,
                            topMargin=64, bottomMargin=54, title="Contingency Atlas review pack",
                            author="Contingency Atlas")
    doc.build(story, onFirstPage=page_header, onLaterPages=page_header)
    return output.getvalue()


def _field_label(value: str) -> str:
    return str(value).removesuffix("_json").replace("_", " ").capitalize()


def _record_title(row: dict, index: int) -> str:
    return str(next((row[key] for key in ("name", "title", "issue", "headline", "statement", "claim",
                                        "record_id", "id", "scenario_id", "event_id") if row.get(key)),
                    f"Record {index}"))


def _readable_lines(value: Any) -> list[str]:
    """Express nested records as labeled paragraphs without embedding raw JSON."""
    if isinstance(value, str) and value.strip().startswith(("[", "{")):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            pass
    if isinstance(value, dict):
        return [f"{_field_label(key)}: {line}" for key, item in value.items()
                for line in _readable_lines(item)] or ["None recorded"]
    if isinstance(value, list):
        return [line for item in value for line in _readable_lines(item)] or ["None recorded"]
    if value is None or value == "":
        return ["Not recorded"]
    if isinstance(value, bool):
        return ["Yes" if value else "No"]
    return str(value).splitlines() or ["Not recorded"]


def _office_text(value: Any) -> str:
    """Keep Unicode text; make XML-forbidden code points visible as escapes."""
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]",
                  lambda match: f"\\u{ord(match.group()):04x}", str(value))


def _document_sections(snapshot: dict):
    """Editable report content from the frozen snapshot, without new inference."""
    manifest, tables = snapshot["manifest"], snapshot["tables"]
    private = manifest.get("classification") == "includes_organization_context"
    yield "Scope and interpretation", [
        ("Purpose", "Review the selected hypotheses, evidence and recorded organization decisions."),
        ("Prepared", manifest["generated_at"]),
        ("Classification", "Includes private organization context" if private else "Public research and synthetic records"),
        ("Sharing", "Apply the organization's sharing and retention policy." if private else "Recorded provenance and review states remain part of this snapshot."),
        ("Snapshot", manifest["snapshot_id"]), ("Selection", {k: v for k, v in manifest.get("selection", {}).items() if v is not None}),
        ("Included records", {k: v for k, v in manifest.get("record_counts", {}).items() if v}),
        ("Interpretation", manifest.get("notice") or NOTICE),
        ("References requiring attention", manifest.get("warnings") or ["No reference warnings recorded in this snapshot."]),
        ("Canonical tables SHA 256", manifest["tables_sha256"]),
        ("Complete structured history", "Use the canonical JSON export for all scoring, relationship and database details. Editing this document does not change Atlas records or the original snapshot checksum."),
    ]
    scenario_fields = ("scenario_id", "origin", "status", "description", "operational_impact",
                       "transmission_mechanism", "affected_capabilities", "challenged_assumptions",
                       "strengthening_signposts", "weakening_signposts", "defensive_options",
                       "resilience_opportunities", "preparation_strategy", "discussion_prompts",
                       "evidence_refs", "urgency_score", "last_scored", "judge_score",
                       "signpost_status", "signpost_review_status")
    evidence_fields = ("event_id", "provenance_class", "review_status", "source_publisher", "source_title",
                       "source_date", "source_url", "instrument_status", "retrieved_at", "source_locator",
                       "independently_authored_summary", "research_inference", "rights_review", "reviewed_at",
                       "taxonomy_version", "content_fingerprint", "provenance_note")
    order = ["organization_review_summary", "organization_changes", "organization_research_changes", "organization_review_notes",
             "scenarios", "risks", "compound_scenarios", "evidence_events", "framework_mapping_proposals",
             "signpost_assessments", "review_decisions"]
    order += [name for name in tables if name.startswith("organization_") and name not in order]
    for table in order:
        for index, row in enumerate(tables.get(table, []), 1):
            title = _record_title(row, index)
            fields = scenario_fields if table == "scenarios" else evidence_fields if table == "evidence_events" else tuple(row)
            pairs = [("Record", title)]
            for field in fields:
                if row.get(field) not in (None, "", [], {}):
                    pairs.append(("Business relevance" if field == "operational_impact" else _field_label(field), row[field]))
            section = "Scenario brief" if table == "scenarios" else "Evidence and provenance" if table == "evidence_events" else _field_label(table)
            yield f"{section}   {title}", pairs


def _docx(snapshot: dict) -> bytes:
    from docx import Document
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Inches, Pt, RGBColor

    document = Document()
    section = document.sections[0]
    section.page_width, section.page_height = Inches(8.27), Inches(11.69)
    section.left_margin = section.right_margin = Inches(0.75)
    section.top_margin, section.bottom_margin = Inches(0.8), Inches(0.7)
    for style_name, size in (("Normal", 11), ("Title", 25), ("Heading 1", 16), ("Heading 2", 12), ("Header", 8), ("Footer", 8)):
        style = document.styles[style_name]
        style.font.name, style.font.size, style.font.color.rgb = "Arial", Pt(size), RGBColor(0, 0, 0)
        style.paragraph_format.space_after = Pt(7 if style_name == "Normal" else 10)
        # Some python-docx distributions ship a blue border in the Title
        # template. The report uses plain black typography on every runtime.
        for border in list(style.element.iter(qn("w:pBdr"))):
            border.getparent().remove(border)
    document.styles["Normal"].paragraph_format.line_spacing = 1.12
    document.styles["Heading 1"].paragraph_format.space_before = Pt(15)
    document.styles["Heading 1"].paragraph_format.keep_with_next = True
    document.add_paragraph("Operational risk review pack", "Title")
    section.header.paragraphs[0].text = "CONTINGENCY ATLAS"
    manifest = snapshot["manifest"]
    footer = section.footer.paragraphs[0]
    classification = "Private organization context" if manifest.get("classification") == "includes_organization_context" else "Research review"
    footer.add_run(f"{manifest['snapshot_id']}   {classification}   Page ")
    field = OxmlElement("w:fldSimple")
    field.set(qn("w:instr"), "PAGE")
    footer._p.append(field)
    document.core_properties.title = "Contingency Atlas operational risk review"
    document.core_properties.author = "Contingency Atlas"
    document.core_properties.subject = classification
    count = 0
    for heading, pairs in _document_sections(snapshot):
        document.add_heading(_office_text(heading), level=1)
        for label, value in pairs:
            for index, line in enumerate(_readable_lines(value)):
                count += 1
                if count > 20_000:
                    raise ExportError("This Word report exceeds 20,000 paragraphs. Select fewer records or use JSON.")
                paragraph = document.add_paragraph()
                paragraph.paragraph_format.widow_control = True
                if index == 0:
                    paragraph.add_run(_office_text(label) + ": ").bold = True
                paragraph.add_run(_office_text(line))
    output = io.BytesIO()
    document.save(output)
    return output.getvalue()


def _slide_lines(text: str, *, font_size: float = 20, width: float = 805) -> list[str]:
    """Conservative font-width wrapping, including long identifiers and CJK text."""
    import unicodedata
    from reportlab.pdfbase.pdfmetrics import stringWidth

    def char_width(char):
        if unicodedata.combining(char):
            return 0
        if unicodedata.east_asian_width(char) in {"W", "F"}:
            return font_size
        return stringWidth(char, "Helvetica", font_size) * 1.08

    lines = []
    for logical_line in _office_text(text).splitlines() or [""]:
        current, measured = "", 0.0
        for char in logical_line:
            measured_char = char_width(char)
            if current and measured + measured_char > width:
                # Prefer a recent word boundary while preserving every character.
                cut = current.rfind(" ")
                if cut > len(current) * 0.55:
                    lines.append(current[:cut])
                    current = current[cut + 1:] + char
                    measured = sum(char_width(c) for c in current)
                else:
                    lines.append(current)
                    current, measured = char, measured_char
            else:
                current += char
                measured += measured_char
        lines.append(current)
    return lines


def _pptx(snapshot: dict) -> bytes:
    from pptx import Presentation
    from pptx.dml.color import RGBColor
    from pptx.enum.text import MSO_ANCHOR, MSO_AUTO_SIZE
    from pptx.util import Inches, Pt

    deck = Presentation()
    deck.slide_width, deck.slide_height = Inches(13.333), Inches(7.5)
    deck.core_properties.title = "Contingency Atlas operational risk review"
    deck.core_properties.author = "Contingency Atlas"
    manifest = snapshot["manifest"]
    private = manifest.get("classification") == "includes_organization_context"
    deck.core_properties.subject = "Includes private organization context" if private else "Research review"

    def textbox(slide, x, y, w, height, lines, size, *, bold=False, color="233642"):
        shape = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(height))
        frame = shape.text_frame
        frame.clear()
        frame.word_wrap, frame.auto_size, frame.vertical_anchor = False, MSO_AUTO_SIZE.NONE, MSO_ANCHOR.TOP
        frame.margin_left = frame.margin_right = frame.margin_top = frame.margin_bottom = 0
        for index, line in enumerate(lines):
            paragraph = frame.paragraphs[0] if index == 0 else frame.add_paragraph()
            paragraph.text = _office_text(line)
            paragraph.font.name, paragraph.font.size, paragraph.font.bold = "Arial", Pt(size), bold
            paragraph.font.color.rgb = RGBColor.from_string(color)
            paragraph.line_spacing, paragraph.space_after, paragraph.space_before = 1.1, Pt(0), Pt(0)
        return shape

    def page(title, body, *, cover=False):
        if len(deck.slides) >= 200:
            raise ExportError("This presentation exceeds 200 slides. Select fewer records or use Word or JSON.")
        slide = deck.slides.add_slide(deck.slide_layouts[6])
        title_size = 42 if cover else 32
        title_lines = _slide_lines(title, font_size=title_size, width=805)
        if len(title_lines) > 2:
            title_lines = title_lines[:2]
            title_lines[-1] = title_lines[-1].rstrip()[:-3] + "..."
        textbox(slide, 0.75, 0.52, 11.8, 1.12, title_lines, title_size, bold=True, color="153F4B")
        textbox(slide, 0.75, 1.88, 11.8, 4.68, body, 20)
        footer = f"{manifest['snapshot_id']}   {'PRIVATE ORGANIZATION CONTEXT' if private else 'RESEARCH REVIEW'}   {len(deck.slides)}"
        textbox(slide, 0.75, 7.02, 11.8, 0.25, [footer], 10, color="506572")

    page("Operational risk review pack", [
        "Contingency Atlas",
        "",
        "Selected hypotheses, supporting evidence and recorded decisions",
        "",
        "Includes private organization context" if private else "Public research and synthetic records",
        "Prepared " + str(manifest["generated_at"])[:10],
        "",
        "Editable source material for a review or workshop",
        "Full structured history remains available in the JSON export",
    ], cover=True)
    for heading, pairs in _document_sections(snapshot):
        lines = []
        for label, value in pairs:
            for index, line in enumerate(_readable_lines(value)):
                lines.extend(_slide_lines((label + ": " if index == 0 else "") + line))
        for offset in range(0, len(lines), 13):
            page(heading + (" continued" if offset else ""), lines[offset:offset + 13])
    output = io.BytesIO()
    deck.save(output)
    return output.getvalue()


def export_data(settings: Settings, *, format: str = "json", **selection: Any) -> ExportArtifact:
    """Build a fresh snapshot and render it into one downloadable artifact."""
    if not isinstance(format, str) or format not in FORMATS:
        raise ExportError("Choose json, csv, xlsx, pdf, docx or pptx.")
    snapshot = build_snapshot(settings, **selection)
    return render_snapshot(snapshot, format=format)


def render_snapshot(snapshot: dict, *, format: str = "json") -> ExportArtifact:
    """Render a previously built snapshot, keeping preview/download selections identical."""
    renderers = {"json": (_json_bytes, "application/json", "json"),
                 "csv": (_csv_zip, "application/zip", "zip"),
                 "xlsx": (_xlsx, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "xlsx"),
                 "pdf": (_pdf, "application/pdf", "pdf"),
                 "docx": (_docx, "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "docx"),
                 "pptx": (_pptx, "application/vnd.openxmlformats-officedocument.presentationml.presentation", "pptx")}
    if not isinstance(format, str) or format not in renderers:
        raise ExportError("Choose json, csv, xlsx, pdf, docx or pptx.")
    renderer, media_type, extension = renderers[format]
    try:
        content = renderer(snapshot)
    except ImportError as exc:
        raise ExportError("An export dependency is missing. Reinstall Contingency Atlas with its current dependencies.") from exc
    except ExportError:
        raise
    except Exception as exc:
        raise ExportError("The selected data could not be rendered safely; use canonical JSON or repair the affected fields.") from exc
    manifest = snapshot["manifest"]
    return ExportArtifact(f"contingency-atlas-{manifest['snapshot_id']}.{extension}", media_type,
                          content, manifest["record_counts"], manifest["snapshot_id"])


def main(argv: list[str] | None = None) -> int:
    """``atlas-export --format pdf --scenario-id ID --output review.pdf``."""
    parser = argparse.ArgumentParser(prog="atlas export", description="Export a governed research snapshot without running a model.")
    parser.add_argument("--format", choices=FORMATS, default="json")
    parser.add_argument("--output", type=Path, required=True)
    for kind in ("scenario", "risk", "evidence", "compound"):
        parser.add_argument(f"--{kind}-id", action="append", dest=f"{kind}_ids")
    parser.add_argument("--status")
    parser.add_argument("--origin")
    parser.add_argument("--include-context", action="store_true",
                        help="Include explicitly linked organization context for selected scenario IDs")
    args = parser.parse_args(argv)
    try:
        artifact = export_data(get_settings(), format=args.format, scenario_ids=args.scenario_ids,
                               risk_ids=args.risk_ids, evidence_ids=args.evidence_ids,
                               compound_ids=args.compound_ids, status=args.status, origin=args.origin,
                               include_context=args.include_context)
        # Never overwrite a previously distributed report by accident.
        with args.output.open("xb") as output:
            output.write(artifact.content)
    except (ExportError, OSError) as exc:
        print(f"Export failed: {exc}", file=sys.stderr)
        return 1
    print(f"Exported {artifact.snapshot_id} to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
