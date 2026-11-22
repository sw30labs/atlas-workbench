"""Human-selected submission of a staged batch through the existing import gate.

Nothing auto-submits. A named actor selects records ("all" means every
``ready`` org-context record, never ``needs_review`` or ``invalid`` ones);
each handoff payload then goes through ``imports.preview_import`` and only the
selected, valid preview rows are confirmed with ``imports.submit_import``.
Payloads are submitted in dependency order; each one is atomic, the sequence
is not.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..config import Settings
from ..context import _LINKS
from ..imports import preview_import, submit_import
from .models import ORG_CONTEXT_ROUTES


SUBMIT_ERROR_CODES = (
    "not_a_batch",         # the file is not a bulk-ingest batch
    "invalid_selection",   # unknown, non-org-context or invalid ids, or a malformed selection
    "actor_required",      # no named reviewer
    "nothing_to_submit",   # the selection has no ready record
    "already_exists",      # a selected id is already in the organization workspace; nothing was written
    "revision_required",   # the workspace changed under the write (optimistic revision conflict)
    "preview_failed",      # the import gate's preview rejected a payload
    "import_rejected",     # the import gate refused the write for another reason
    "stale_plan",          # reserved for callers: the reviewed plan no longer matches the batch
)


class SubmitError(ValueError):
    """The selection or the batch cannot be submitted as asked.

    ``code`` is one of :data:`SUBMIT_ERROR_CODES`, so callers map failures to
    responses without reading the message. ``written`` counts payloads already
    written when the failure happened (payloads are atomic, the sequence is not)."""

    def __init__(self, message: str, *, code: str = "invalid_selection", written: int = 0,
                 ids: list[str] | None = None):
        super().__init__(message)
        self.code = code if code in SUBMIT_ERROR_CODES else "invalid_selection"
        self.written = written
        self.ids = list(ids or [])


def load_batch(path: str | Path) -> tuple[dict[str, Any], Path]:
    path = Path(path).expanduser().resolve()
    batch = json.loads(path.read_text(encoding="utf-8"))
    if batch.get("kind") != "atlas_bulk_ingest_batch":
        raise SubmitError("Not an Atlas bulk-ingest batch file.", code="not_a_batch")
    return batch, path.parent


def _existing_ids(settings: Settings) -> set[str]:
    """Ids already in the organization workspace (read-only; empty when disabled or unused)."""
    from ..context import context_payload

    records = context_payload(settings).get("records", {})
    return {r["id"] for rows in records.values() for r in rows if isinstance(r, dict) and r.get("id")}


# Scenario references may point at organization scenarios staged in the same batch.
_REFERENCE_FIELDS = tuple(_LINKS) + ("scenario_id", "scenario_ids")


def _targets(data: dict[str, Any]) -> list[str]:
    out = []
    for field in _REFERENCE_FIELDS:
        value = data.get(field)
        out.extend(t for t in (value if isinstance(value, list) else [value]) if t)
    return out


def plan_submission(batch: dict[str, Any], selection: str | list[str]) -> dict[str, Any]:
    """What would be written, per payload, without writing anything."""
    candidates = {c["id"]: c for c in batch.get("candidates", [])}
    payload_of = {cid: p for p in batch.get("handoff", {}).get("org_context", []) for cid in p["candidate_ids"]}
    if selection == "all":
        chosen = [cid for cid, c in candidates.items() if c["kind"] in ORG_CONTEXT_ROUTES
                  and c["status"] == "ready" and cid in payload_of]
        skipped = [{"id": cid, "reason": c["status"]} for cid, c in candidates.items()
                   if c["kind"] in ORG_CONTEXT_ROUTES and cid not in chosen]
        # Keep "all" closed under references: drop ready records that point at unselected batch records.
        changed = True
        while changed:
            changed = False
            selected = set(chosen)
            for cid in list(chosen):
                missing = sorted(t for t in _targets(candidates[cid]["data"]) if t in candidates and t not in selected)
                if missing:
                    chosen.remove(cid)
                    skipped.append({"id": cid, "reason": "references unselected " + ", ".join(missing)})
                    changed = True
    else:
        if not isinstance(selection, list) or not selection:
            raise SubmitError("Select 'all' or one or more candidate ids.")
        problems = []
        for cid in selection:
            c = candidates.get(cid)
            if c is None:
                problems.append(f"{cid}: not in this batch")
            elif c["kind"] not in ORG_CONTEXT_ROUTES:
                problems.append(f"{cid}: {c['kind']} is not an organization-context record "
                                f"({c['contract_status']} contract; not importable here)")
            elif c["errors"] or cid not in payload_of:
                problems.append(f"{cid}: has validation errors; fix the source and re-run the batch")
        if problems:
            raise SubmitError("Selection rejected; nothing was written:\n" + "\n".join(problems))
        chosen, skipped = list(dict.fromkeys(selection)), []
    chosen_set = set(chosen)
    warnings = [f"{cid} references {target}, which is not selected; it must already exist in the "
                "workspace or the payload will be rejected"
                for cid in chosen for target in _targets(candidates[cid]["data"])
                if target in candidates and target not in chosen_set]
    payloads = []
    for p in batch.get("handoff", {}).get("org_context", []):
        ids = [cid for cid in p["candidate_ids"] if cid in chosen_set]
        if ids:
            payloads.append({"file": p["file"], "order": p["order"], "records": [
                {"id": cid, "kind": candidates[cid]["kind"],
                 "label": candidates[cid]["data"].get("name") or candidates[cid]["data"].get("title")
                 or candidates[cid]["data"].get("statement") or cid} for cid in ids]})
    return {"batch_id": batch.get("batch_id"), "selected": len(chosen), "payloads": payloads,
            "skipped": skipped, "warnings": sorted(set(warnings))}


def submit_batch(settings: Settings, batch_path: str | Path, *, selection: str | list[str],
                 actor: str) -> dict[str, Any]:
    """Preview then confirm each payload's selected records with the named actor."""
    if not isinstance(actor, str) or not actor.strip():
        raise SubmitError("A named reviewer is required.", code="actor_required")
    batch, base = load_batch(batch_path)
    plan = plan_submission(batch, selection)
    if not plan["payloads"]:
        raise SubmitError("Nothing to submit: no selected record is ready.", code="nothing_to_submit")
    selected = [r["id"] for step in plan["payloads"] for r in step["records"]]
    present = sorted(set(selected) & _existing_ids(settings))
    if present:
        raise SubmitError(f"Nothing was written (0 earlier payload(s)): {len(present)} selected record(s) already "
                          "exist in the organization workspace; updating them needs a revision-aware edit: "
                          + ", ".join(present[:20]) + (" ..." if len(present) > 20 else ""),
                          code="already_exists", ids=present)
    source = f"Atlas bulk ingest batch {batch['batch_id']} (prepared by {batch.get('prepared_by', 'unknown')})"
    results = []
    for step in plan["payloads"]:
        wanted = {r["id"] for r in step["records"]}
        content = json.loads((base / step["file"]).read_text(encoding="utf-8"))
        records = [r for r in content["records"] if r["data"].get("id") in wanted]
        preview = preview_import(settings, content=json.dumps({"records": records}), format="json",
                                 actor=actor, source=source)
        bad = [f"{row['candidate_id']}: {'; '.join(row['errors'])}" for row in preview["candidates"] if row["errors"]]
        if bad:
            raise SubmitError(f"{step['file']} failed preview; it and later payloads were not written "
                              f"({len(results)} earlier payload(s) were):\n" + "\n".join(bad),
                              code="preview_failed", written=len(results))
        try:
            done = submit_import(settings, preview_id=preview["preview_id"], actor=actor,
                                 selected_ids=[row["candidate_id"] for row in preview["candidates"]])
        except ValueError as exc:
            from ..context import ContextConflict

            cause = exc.__cause__ if exc.__cause__ is not None else exc
            code = "revision_required" if isinstance(cause, ContextConflict) else "import_rejected"
            raise SubmitError(f"{step['file']} was not written ({len(results)} earlier payload(s) were): "
                              f"{exc}", code=code, written=len(results)) from exc
        results.append({"file": step["file"], "imported_count": done["imported_count"],
                        "ids": [r["data"]["id"] for r in records]})
    return {"ok": True, "batch_id": batch["batch_id"], "actor": actor.strip(), "payloads": results,
            "imported_count": sum(r["imported_count"] for r in results), "warnings": plan["warnings"]}
