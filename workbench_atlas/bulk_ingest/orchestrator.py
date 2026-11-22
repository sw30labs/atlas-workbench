"""Orchestrator: run the pods over one folder and stage one reviewable batch.

Staging only. Nothing here writes the organization workspace, the evidence
store or the emerging-risk register; :mod:`.submit` is the separate, explicit,
human-selected path into ``imports.preview_import`` / ``submit_import``.
"""

from __future__ import annotations

import hashlib
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from ..config import Settings
from .critic import critique
from .extractors import extract_document
from .inventory import Limits, Manifest, build_inventory
from .linker import link_batch
from .llm_lane import LLMLane
from .models import BATCH_SCHEMA_VERSION, Candidate, DocumentResult, Issue, Route
from .readers import ReaderError, read_document
from .stager import check_output_dir, stage
from .triage import mark_superseded, triage

Progress = Callable[[dict[str, Any]], None]
_NOT_DOCUMENTS = ("hidden or system entry", "system file", "excluded: scoring answer keys are never read",
                  "symlink not followed")
NOTICE = ("Staging only. Organization records enter the private workspace only through "
          "imports.preview_import/submit_import with a named reviewer; evidence enters the review queue "
          "as pending; proposed contracts are not persisted. File contents were treated as data.")


class BatchCancelled(RuntimeError):
    """The operator cancelled the batch; nothing was staged."""


@dataclass
class BatchResult:
    batch_id: str
    status: str
    out_dir: str | None
    batch_path: str | None
    counts: dict[str, Any]
    documents: list[dict[str, Any]] = field(default_factory=list)
    candidates: list[dict[str, Any]] = field(default_factory=list)
    issues: list[dict[str, Any]] = field(default_factory=list)
    handoff: dict[str, Any] = field(default_factory=dict)
    links: dict[str, Any] = field(default_factory=dict)
    batch: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        return {"batch_id": self.batch_id, "status": self.status, "out_dir": self.out_dir,
                "batch_path": self.batch_path, "counts": self.counts, "handoff": self.handoff}


def batches_dir(settings: Settings) -> Path:
    """Where batches are staged by default: ``<workspace>/generated/bulk_ingest`` (resolved)."""
    return (Path(settings.workspace) / "generated" / "bulk_ingest").resolve()


def default_output_dir(settings: Settings, batch_id: str) -> Path:
    return batches_dir(settings) / batch_id


def batch_identity(manifest: Manifest, *, use_llm: bool, classification: str) -> str:
    digest = hashlib.sha256()
    for entry in manifest.entries:
        digest.update(f"{entry.path}\x1f{entry.sha256}\x1f{entry.status}\n".encode())
    digest.update(f"llm={use_llm};classification={classification};schema={BATCH_SCHEMA_VERSION}".encode())
    return "bulk-" + digest.hexdigest()[:12]


def _is_document(entry) -> bool:
    return not entry.path.endswith("/") and entry.reason not in _NOT_DOCUMENTS


def run_batch(
    settings: Settings, folder: str | Path, *, actor: str, use_llm: bool = False, max_workers: int = 4,
    progress: Progress | None = None, cancel: threading.Event | None = None, out_dir: str | Path | None = None,
    client: Any = None, classification: str = "organization", limits: Limits | None = None, write: bool = True,
) -> BatchResult:
    if not isinstance(actor, str) or not actor.strip() or len(actor.strip()) > 160:
        raise ValueError("A named operator (1 to 160 characters) is required to prepare a batch.")
    if classification not in ("organization", "synthetic"):
        raise ValueError("classification must be 'organization' or 'synthetic'")
    max_workers = max(1, min(int(max_workers), 16))

    def emit(**event: Any) -> None:
        if progress is not None:
            try:
                progress(event)
            except Exception:  # a broken progress sink must not break the batch
                pass

    def check_cancel() -> None:
        if cancel is not None and cancel.is_set():
            raise BatchCancelled("bulk ingest cancelled by the operator")

    manifest = build_inventory(folder, limits)
    batch_id = batch_identity(manifest, use_llm=use_llm, classification=classification)
    emit(stage="inventory", status="done", total=len(manifest.entries), accepted=len(manifest.accepted))
    lane = None
    if use_llm:
        if client is None:
            from ..llm import OMLXClient

            client = OMLXClient(settings)
        lane = LLMLane(client)
    results = [DocumentResult(entry) for entry in manifest.entries if _is_document(entry)]
    try:
        _run_stage("read", results, lambda r: _read(manifest.root, r), max_workers, emit, check_cancel)
        _run_stage("triage", results, lambda r: _triage(r, lane), max_workers, emit, check_cancel)
        mark_superseded(results)
        _inherit_duplicates(results)
        _run_stage("extract", results, lambda r: _extract(r, lane), max_workers, emit, check_cancel)
        check_cancel()
    except BatchCancelled:
        emit(stage="batch", status="cancelled")
        return BatchResult(batch_id, "cancelled", None, None, {})
    candidates: list[Candidate] = [c for r in results for c in r.candidates]
    linked = link_batch(candidates, classification=classification,
                        alias_hints=[h for r in results for h in r.alias_hints])
    emit(stage="link", status="done", candidates=len(linked.candidates))
    issues = critique(results, linked.candidates, linked.issues)
    emit(stage="critic", status="done", issues=len(issues))
    counts = _counts(results, linked.candidates, issues)
    status = "completed_with_issues" if any(i.severity == "error" for i in issues) else "completed"
    batch = {
        "schema_version": BATCH_SCHEMA_VERSION, "kind": "atlas_bulk_ingest_batch", "batch_id": batch_id,
        "status": status, "generated_at": datetime.now(timezone.utc).isoformat(), "prepared_by": actor.strip(),
        "folder": str(manifest.root), "use_llm": use_llm, "classification": classification, "notice": NOTICE,
        "counts": counts, "manifest": manifest.to_dict(), "documents": [r.summary() for r in results],
        "candidates": [c.to_dict() for c in linked.candidates], "links": linked.to_dict(),
        "issues": [asdict(i) for i in issues], "handoff": {},
    }
    result = BatchResult(batch_id, status, None, None, counts, batch["documents"], batch["candidates"],
                         batch["issues"], {}, batch["links"], batch)
    if write:
        target = Path(out_dir) if out_dir else default_output_dir(settings, batch_id)
        target = check_output_dir(target, manifest.root, [settings.scenario_db_path, settings.evidence_db_path,
                                                          settings.emerging_risks_path, settings.scenario_export_path,
                                                          settings.context_db_path])
        result.handoff = stage(target, batch, linked.candidates)
        result.out_dir, result.batch_path = str(target), str(target / "batch.json")
        emit(stage="stage", status="done", out_dir=str(target))
    return result


