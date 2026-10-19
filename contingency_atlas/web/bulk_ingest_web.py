"""Dashboard adapter for bulk ingest: folder allow-list, batch reads, plan and submit.

The engine (:mod:`contingency_atlas.bulk_ingest`) is used through its public
API only. This module decides which server-side folders a dashboard user may
name, which staged batches the browser may read, and binds a submission to the
plan the reviewer actually saw. Nothing here approves or auto-submits: the
import gate (``imports.preview_import`` / ``submit_import``) still writes.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from contingency_atlas.config import Settings

BATCH_ID = re.compile(r"bulk-[0-9a-f]{12}\Z")
CANDIDATE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}\Z")
ANSWER_KEY = "answer_key.json"
MAX_SELECTION = 5000
MAX_LISTED = 200
LIST_DEPTH = 2  # immediate subfolders of a root, and their subfolders
_SCAN_LIMIT = 5000  # directory entries examined per candidate folder
_HIDDEN = (".", "__")


class FolderError(ValueError):
    """The requested folder is not an allowed bulk-ingest source."""


class BatchError(ValueError):
    """A batch request failed. ``code`` decides the HTTP status (see :data:`HTTP_STATUS`):
    ``not_found``, ``stale_plan``, ``invalid_request``, or an engine ``SubmitError`` code."""

    def __init__(self, message: str, *, code: str = "invalid_request"):
        super().__init__(message)
        self.code = code


# Conflicts with the current state are 409; a missing batch is 404; everything else is a bad request.
HTTP_STATUS = {"not_found": 404, "stale_plan": 409, "already_exists": 409, "revision_required": 409}


def http_status(error: BatchError) -> int:
    return HTTP_STATUS.get(error.code, 400)


def allowed_roots(settings: Settings) -> list[Path]:
    """Configured roots, resolved. Missing roots are kept (they list nothing)."""
    roots = []
    for raw in settings.bulk_ingest_roots or []:
        path = Path(raw).expanduser()
        roots.append(path.resolve() if path.exists() else Path(os.path.abspath(path)))
    return list(dict.fromkeys(roots))


def batches_dir(settings: Settings) -> Path:
    """Where ``run_batch`` stages batches by default; the engine owns the path."""
    from contingency_atlas.bulk_ingest import batches_dir as engine_batches_dir

    return engine_batches_dir(settings)


def _public_stores(settings: Settings) -> list[Path]:
    paths = [settings.scenario_db_path, settings.evidence_db_path, settings.emerging_risks_path,
             settings.scenario_export_path, settings.context_db_path]
    return [Path(p).expanduser().resolve() for p in paths]


def _within(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def _contains_answer_key(folder: Path) -> bool:
    seen = 0
    for _dirpath, dirnames, filenames in os.walk(folder, followlinks=False):
        if ANSWER_KEY in filenames:
            return True
        seen += len(dirnames) + len(filenames)
        if seen > _SCAN_LIMIT:
            return True  # too large to prove clean: fail closed
    return False


def _file_count(folder: Path) -> int:
    count = 0
    for _dirpath, dirnames, filenames in os.walk(folder, followlinks=False):
        dirnames[:] = [d for d in dirnames if not d.startswith(_HIDDEN)]
        count += sum(1 for f in filenames if not f.startswith("."))
        if count > _SCAN_LIMIT:
            break
    return count


def resolve_ingest_folder(settings: Settings, raw: Any) -> Path:
    """Return the real folder for ``raw`` or raise :class:`FolderError`.

    Accepted: an absolute path, or a path relative to the project root, that
    names an existing directory strictly inside one configured root, reached
    without any symlink, holding no scoring answer key and no public store.
    """
    if not isinstance(raw, str) or not raw.strip() or len(raw) > 4096 or "\x00" in raw:
        raise FolderError("Choose a folder from the list.")
    text = raw.strip()
    if ".." in Path(text).parts:
        raise FolderError("Folder paths may not contain '..'.")
    from contingency_atlas.config import project_root

    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        candidate = project_root() / candidate
    candidate = Path(os.path.abspath(candidate))
    for root in allowed_roots(settings):
        relative = None
        for base in dict.fromkeys((root, Path(os.path.abspath(root)))):
            if _within(candidate, base) and candidate != base:
                relative = candidate.relative_to(base)
                break
        if relative is None:
            continue
        current = root
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise FolderError("Folders reached through a symlink are not accepted.")
        if not current.is_dir():
            raise FolderError("That folder does not exist.")
        real = current.resolve()
        if not _within(real, root) or real == root:
            raise FolderError("That folder is outside the allowed bulk-ingest roots.")
        for store in _public_stores(settings):
            if _within(store, real):
                raise FolderError("That folder holds an Atlas store and cannot be ingested.")
        if _within(real, batches_dir(settings)) or _within(batches_dir(settings), real):
            raise FolderError("Staged batch output cannot be ingested.")
        if _contains_answer_key(real):
            raise FolderError("That folder holds a scoring answer key. Choose the pack folder inside it.")
        return real
    raise FolderError("That folder is outside the allowed bulk-ingest roots.")


def list_ingest_folders(settings: Settings) -> dict[str, Any]:
    """Subfolders (up to two levels) of each root that hold files and pass the allow-list."""
    folders: list[dict[str, Any]] = []
    roots_out = []
    for root in allowed_roots(settings):
        roots_out.append({"path": str(root), "exists": root.is_dir()})
        if not root.is_dir():
            continue
        frontier = [root]
        for _depth in range(LIST_DEPTH):
            following = []
            for parent in frontier:
                try:
                    children = sorted(p for p in parent.iterdir()
                                      if p.is_dir() and not p.is_symlink() and not p.name.startswith(_HIDDEN))
                except OSError:
                    continue
                for child in children:
                    following.append(child)
                    try:
                        real = resolve_ingest_folder(settings, str(child))
                    except FolderError:
                        continue
                    files = _file_count(real)
                    if files:
                        folders.append({"path": str(real), "root": str(root),
                                        "label": f"{root.name}/{real.relative_to(root).as_posix()}",
                                        "files": files})
                    if len(folders) >= MAX_LISTED:
                        break
            frontier = following
    return {"roots": roots_out, "folders": folders[:MAX_LISTED], "batches": list_batches(settings)}


def list_batches(settings: Settings, limit: int = 20) -> list[dict[str, Any]]:
    """Recently staged batches, newest first (id, status, folder, counts)."""
    base = batches_dir(settings)
    if not base.is_dir():
        return []
    rows = []
    for child in base.iterdir():
        if not BATCH_ID.fullmatch(child.name) or child.is_symlink():
            continue
        path = child / "batch.json"
        if not path.is_file() or path.is_symlink():
            continue
        try:
            rows.append((path.stat().st_mtime, child.name, path))
        except OSError:
            continue
    out = []
    for _mtime, batch_id, path in sorted(rows, reverse=True)[:limit]:
        try:
            batch = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        out.append({"batch_id": batch_id, "status": batch.get("status"), "folder": batch.get("folder"),
                    "generated_at": batch.get("generated_at"), "classification": batch.get("classification"),
                    "use_llm": batch.get("use_llm"), "prepared_by": batch.get("prepared_by")})
    return out


def batch_path(settings: Settings, batch_id: Any) -> Path:
    """``batch.json`` for a well-formed id, only inside the bulk-ingest output dir."""
    if not isinstance(batch_id, str) or not BATCH_ID.fullmatch(batch_id):
        raise BatchError("Unknown batch id.", code="not_found")
    base = batches_dir(settings)
    folder = base / batch_id
    path = folder / "batch.json"
    if folder.is_symlink() or path.is_symlink() or not path.is_file():
        raise BatchError("That batch was not found.", code="not_found")
    if path.resolve().parent != folder.resolve() or folder.resolve().parent != base:
        raise BatchError("That batch was not found.", code="not_found")
    return path


def load_batch_for_review(settings: Settings, batch_id: Any) -> dict[str, Any]:
    from contingency_atlas.bulk_ingest import SubmitError, load_batch

    path = batch_path(settings, batch_id)
    try:
        batch, _base = load_batch(path)
    except (SubmitError, OSError, ValueError) as exc:
        raise BatchError(f"That batch cannot be read: {exc}") from exc
    if batch.get("batch_id") != batch_id:
        raise BatchError("That batch file does not match its id.")
    review = {k: v for k, v in batch.items() if k != "links"}
    review["plan_all"] = _plan(batch, "all")
    return review


def _selection(raw: Any) -> str | list[str]:
    if raw == "all":
        return "all"
    if (not isinstance(raw, list) or not raw or len(raw) > MAX_SELECTION
            or not all(isinstance(x, str) and CANDIDATE_ID.fullmatch(x) for x in raw)):
        raise BatchError("Select 'all' or a list of record ids.")
    return list(dict.fromkeys(raw))


def _plan(batch: dict[str, Any], selection: str | list[str]) -> dict[str, Any]:
    from contingency_atlas.bulk_ingest import plan_submission

    return plan_submission(batch, selection)


def _digest(batch_file: Path, plan: dict[str, Any]) -> str:
    digest = hashlib.sha256(batch_file.read_bytes())
    digest.update(json.dumps(plan, sort_keys=True, default=str).encode())
    return digest.hexdigest()


def plan(settings: Settings, batch_id: Any, selection: Any) -> dict[str, Any]:
    """The engine's plan for a selection, plus a digest that ``submit`` must echo."""
    from contingency_atlas.bulk_ingest import SubmitError, load_batch

    path = batch_path(settings, batch_id)
    try:
        batch, _base = load_batch(path)
        result = _plan(batch, _selection(selection))
    except SubmitError as exc:
        raise BatchError(str(exc), code=exc.code) from exc
    return {**result, "plan_digest": _digest(path, result)}


def submit(settings: Settings, batch_id: Any, selection: Any, *, actor: Any, plan_digest: Any) -> dict[str, Any]:
    """Write the reviewed plan through the import gate with the authenticated actor."""
    from contingency_atlas.bulk_ingest import SubmitError, submit_batch

    if not isinstance(actor, str) or not actor.strip() or len(actor.strip()) > 160:
        raise BatchError("Enter your name to record this import.", code="actor_required")
    current = plan(settings, batch_id, selection)
    if not isinstance(plan_digest, str) or plan_digest != current["plan_digest"]:
        raise BatchError("The batch or the plan changed since you reviewed it. Preview the plan again.",
                         code="stale_plan")
    if not current["payloads"]:
        raise BatchError("Nothing to submit: no selected record is ready.", code="nothing_to_submit")
    try:
        return submit_batch(settings, batch_path(settings, batch_id), selection=_selection(selection),
                            actor=actor.strip())
    except SubmitError as exc:
        raise BatchError(str(exc), code=exc.code) from exc
