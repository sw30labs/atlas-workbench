"""Stager pod: write batch.json and the handoff payloads. Never touches a store.

Organization-context payloads are mixed ``{"records": [{kind, data}]}`` JSON
for ``imports.preview_import(format="json")``, split under its 2 MB / 500
record limits in dependency order. External evidence is emitted as research
material for the existing draft-first intake path plus pending reference
drafts. Organization scenarios, internal losses and taxonomy crosswalk rows
are ordinary organization-context records (agreed 2026-10-18).
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from ..categories import TAXONOMY_VERSION, derive_category
from ..imports import MAX_IMPORT_BYTES, MAX_IMPORT_ROWS
from .linker import short_hash
from .models import ORG_CONTEXT_ROUTES, Candidate

# Same order as context.import_records: referenced kinds come first.
IMPORT_ORDER = ("services", "mechanisms", "org_scenarios", "taxonomy_crosswalk", "dependencies", "controls",
                "assumptions", "exposures", "exercises", "actions", "assessments", "observations",
                "impact_assessments", "internal_loss_events", "saved_views")
PAYLOAD_BYTE_BUDGET = MAX_IMPORT_BYTES - 64 * 1024
MAX_INTAKE_UNITS = 100
MAX_INTAKE_CHARS = 190_000


class StagingError(ValueError):
    """The output location is unsafe."""


def write_json(path: Path, value: Any) -> None:
    text = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    write_text(path, text)


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=path.suffix)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def check_output_dir(out_dir: Path, folder: Path, public_paths: list[Path]) -> Path:
    out_dir = out_dir.expanduser().resolve()
    folder = folder.resolve()
    if out_dir == folder or folder in out_dir.parents:
        raise StagingError("The output directory must be outside the ingest folder.")
    for public in public_paths:
        if public.expanduser().resolve().is_relative_to(out_dir):
            raise StagingError("The output directory must not contain the public research stores.")
    return out_dir


def org_payloads(candidates: list[Candidate]) -> list[dict[str, Any]]:
    """Split valid org-context candidates into preview-sized payloads, services first."""
    rank = {kind: i for i, kind in enumerate(IMPORT_ORDER)}
    eligible = sorted((c for c in candidates if c.kind in ORG_CONTEXT_ROUTES and not c.errors),
                      key=lambda c: (rank[c.kind], c.id))
    payloads: list[dict[str, Any]] = []
    current: list[Candidate] = []
    size = len(json.dumps({"records": []}))

    def flush():
        if current:
            payloads.append({"records": [{"kind": c.kind, "data": c.data} for c in current],
                             "candidate_ids": [c.id for c in current]})

    for c in eligible:
        entry_size = len(json.dumps({"kind": c.kind, "data": c.data}, ensure_ascii=False).encode()) + 2
        if current and (len(current) >= MAX_IMPORT_ROWS or size + entry_size > PAYLOAD_BYTE_BUDGET):
            flush()
            current, size = [], len(json.dumps({"records": []}))
        current.append(c)
        size += entry_size
    flush()
    return payloads


def _unit(c: Candidate) -> str:
    d = c.data
    lines = [d.get("headline", "")]
    meta = ", ".join(str(v) for v in (d.get("organization"), d.get("source_date"), d.get("country")) if v)
    if meta:
        lines.append(meta)
    if d.get("independently_authored_summary"):
        lines.append(d["independently_authored_summary"])
    if d.get("loss_amount_usd") is not None:
        lines.append(f"Reported loss (USD): {d['loss_amount_usd']:g}")
    lines.append(f"Source: {d.get('source_url', '')}")
    return "\n".join(line.replace("\n", " ").strip() for line in lines if line)


def evidence_handoff(candidates: list[Candidate]) -> tuple[list[str], list[dict[str, Any]]]:
    valid = [c for c in candidates if c.kind == "evidence_events" and not c.errors]
    materials, current, size = [], [], 0
    for c in valid:
        unit = _unit(c)
        if current and (len(current) >= MAX_INTAKE_UNITS or size + len(unit) + 2 > MAX_INTAKE_CHARS):
            materials.append("\n\n".join(current))
            current, size = [], 0
        current.append(unit)
        size += len(unit) + 2
    if current:
        materials.append("\n\n".join(current))
    drafts = []
    for c in valid:
        d, prov = c.data, c.provenance[0]
        drafts.append({
            "event_id": "EVT-BULK-" + short_hash(c.id).upper(),
            "headline": d.get("headline"), "organization": d.get("organization"),
            "country": d.get("country"), "region": d.get("region"),
            "loss_amount_usd": d.get("loss_amount_usd"), "source_date": d.get("source_date"),
            "risk_category": derive_category({"title": d.get("headline", ""),
                                              "description": d.get("independently_authored_summary", "")}),
            "source_url": d.get("source_url"), "source_publisher": d.get("source_publisher", ""),
            "source_title": d.get("source_title", ""), "source_type": "public_reporting",
            "record_type": "event", "provenance_class": "public_secondary",
            "independently_authored_summary": d.get("independently_authored_summary"),
            "rights_review": "pending", "review_status": "pending", "reviewed_at": None,
            "taxonomy_version": TAXONOMY_VERSION,
            "provenance_note": f"Staged by bulk ingest from {prov.file} ({prov.locator}); citation kept as "
                               "supplied; pending human review.",
            "bulk_candidate_id": c.id,
        })
    return materials, drafts


def stage(out_dir: Path, batch: dict[str, Any], candidates: list[Candidate]) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    handoff_dir = out_dir / "handoff"
    for stale in handoff_dir.glob("*") if handoff_dir.is_dir() else []:
        if stale.is_file():
            stale.unlink()
    handoff: dict[str, Any] = {"org_context": [], "evidence": {}, "proposed": []}
    for n, payload in enumerate(org_payloads(candidates), start=1):
        rel = f"handoff/org_context_{n:03d}.json"
        write_json(out_dir / rel, {"records": payload["records"]})
        kinds: dict[str, int] = {}
        for record in payload["records"]:
            kinds[record["kind"]] = kinds.get(record["kind"], 0) + 1
        handoff["org_context"].append({
            "file": rel, "order": n, "records": len(payload["records"]), "kinds": kinds,
            "candidate_ids": payload["candidate_ids"],
            "bytes": (out_dir / rel).stat().st_size,
        })
    materials, drafts = evidence_handoff(candidates)
    files = []
    for n, material in enumerate(materials, start=1):
        rel = f"handoff/evidence_research_material_{n:03d}.txt"
        write_text(out_dir / rel, material + "\n")
        files.append(rel)
    if drafts:
        write_json(out_dir / "handoff/evidence_drafts.json",
                   {"contract": "evidence_events", "review_status": "pending", "records": drafts})
    handoff["evidence"] = {
        "research_material_files": files, "drafts_file": "handoff/evidence_drafts.json" if drafts else None,
        "count": len(drafts),
        "how_to_queue": "workbench_atlas.web.intake_flow.prepare_research_material(material, settings=..., "
                        "client=OMLXClient(settings)) then submit_draft_to_review(draft_id, selected_ids, "
                        "settings=...). Records land as pending; that path needs the LLM judge.",
    }
    # "proposed" stays in the shape (always empty) so older readers keep working.
    batch["handoff"] = handoff
    write_json(out_dir / "batch.json", batch)
    return handoff
