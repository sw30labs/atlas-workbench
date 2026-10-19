"""Shared data shapes for the bulk-ingest pods.

Everything here is plain data. File contents are carried as untrusted text and
never interpreted as instructions by any pod.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

BATCH_SCHEMA_VERSION = 1

ORG_CONTEXT_ROUTES = (
    "services", "dependencies", "mechanisms", "controls", "assumptions",
    "actions", "exercises", "observations", "impact_assessments",
    "internal_loss_events", "org_scenarios", "taxonomy_crosswalk",
)
EVIDENCE_ROUTES = ("evidence_events",)
# The three formerly proposed contracts were agreed on 2026-10-18 and are now
# organization-context kinds in ``context.RECORD_MODELS``. Kept for importers.
AGREED_ROUTES = ("internal_loss_events", "org_scenarios", "taxonomy_crosswalk")
PROPOSED_ROUTES: tuple[str, ...] = ()
# Order matters: triage breaks score ties by it.
CONTRACT_ROUTES = tuple(r for r in ORG_CONTEXT_ROUTES if r not in AGREED_ROUTES) + EVIDENCE_ROUTES + AGREED_ROUTES
ROUTES = CONTRACT_ROUTES + ("ignore", "quarantine")
FLAGS = ("pii", "injection", "duplicate", "superseded", "unreadable", "out_of_scope", "conflict")

FileStatus = Literal["accepted", "skipped", "quarantined", "duplicate"]
Severity = Literal["error", "warning", "info"]
Method = Literal["deterministic", "llm"]


def contract_status(kind: str) -> str:
    if kind in AGREED_ROUTES:
        return "agreed"
    if kind in ORG_CONTEXT_ROUTES:
        return "existing"
    return "evidence"


@dataclass
class FileEntry:
    path: str
    size: int
    status: FileStatus
    reason: str = ""
    format: str = "unknown"
    sha256: str | None = None
    duplicate_of: str | None = None


@dataclass
class Column:
    index: int
    label: str
    group: str = ""

    @property
    def header(self) -> str:
        return f"{self.group} / {self.label}" if self.group and self.group != self.label else self.label


@dataclass
class Row:
    number: int
    values: list[str]
    locator: str


@dataclass
class Table:
    name: str
    columns: list[Column]
    rows: list[Row]
    header_rows: list[int] = field(default_factory=list)
    sheet: str | None = None
    locator_prefix: str = ""
    preamble: list[tuple[str, list[str]]] = field(default_factory=list)  # rows above the header

    def cell_locator(self, row: Row, column: int) -> str:
        if self.sheet is not None:
            return f"{_sheet_ref(self.sheet)}!{column_letter(column)}{row.number}"
        return f"{row.locator} col {column + 1}"

    @property
    def title(self) -> str:
        return self.sheet or self.name


@dataclass
class Block:
    text: str
    locator: str
    kind: str = "paragraph"
    level: int | None = None


@dataclass
class Document:
    path: str
    format: str
    tables: list[Table] = field(default_factory=list)
    blocks: list[Block] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "path": self.path, "format": self.format, "metadata": self.metadata,
            "warnings": self.warnings, "blocks": len(self.blocks),
            "tables": [{"name": t.title, "rows": len(t.rows), "header_rows": t.header_rows,
                        "columns": [c.header for c in t.columns]} for t in self.tables],
        }


@dataclass
class Route:
    route: str
    score: float
    reasons: list[str]
    target: str = "text"  # "text" or "table:<index>"
    method: Method = "deterministic"
    confidence: Literal["high", "low"] = "high"
    field_map: dict[str, int] = field(default_factory=dict)


@dataclass
class Flag:
    flag: str
    reason: str
    locator: str = ""
    excerpt: str = ""


@dataclass
class Issue:
    severity: Severity
    code: str
    message: str
    file: str = ""
    locator: str = ""
    candidate_ids: list[str] = field(default_factory=list)


@dataclass
class Provenance:
    file: str
    locator: str
    pod: str
    method: Method
    spans: dict[str, str] = field(default_factory=dict)
    cells: dict[str, str] = field(default_factory=dict)


@dataclass
class Candidate:
    kind: str
    data: dict[str, Any]
    provenance: list[Provenance]
    meta: dict[str, Any] = field(default_factory=dict)
    id: str = ""
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    status: Literal["ready", "needs_review", "invalid"] = "ready"

    @property
    def contract_status(self) -> str:
        return contract_status(self.kind)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["contract_status"] = self.contract_status
        return value


@dataclass
class DocumentResult:
    entry: FileEntry
    document: Document | None = None
    routes: list[Route] = field(default_factory=list)
    flags: list[Flag] = field(default_factory=list)
    candidates: list[Candidate] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    excluded_blocks: set[str] = field(default_factory=set)
    excluded_columns: dict[int, set[int]] = field(default_factory=dict)
    context: dict[str, tuple[str, str]] = field(default_factory=dict)  # normalized key -> (value, locator)
    extract: bool = True
    # (service reference, name) pairs seen in any table, e.g. a processes sheet keyed by IBS number.
    alias_hints: list[dict[str, str]] = field(default_factory=list)

    @property
    def route_names(self) -> list[str]:
        return sorted({r.route for r in self.routes})

    def flag(self, name: str, reason: str, locator: str = "", excerpt: str = "") -> None:
        if not any(f.flag == name and f.reason == reason and f.locator == locator for f in self.flags):
            self.flags.append(Flag(name, reason, locator, excerpt))

    def summary(self) -> dict[str, Any]:
        return {
            "path": self.entry.path, "format": self.entry.format, "status": self.entry.status,
            "reason": self.entry.reason, "sha256": self.entry.sha256,
            "duplicate_of": self.entry.duplicate_of,
            "routes": [asdict(r) for r in self.routes], "route_names": self.route_names,
            "flags": [asdict(f) for f in self.flags],
            "flag_names": sorted({f.flag for f in self.flags}),
            "extracted": self.extract,
            "context": {k: {"value": v, "locator": loc} for k, (v, loc) in sorted(self.context.items())},
            "document": self.document.summary() if self.document else None,
        }


def column_letter(index: int) -> str:
    index += 1
    letters = ""
    while index:
        index, rem = divmod(index - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def _sheet_ref(name: str) -> str:
    return name if name.replace("_", "").isalnum() else "'" + name.replace("'", "''") + "'"