def _run_stage(name: str, results: list[DocumentResult], work: Callable[[DocumentResult], None],
               max_workers: int, emit: Callable[..., None], check_cancel: Callable[[], None]) -> None:
    total = len(results)

    def guarded(item: tuple[int, DocumentResult]) -> None:
        index, result = item
        check_cancel()
        try:
            work(result)
            emit(stage=name, file=result.entry.path, status="done", done=index + 1, total=total)
        except BatchCancelled:
            raise
        except Exception as exc:  # one bad file never fails the batch
            result.issues.append(Issue("error", f"{name}_failed", f"{type(exc).__name__}: {str(exc)[:200]}",
                                       result.entry.path))
            result.extract = False
            emit(stage=name, file=result.entry.path, status="error", done=index + 1, total=total)

    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix=f"bulk-{name}") as pool:
        list(pool.map(guarded, enumerate(results)))


def _read(root: Path, result: DocumentResult) -> None:
    entry = result.entry
    if entry.status == "accepted":
        try:
            result.document = read_document(root, entry)
        except ReaderError as exc:
            entry.status, entry.reason = "quarantined", str(exc)
    if entry.status == "quarantined":
        result.flag("unreadable", entry.reason)
        result.routes.append(Route("quarantine", 0.0, [entry.reason]))
        result.extract = False
    elif entry.status == "skipped":
        result.flag("out_of_scope", entry.reason)
        result.routes.append(Route("ignore", 0.0, [entry.reason]))
        result.extract = False
    elif entry.status == "duplicate":
        result.flag("duplicate", entry.reason)
        result.extract = False


def _triage(result: DocumentResult, lane: LLMLane | None) -> None:
    if result.document is not None:
        triage(result, tiebreak=lane.tiebreak if lane else None)
        if any(r.route == "quarantine" for r in result.routes):
            result.extract = False


def _extract(result: DocumentResult, lane: LLMLane | None) -> None:
    extract_document(result, llm=lane)


def _inherit_duplicates(results: list[DocumentResult]) -> None:
    by_path = {r.entry.path: r for r in results}
    for result in results:
        original = by_path.get(result.entry.duplicate_of or "")
        if result.entry.status == "duplicate" and original is not None:
            result.routes = [Route("ignore", 0.0, [f"duplicate of {original.entry.path}; content routes: "
                                   + ", ".join(original.route_names)])]
    for result in results:
        if any(f.flag == "superseded" for f in result.flags):
            names = ", ".join(sorted({r.route for r in result.routes} - {"ignore"})) or "none"
            reason = next(f.reason for f in result.flags if f.flag == "superseded")
            result.routes = [Route("ignore", 0.0, [f"{reason}; content routes: {names}"])]


def _counts(results: list[DocumentResult], candidates: list[Candidate], issues: list[Issue]) -> dict[str, Any]:
    routes, flags = Counter(), Counter()
    for r in results:
        routes.update(r.route_names)
        flags.update({f.flag for f in r.flags})
    by_kind: dict[str, dict[str, int]] = {}
    for c in candidates:
        bucket = by_kind.setdefault(c.kind, {"total": 0, "ready": 0, "needs_review": 0, "invalid": 0})
        bucket["total"] += 1
        bucket[c.status] += 1
    return {"files": len(results), "files_by_route": dict(sorted(routes.items())),
            "files_by_flag": dict(sorted(flags.items())), "records_by_contract": dict(sorted(by_kind.items())),
            "issues_by_severity": dict(sorted(Counter(i.severity for i in issues).items())),
            "issues_by_code": dict(sorted(Counter(i.code for i in issues).items()))}


def run_bulk_ingest_job(params: dict[str, Any], settings: Settings, progress: Progress | None = None,
                        client: Any = None, *, cancel: threading.Event | None = None) -> dict[str, Any]:
    """Job-runner entry point (see docs/reference/bulk-ingest.md, 'Integration').

    ``cancel`` is passed to :func:`run_batch`; a cancelled batch returns status
    ``cancelled`` and writes nothing."""
    folder = str(params.get("folder") or "").strip()
    if not folder:
        raise ValueError("bulk_ingest needs a folder")
    result = run_batch(settings, folder, actor=str(params.get("actor") or "local operator"),
                       use_llm=bool(params.get("use_llm")), progress=progress, client=client, cancel=cancel,
                       classification=str(params.get("classification") or "organization"))
    return result.summary()
