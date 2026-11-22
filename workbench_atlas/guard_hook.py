"""guard_hook.py — adapter between Atlas write seams and the ontology guard.

The probabilistic pipeline proposes; the ontology permits. Machine-producer
seams validate their batch through ``ontology/guard.py`` (a subprocess running
the two-gate validator) before persisting: FLAGs are logged and admitted,
REJECTs block the write — the previous store state stands (the atomic-write
discipline the stores already follow).

Kill switch: ``ATLAS_GUARD=off`` (the test suite sets this in
``tests/conftest.py``). When enabled, a missing or incomplete ontology bundle
raises ``GuardError`` before any producer write can proceed.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import sysconfig
import tempfile
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, files
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
_SOURCE_ONTOLOGY_DIR = ROOT / "ontology"


def _installed_ontology_dir() -> Path:
    """Resolve wheel data through its installation metadata when available.

    The distribution name is ``atlas-workbench`` (``pyproject.toml``).
    The data-files directory is ``share/workbench-atlas``. Asking
    ``files()`` for the directory name never finds a distribution, so
    that lookup has to use the project name and then fall back to the
    sysconfig data path.
    """
    for dist_name in ("atlas-workbench",):
        try:
            for entry in files(dist_name) or ():
                label = entry.as_posix()
                if label.endswith("share/workbench-atlas/ontology/guard.py"):
                    return Path(entry.locate()).resolve().parent
        except PackageNotFoundError:
            continue
    return (
        Path(sysconfig.get_path("data"))
        / "share"
        / "workbench-atlas"
        / "ontology"
    )


_INSTALLED_ONTOLOGY_DIR = _installed_ontology_dir()
ONTOLOGY_DIR = (
    _SOURCE_ONTOLOGY_DIR
    if (_SOURCE_ONTOLOGY_DIR / "guard.py").is_file()
    else _INSTALLED_ONTOLOGY_DIR
)
GUARD_SCRIPT = ONTOLOGY_DIR / "guard.py"
EXPORTER_SCRIPT = ONTOLOGY_DIR / "export_existing.py"
DOMAIN_TTL = ONTOLOGY_DIR / "domain.ttl"
SHAPES_TTL = ONTOLOGY_DIR / "shapes.ttl"
CATEGORY_MAP = ONTOLOGY_DIR / "category-map.json"
RUNTIME_ROOT = ROOT if ONTOLOGY_DIR == _SOURCE_ONTOLOGY_DIR else Path.cwd()
EXISTING_TTL = (
    ONTOLOGY_DIR / "existing.ttl"
    if ONTOLOGY_DIR == _SOURCE_ONTOLOGY_DIR
    else RUNTIME_ROOT / "generated" / "ontology" / "existing.ttl"
)
DATA_NAMESPACE = "https://workbench-atlas.local/id/"
GUARD_TIMEOUT = 300

_REJECTS = {"REJECT", "REJECTED_AT_DOOR"}


class GuardRejected(RuntimeError):
    """A producer batch failed the door or the ledger; the write was blocked."""


class GuardError(RuntimeError):
    """The guard itself failed to run. Fail-closed: a gate that silently
    opens is the failure mode the ontology exists to remove."""


@dataclass
class GuardReport:
    verdicts: dict[str, str]  # "kind:id" -> ADMIT / AUTO_MERGE / FLAG / REJECT / REJECTED_AT_DOOR
    repair: list[dict[str, Any]]
    review_queue: list[dict[str, Any]]

    @property
    def rejected_ids(self) -> set[str]:
        return {
            key.split(":", 1)[1]
            for key, v in self.verdicts.items()
            if v in _REJECTS
        }

    def raise_if_rejected(self, seam: str) -> None:
        if self.rejected_ids:
            raise GuardRejected(
                f"{seam}: guard rejected {len(self.rejected_ids)} record(s): "
                f"{sorted(self.rejected_ids)} — repair instructions in guard output"
            )


def guard_enabled() -> bool:
    return os.environ.get("ATLAS_GUARD", "on").strip().lower() not in {
        "off", "0", "false", "no",
    }


def _require_ontology_bundle() -> None:
    """Fail closed when an enabled installation lacks its validator bundle."""
    required = (
        GUARD_SCRIPT,
        EXPORTER_SCRIPT,
        DOMAIN_TTL,
        SHAPES_TTL,
        CATEGORY_MAP,
    )
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise GuardError(
            "ontology guard is enabled but its installed bundle is incomplete: "
            + ", ".join(missing)
        )


def mint_risk_id(issue: str) -> str:
    """Deterministic project-native risk id (C-03).

    The id IS the lossless title slug — the same key ``compound_scenarios``,
    the scorer and ``atlas:slugId`` already join on, so the register, the
    scoring layer and the graph share one identifier instead of three.

    This is a fallback: once ``scripts/migrate_register_ids.py`` has run, the
    record carries ``risk_id`` and every reader prefers the stored value, so a
    later title edit no longer moves the record's identity. Minting cannot
    disambiguate a slug collision (that needs the whole register); the stored
    ids can, and the ledger flags collisions it sees (C-03, Warning)."""
    from .scoring.inventory import slugify

    return slugify(issue)


# -- record builders (producer row -> guard candidate) ------------------------
# Builders pass the full source dict through; the door models ignore
# unmodeled fields (extra="ignore") and constrain only what the ontology owns.
#
# "kind" is the door's discriminator, so it is written AFTER the spread in
# every builder. Written before, a producer row carrying its own "kind" column
# silently overwrites it and the record is refused as an unknown type —
# channel proposals, whose domain kind is edge_add/tag_add/new_channel, hit
# exactly that and were rejected wholesale from the moment the guard shipped.


def risk_record(rec: dict[str, Any]) -> dict[str, Any]:
    title = str(rec.get("issue") or rec.get("title") or "untitled-risk")
    return {
        **rec,
        "kind": "risk",
        "id": rec.get("risk_id") or mint_risk_id(title),
    }


def evidence_event_record(event: dict[str, Any]) -> dict[str, Any]:
    return {
        **event,
        "kind": "evidence_event",
        "id": str(event.get("event_id") or ""),
    }


def scenario_record(row: dict[str, Any]) -> dict[str, Any]:
    return {
        **row,
        "kind": "scenario",
        "id": str(row.get("scenario_id") or ""),
    }


def compound_record(
    row: dict[str, Any], *, run_id: str, status: str = "candidate"
) -> dict[str, Any]:
    return {
        "kind": "compound",
        "id": str(row["compound_id"]),
        "pair_id": str(row["pair_id"]),
        "run_id": run_id,
        "risk_ids": [str(r) for r in row["risk_ids"]],
        "p_hat": float(row["p_hat"]),
        "uncertainty_class": str(row["uncertainty_class"]),
        "loss_units": str(row["loss_units"]),
        "status": status,
        "data_regime": row.get("data_regime"),
    }


def run_record(run_id: str, status: str = "ok") -> dict[str, Any]:
    return {"kind": "compound_run", "id": run_id, "status": status}


def channel_proposal_record(row: dict[str, Any]) -> dict[str, Any]:
    # The row's own "kind" (edge_add | tag_add | new_channel) moves to
    # proposal_kind, where the ontology models it; "kind" belongs to the door.
    return {
        **row,
        "kind": "channel_proposal",
        "id": str(row.get("proposal_id") or ""),
        "proposal_kind": str(row.get("kind") or ""),
    }


def mapping_proposal_record(row: dict[str, Any]) -> dict[str, Any]:
    """I adapt one external-framework mapping claim for the C-09 gate."""
    return {
        **row,
        "kind": "mapping_proposal",
        "id": str(row.get("proposal_id") or ""),
    }


# -- snapshot refresh ----------------------------------------------------------

_STORES = ("emerging_risks.json", "atlas.db", "event_evidence.db")


def refresh_existing() -> None:
    """Regenerate existing.ttl when any store is newer than the snapshot."""
    if EXISTING_TTL.exists():
        snapshot_mtime = EXISTING_TTL.stat().st_mtime
        stale = any(
            (store := RUNTIME_ROOT / name).exists()
            and store.stat().st_mtime > snapshot_mtime
            for name in _STORES
        )
        if not stale:
            return
    EXISTING_TTL.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [
            sys.executable,
            str(EXPORTER_SCRIPT),
            "--root",
            str(RUNTIME_ROOT),
            "--out",
            str(EXISTING_TTL),
        ],
        cwd=RUNTIME_ROOT,
        capture_output=True,
        text=True,
        timeout=GUARD_TIMEOUT,
    )
    if proc.returncode != 0:
        raise GuardError(f"existing.ttl refresh failed: {proc.stderr[-300:]}")


# -- the gate ------------------------------------------------------------------


def validate(records: list[dict[str, Any]], *, seam: str) -> GuardReport:
    """Run both gates over a producer batch. Writes nothing to any store —
    the guard's outputs live in a temp dir and are distilled into the report.

    The snapshot models write-as-replace: triples of any existing node whose
    id appears in the batch are removed before validation, so a write that
    *fixes* a drifted stored record is not blocked by the stale value it is
    about to overwrite (the batch cannot erase the snapshot's assertions
    otherwise — a re-seeded demo row would REJECT on its own repair)."""
    empty = GuardReport({}, [], [])
    if not records or not guard_enabled():
        return empty
    _require_ontology_bundle()
    refresh_existing()
    with tempfile.TemporaryDirectory(prefix="atlas-guard-") as td:
        candidates = Path(td) / "candidates.json"
        candidates.write_text(json.dumps(records, ensure_ascii=False))
        existing_for_run: Path = EXISTING_TTL
        batch_iris = {DATA_NAMESPACE + str(r["id"]) for r in records if r.get("id")}
        if batch_iris:
            # Local import: the package must stay importable without the
            # ontology extra installed.
            from rdflib import Graph, URIRef

            snapshot = Graph()
            snapshot.parse(EXISTING_TTL)
            for iri in batch_iris:
                snapshot.remove((URIRef(iri), None, None))
            existing_for_run = Path(td) / "existing.ttl"
            snapshot.serialize(existing_for_run, format="turtle")
        out_dir = Path(td) / "out"
        proc = subprocess.run(
            [
                sys.executable, str(GUARD_SCRIPT), str(candidates),
                "--existing", str(existing_for_run), "--out", str(out_dir),
            ],
            cwd=ONTOLOGY_DIR, capture_output=True, text=True, timeout=GUARD_TIMEOUT,
        )
        verdicts_path = out_dir / "verdicts.json"
        if not verdicts_path.exists():
            raise GuardError(
                f"guard produced no verdicts at seam {seam}: {proc.stderr[-400:]}"
            )
        verdicts = {
            key: value["verdict"]
            for key, value in json.loads(verdicts_path.read_text()).items()
        }
        repair = json.loads((out_dir / "repair.json").read_text())
        queue = json.loads((out_dir / "review_queue.json").read_text())
    report = GuardReport(verdicts, repair, queue)
    n_flag = sum(1 for v in verdicts.values() if v == "FLAG")
    if n_flag:
        logger.info("guard [%s]: %d record(s) flagged for review", seam, n_flag)
    for entry in repair:
        logger.warning(
            "guard [%s] %s %s: %s",
            seam, entry.get("gate"), entry.get("id"),
            "; ".join(str(e) for e in entry.get("errors", [])),
        )
    return report
