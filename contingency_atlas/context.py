"""Opt-in private organization context. No LLM, pipeline or public-store writes.

Records are typed declarations, not inferred enterprise facts. The denominator
is the declared service × mechanism matrix; an empty cell means unassessed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from .config import Settings

SCHEMA_VERSION = 1
SNAPSHOT_SCHEMA_VERSION = 2
MAX_WATCH_SCENARIOS = 2000
MAX_WATCH_EVIDENCE = 20000
Text = Annotated[str, Field(min_length=1, max_length=10000)]
Identifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")]


class ContextError(ValueError):
    """Invalid, disabled or unavailable organization workspace."""


class ContextConflict(ContextError):
    """The operator edited an older revision; reload before saving."""


class ContextRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    classification: Literal["organization", "synthetic"] = "organization"
    source: Text
    source_refs: list[Text] = Field(default_factory=list)
    notes: str = ""


class Service(ContextRecord):
    name: Text
    owner: Text
    criticality: Literal["critical", "high", "standard"] = "standard"
    impact_tolerance: Text
    legal_entity: str = ""
    region: str = ""
    process_ids: list[Text] = Field(default_factory=list)
    process_names: list[Text] = Field(default_factory=list)
    supplier_external_ids: list[Text] = Field(default_factory=list)


class Mechanism(ContextRecord):
    name: Text
    description: Text


class Dependency(ContextRecord):
    name: Text
    from_service_id: Identifier
    to_service_id: Identifier | None = None
    external_provider: str = ""
    category: Text
    common_cause: str = ""
    supplier_external_id: str = ""

    @model_validator(mode="after")
    def one_destination(self):
        if bool(self.to_service_id) == bool(self.external_provider):
            raise ValueError("declare exactly one destination service or external provider")
        if self.to_service_id == self.from_service_id:
            raise ValueError("a dependency must reference a different service")
        return self


class Control(ContextRecord):
    name: Text
    owner: Text
    service_ids: list[Identifier] = Field(min_length=1)
    mechanism_ids: list[Identifier] = Field(default_factory=list)
    validation_status: Literal["untested", "validated", "failed"] = "untested"
    validated_on: date | None = None
    evidence_refs: list[Text] = Field(default_factory=list)
    stale_after_days: Annotated[int, Field(ge=1, strict=True)] | None = None
    valid_until: date | None = None

    @model_validator(mode="after")
    def evidence_for_validation(self):
        if self.validation_status != "untested" and (not self.evidence_refs or not self.validated_on):
            raise ValueError("control validation requires a date and evidence references")
        if self.validated_on and self.validated_on > date.today():
            raise ValueError("control validation cannot be dated in the future")
        if self.valid_until and self.validated_on and self.valid_until < self.validated_on:
            raise ValueError("control validity cannot end before its validation date")
        return self


class Assumption(ContextRecord):
    statement: Text
    owner: Text
    service_ids: list[Identifier] = Field(default_factory=list)
    scenario_ids: list[Identifier] = Field(default_factory=list)
    expires_on: date
    test: Text
    status: Literal["open", "validated", "invalidated"] = "open"
    evidence_refs: list[Text] = Field(default_factory=list)

    @model_validator(mode="after")
    def evidence_for_resolution(self):
        if self.status != "open" and not self.evidence_refs:
            raise ValueError("resolving an assumption requires evidence references")
        return self


class Exposure(ContextRecord):
    scenario_id: Identifier
    service_id: Identifier
    mechanism_ids: list[Identifier] = Field(default_factory=list)
    rationale: Text
    status: Literal["proposed", "confirmed", "dismissed"] = "proposed"


class Action(ContextRecord):
    title: Text
    owner: Text
    due_date: date
    status: Literal["open", "in_progress", "blocked", "closed", "cancelled"] = "open"
    service_ids: list[Identifier] = Field(default_factory=list)
    scenario_ids: list[Identifier] = Field(default_factory=list)
    exposure_ids: list[Identifier] = Field(default_factory=list)
    control_ids: list[Identifier] = Field(default_factory=list)
    assumption_ids: list[Identifier] = Field(default_factory=list)
    closure_evidence: list[Text] = Field(default_factory=list)
    decision_type: Literal["investigate", "test", "remediate", "accept_risk"] = "investigate"
    rationale: str = ""
    decision_evidence: list[Text] = Field(default_factory=list)
    acceptance_expires_on: date | None = None

    @model_validator(mode="after")
    def evidence_for_closure(self):
        if self.status == "closed" and not (self.closure_evidence or (
            self.decision_type == "accept_risk" and self.decision_evidence
        )):
            raise ValueError("closing an action requires closure evidence")
        if self.decision_type == "accept_risk" and (
            not self.rationale.strip() or not self.acceptance_expires_on or not self.decision_evidence
        ):
            raise ValueError("risk acceptance requires an owner, rationale, expiry and decision evidence")
        return self


class Exercise(ContextRecord):
    title: Text
    scenario_ids: list[Identifier] = Field(min_length=1)
    service_ids: list[Identifier] = Field(min_length=1)
    injects: list[Text] = Field(default_factory=list)
    outcomes: list[Text] = Field(default_factory=list)
    status: Literal["planned", "completed"] = "planned"
    completed_on: date | None = None
    evidence_refs: list[Text] = Field(default_factory=list)

    @model_validator(mode="after")
    def completion_evidence(self):
        if self.status == "completed" and (
            not self.completed_on or not self.injects or not self.outcomes or not self.evidence_refs
        ):
            raise ValueError("completed exercises require a date, injects, outcomes and evidence")
        if self.completed_on and self.completed_on > date.today():
            raise ValueError("exercise completion cannot be dated in the future")
        return self


class Assessment(ContextRecord):
    service_id: Identifier
    mechanism_id: Identifier
    status: Literal["unassessed", "mapped", "tested"] = "unassessed"
    exposure_ids: list[Identifier] = Field(default_factory=list)
    exercise_ids: list[Identifier] = Field(default_factory=list)
    evidence_refs: list[Text] = Field(default_factory=list)
    assessed_on: date


class Observation(ContextRecord):
    title: Text
    service_ids: list[Identifier] = Field(default_factory=list)
    mechanism_ids: list[Identifier] = Field(default_factory=list)
    scenario_ids: list[Identifier] = Field(default_factory=list)
    observed_on: date
    evidence_refs: list[Text] = Field(min_length=1)
    description: Text


ImpactNumber = Annotated[float, Field(ge=0, allow_inf_nan=False, strict=True)]


class ImpactAssessment(ContextRecord):
    title: Text
    service_id: Identifier
    scenario_id: Identifier | None = None
    observed_on: date
    time_since_onset_minutes: ImpactNumber
    downtime_minutes: ImpactNumber | None = None
    backlog: ImpactNumber | None = None
    customers: Annotated[int, Field(ge=0, strict=True)] | None = None
    capacity_loss: Annotated[float, Field(ge=0, le=100, allow_inf_nan=False, strict=True)] | None = None
    basis: Literal["assumed", "measured"] = "assumed"
    evidence_refs: list[Text] = Field(default_factory=list)
    units: dict[str, str] = Field(default_factory=lambda: {
        "time_since_onset_minutes": "minutes", "downtime_minutes": "minutes",
        "backlog": "items", "customers": "count", "capacity_loss": "percent",
    })
    tolerance_reference: Text
    service_revision: Annotated[int, Field(ge=1, strict=True)] | None = None

    @model_validator(mode="after")
    def explicit_impact_basis(self):
        if all(getattr(self, field) is None for field in ("downtime_minutes", "backlog", "customers", "capacity_loss")):
            raise ValueError("impact assessment requires at least one explicit impact quantity; missing quantities remain unknown")
        if self.basis == "measured" and (not self.evidence_refs or self.observed_on > date.today()):
            raise ValueError("measured impact requires evidence and an observation date that is not in the future")
        required_units = {"time_since_onset_minutes": "minutes", "downtime_minutes": "minutes",
                          "customers": "count", "capacity_loss": "percent"}
        if set(self.units) - (set(required_units) | {"backlog"}):
            raise ValueError("unknown impact unit field")
        for field, unit in required_units.items():
            if getattr(self, field) is not None and self.units.get(field) != unit:
                raise ValueError(f"{field} units must be {unit}")
        if self.backlog is not None and not self.units.get("backlog", "").strip():
            raise ValueError("backlog units must be declared")
        return self


class SavedView(ContextRecord):
    name: Text
    view_kind: str
    filters: dict[str, str | list[str]] = Field(default_factory=dict)
    columns: list[str] = Field(default_factory=list)


class EvidenceAssessment(ContextRecord):
    """An attributed claim review, never an inferred independent event."""
    evidence_id: Identifier
    owner: Text
    claim: Text
    rationale: Text
    relation: Literal["corroborates", "contradicts", "context_only", "uncertain"]
    directness: Literal["direct_observation", "secondary_report", "inference", "unknown"] = "unknown"
    evidence_refs: list[Text] = Field(min_length=1)
    scenario_ids: list[Identifier] = Field(default_factory=list)
    source_family: str = ""
    incident_family: str = ""
    independence_status: Literal["not_assessed", "independent", "shared_origin", "uncertain"] = "not_assessed"
    independently_verified: StrictBool = False
    verified_on: date | None = None
    evidence_revision: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")] | None = None

    @model_validator(mode="after")
    def accountable_verification(self):
        if self.independence_status == "independent" and not self.independently_verified:
            raise ValueError("declaring source independence requires explicit verification")
        if self.independently_verified and (not self.verified_on or self.independence_status == "not_assessed"):
            raise ValueError("verified evidence assessment requires a date and an explicit independence assessment")
        if self.verified_on and self.verified_on > date.today():
            raise ValueError("evidence verification cannot be dated in the future")
        return self


RECORD_MODELS: dict[str, type[ContextRecord]] = {
    "services": Service, "mechanisms": Mechanism, "dependencies": Dependency,
    "controls": Control, "assumptions": Assumption, "exposures": Exposure,
    "actions": Action, "exercises": Exercise, "assessments": Assessment,
    "observations": Observation, "saved_views": SavedView,
    "impact_assessments": ImpactAssessment,
    "evidence_assessments": EvidenceAssessment,
}

_LINKS = {
    "service_id": "services", "service_ids": "services",
    "from_service_id": "services", "to_service_id": "services",
    "mechanism_id": "mechanisms", "mechanism_ids": "mechanisms",
    "exposure_ids": "exposures", "exercise_ids": "exercises",
    "control_ids": "controls", "assumption_ids": "assumptions",
}
_ENVELOPE = {"kind", "schema_version", "revision", "archived", "created_at", "updated_at",
             "effective_validation_status", "validation_stale_on", "effective_acceptance_status",
             "effective_evidence_state", "effective_verification_status", "current_evidence_review_status",
             "current_evidence_revision"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _actor(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 200:
        raise ContextError("an identified operator actor is required")
    return value.strip()


def _path(settings: Settings) -> Path:
    if not settings.context_enabled:
        raise ContextError("organization context is disabled; explicitly activate the private workspace")
    path = settings.context_db_path.expanduser().resolve()
    for public in (settings.scenario_db_path, settings.evidence_db_path,
                   settings.emerging_risks_path, settings.scenario_export_path):
        public = public.expanduser().resolve()
        if path == public or (path.exists() and public.exists() and path.samefile(public)):
            raise ContextError("organization context must use a separate private database")
    return path


def _migrate(conn: sqlite3.Connection) -> None:
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if tables and "context_meta" not in tables:
        raise ContextError("configured context database belongs to another application")
    if "context_meta" in tables:
        _check_version(conn)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS context_meta (schema_version INTEGER NOT NULL);
        INSERT INTO context_meta SELECT 1 WHERE NOT EXISTS (SELECT 1 FROM context_meta);
        CREATE TABLE IF NOT EXISTS context_records (
            id TEXT PRIMARY KEY, kind TEXT NOT NULL, schema_version INTEGER NOT NULL,
            revision INTEGER NOT NULL, archived INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL, data_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS context_records_kind ON context_records(kind, archived);
        CREATE TABLE IF NOT EXISTS context_decisions (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT, decision_id TEXT NOT NULL UNIQUE,
            recorded_at TEXT NOT NULL, actor TEXT NOT NULL, operation TEXT NOT NULL,
            kind TEXT NOT NULL, record_id TEXT NOT NULL, revision INTEGER NOT NULL,
            before_json TEXT, after_json TEXT NOT NULL, previous_hash TEXT NOT NULL,
            content_hash TEXT NOT NULL
        );
        CREATE TRIGGER IF NOT EXISTS context_decisions_no_update BEFORE UPDATE ON context_decisions
            BEGIN SELECT RAISE(ABORT, 'organization decisions are append-only'); END;
        CREATE TRIGGER IF NOT EXISTS context_decisions_no_delete BEFORE DELETE ON context_decisions
            BEGIN SELECT RAISE(ABORT, 'organization decisions are append-only'); END;
        CREATE TABLE IF NOT EXISTS context_baselines (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at TEXT NOT NULL,
            actor TEXT NOT NULL, store_revision INTEGER NOT NULL, snapshot_json TEXT NOT NULL,
            content_hash TEXT NOT NULL
        );
        CREATE TRIGGER IF NOT EXISTS context_baselines_no_update BEFORE UPDATE ON context_baselines
            BEGIN SELECT RAISE(ABORT, 'organization baselines are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS context_baselines_no_delete BEFORE DELETE ON context_baselines
            BEGIN SELECT RAISE(ABORT, 'organization baselines are immutable'); END;
    """)
    _check_version(conn)


def _check_version(conn: sqlite3.Connection) -> None:
    versions = conn.execute("SELECT schema_version FROM context_meta").fetchall()
    if len(versions) != 1 or versions[0][0] != SCHEMA_VERSION:
        raise ContextError("unsupported organization context schema version")


@contextmanager
def _store(settings: Settings, *, write: bool = False):
    path = _path(settings)
    if write:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not path.exists():
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.close(fd)
            except FileExistsError:
                pass
        conn = sqlite3.connect(path, timeout=5)
    else:
        if not path.is_file():
            raise ContextError("organization context store has not been created")
        conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        if write:
            _migrate(conn)
            conn.execute("BEGIN IMMEDIATE")
        else:
            _check_version(conn)
            conn.execute("BEGIN")
        yield conn
        if write:
            conn.commit()
    except sqlite3.DatabaseError as exc:
        conn.rollback()
        raise ContextError("organization context database could not complete the operation") from exc
    finally:
        conn.close()


def _record(row: sqlite3.Row) -> dict[str, Any]:
    return {
        **json.loads(row["data_json"]), "id": row["id"], "kind": row["kind"],
        "schema_version": row["schema_version"], "revision": row["revision"],
        "archived": bool(row["archived"]), "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _records(conn: sqlite3.Connection, *, include_archived: bool = False) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {kind: [] for kind in RECORD_MODELS}
    for row in conn.execute(
        "SELECT * FROM context_records " + ("" if include_archived else "WHERE archived = 0 ") + "ORDER BY kind, id"
    ):
        if row["kind"] not in grouped or row["schema_version"] != SCHEMA_VERSION:
            raise ContextError("unsupported organization record kind or schema version")
        grouped[row["kind"]].append(_record(row))
    return grouped


def _find(conn: sqlite3.Connection, kind: str, record_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM context_records WHERE id = ? AND kind = ?", (record_id, kind)).fetchone()
    return _record(row) if row else None


def _scenario_ids(settings: Settings) -> set[str]:
    path = settings.scenario_db_path.resolve()
    if not path.is_file():
        return set()
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        return {row[0] for row in conn.execute("SELECT scenario_id FROM scenarios")}
    except sqlite3.DatabaseError as exc:
        raise ContextError("public scenario references could not be verified") from exc
    finally:
        conn.close()


def _public_evidence(settings: Settings, ids: set[str]) -> dict[str, dict]:
    """Resolve only explicitly referenced public evidence without store writes."""
    path = settings.evidence_db_path.resolve()
    if not ids or not path.is_file():
        return {}
    conn = None
    try:
        conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")
        found = {}
        ordered = sorted(ids)
        for start in range(0, len(ordered), 400):
            part = ordered[start:start + 400]
            marks = ",".join("?" for _ in part)
            for row in conn.execute(f"SELECT * FROM evidence_events WHERE event_id IN ({marks})", part):
                if row["event_id"] in found:
                    raise ContextError("public evidence identity is ambiguous")
                found[row["event_id"]] = dict(row)
        return found
    except sqlite3.DatabaseError as exc:
        raise ContextError("public evidence references could not be verified") from exc
    finally:
        if conn:
            conn.close()


def _links(record: dict) -> list[tuple[str, str]]:
    links = []
    for field, kind in _LINKS.items():
        value = record.get(field)
        links.extend((kind, record_id) for record_id in (value if isinstance(value, list) else [value]) if record_id)
    return links


def _scenario_refs(record: dict) -> set[str]:
    return set(record.get("scenario_ids", [])) | ({record["scenario_id"]} if record.get("scenario_id") else set())


def _assessment_state(assessment: dict, by_id: dict[str, dict], scenarios: set[str]) -> str:
    if assessment["status"] == "unassessed":
        return "unassessed"
    exposures = [by_id.get(record_id) for record_id in assessment.get("exposure_ids", [])]
    mappings = [e for e in exposures if e and not e["archived"] and e.get("status") == "confirmed"
                and e.get("service_id") == assessment["service_id"]
                and assessment["mechanism_id"] in e.get("mechanism_ids", [])
                and e.get("scenario_id") in scenarios]
    if not mappings:
        return "unassessed"
    if assessment["status"] == "tested" and assessment.get("evidence_refs"):
        mapped_scenarios = {e["scenario_id"] for e in mappings}
        for record_id in assessment.get("exercise_ids", []):
            exercise = by_id.get(record_id)
            if (exercise and not exercise["archived"] and exercise.get("status") == "completed"
                    and assessment["service_id"] in exercise.get("service_ids", [])
                    and mapped_scenarios.intersection(exercise.get("scenario_ids", []))
                    and exercise.get("completed_on", "") <= assessment["assessed_on"]):
                return "tested"
    return "mapped"


def _validate_links(conn: sqlite3.Connection, settings: Settings, kind: str, data: dict, record_id: str) -> None:
    for target_kind, target_id in _links(data):
        target = _find(conn, target_kind, target_id)
        if target is None or target["archived"]:
            raise ContextError(f"missing or archived {target_kind} reference: {target_id}")
    scenarios = _scenario_ids(settings) if _scenario_refs(data) or kind == "assessments" else set()
    missing = _scenario_refs(data) - scenarios
    if missing:
        raise ContextError(f"unknown scenario reference(s): {', '.join(sorted(missing))}")
    if kind == "evidence_assessments":
        from .decisions import record_revision

        evidence = _public_evidence(settings, {data["evidence_id"]}).get(data["evidence_id"])
        if evidence is None:
            raise ContextError(f"unknown public evidence reference: {data['evidence_id']}")
        current_revision = record_revision(evidence)
        if data["evidence_revision"] is None and _find(conn, kind, record_id) is not None:
            raise ContextConflict("an evidence assessment update requires the exact inspected source evidence revision")
        if data["evidence_revision"] is not None and data["evidence_revision"] != current_revision:
            raise ContextConflict("source evidence changed; review it and supply its current evidence revision")
        data["evidence_revision"] = current_revision
    if kind == "saved_views":
        if data["view_kind"] not in RECORD_MODELS or data["view_kind"] == "saved_views":
            raise ContextError("saved view must name a supported record kind")
        fields = set(RECORD_MODELS[data["view_kind"]].model_fields) | {"id", "revision", "archived"}
        if not set(data["filters"]) <= fields or not set(data["columns"]) <= fields:
            raise ContextError("saved view contains unknown filter or column fields")
    if kind == "assessments":
        records = _records(conn)
        if any(a["id"] != record_id and (a["service_id"], a["mechanism_id"]) ==
               (data["service_id"], data["mechanism_id"]) for a in records["assessments"]):
            raise ContextError("this service and mechanism already have an active assessment")
        by_id = {r["id"]: r for rows in records.values() for r in rows}
        if _assessment_state(data, by_id, scenarios) != data["status"]:
            raise ContextError("assessment status requires confirmed exposure mapping and, for tested, a scoped completed exercise with evidence")
    if kind == "impact_assessments":
        service = _find(conn, "services", data["service_id"])
        if data["service_revision"] is not None and data["service_revision"] != service["revision"]:
            raise ContextConflict("service tolerance or scope changed; reload before recording its impact")
        data["service_revision"] = service["revision"]


def _revision(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COALESCE(MAX(sequence), 0) FROM context_decisions").fetchone()[0]


def _decide(conn: sqlite3.Connection, *, actor: str, operation: str, kind: str,
            record_id: str, revision: int, before: dict | None, after: dict) -> None:
    row = conn.execute("SELECT content_hash FROM context_decisions ORDER BY sequence DESC LIMIT 1").fetchone()
    previous = row[0] if row else ""
    payload = {"decision_id": f"ctx-decision-{uuid.uuid4().hex}", "recorded_at": _now(),
               "actor": actor, "operation": operation, "kind": kind, "record_id": record_id,
               "revision": revision, "before": before, "after": after, "previous_hash": previous}
    conn.execute(
        "INSERT INTO context_decisions(decision_id, recorded_at, actor, operation, kind, record_id, "
        "revision, before_json, after_json, previous_hash, content_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (payload["decision_id"], payload["recorded_at"], actor, operation, kind, record_id, revision,
         _json(before) if before is not None else None, _json(after), previous, _hash(payload)),
    )


def _save(conn: sqlite3.Connection, settings: Settings, kind: str, data: dict,
          actor: str, expected_revision: int | None) -> dict:
    clean = validate_record(kind, data)
    record_id = clean.pop("id", None) or f"ctx-{kind}-{uuid.uuid4().hex[:16]}"
    if expected_revision is not None and (type(expected_revision) is not int or expected_revision < 0):
        raise ContextConflict("expected revision must be a non-negative integer")
    prior = _find(conn, kind, record_id)
    if prior:
        if expected_revision is None or isinstance(expected_revision, bool) or expected_revision != prior["revision"]:
            raise ContextConflict("record changed or revision omitted; reload before saving")
        if prior["archived"]:
            raise ContextError("archived records cannot be edited")
    elif expected_revision not in (None, 0):
        raise ContextConflict("record no longer exists at the expected revision")
    _validate_links(conn, settings, kind, clean, record_id)
    now = _now()
    revision = prior["revision"] + 1 if prior else 1
    conn.execute(
        "INSERT INTO context_records(id, kind, schema_version, revision, archived, created_at, updated_at, data_json) "
        "VALUES(?,?,?,?,0,?,?,?) ON CONFLICT(id) DO UPDATE SET "
        "revision=excluded.revision, updated_at=excluded.updated_at, data_json=excluded.data_json "
        "WHERE context_records.kind=excluded.kind",
        (record_id, kind, SCHEMA_VERSION, revision, prior["created_at"] if prior else now, now, _json(clean)),
    )
    after = _find(conn, kind, record_id)
    if after is None:
        raise ContextError("record identifier already belongs to another kind")
    _decide(conn, actor=actor, operation="update" if prior else "create", kind=kind,
            record_id=record_id, revision=revision, before=prior, after=after)
    return after


def validate_record(kind: str, data: dict) -> dict:
    """Pure syntactic import/form validation; references are checked at save.

    Accept a returned record for editing, preserving an explicit stable ID
    while excluding server-owned revision/timestamp/archive fields.
    """
    if kind not in RECORD_MODELS:
        raise ContextError(f"unknown context record kind: {kind}")
    if not isinstance(data, dict):
        raise ContextError("context record must be an object")
    record_id = data.get("id")
    if record_id is not None and (not isinstance(record_id, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", record_id
    )):
        raise ContextError("invalid stable record identifier")
    incoming = {key: value for key, value in data.items() if key != "id" and key not in _ENVELOPE}
    clean = RECORD_MODELS[kind].model_validate(incoming).model_dump(mode="json")
    if record_id is not None:
        clean["id"] = record_id
    return clean


def save_record(settings: Settings, kind: str, data: dict, *, actor: str,
                expected_revision: int | None = None) -> dict:
    """Create or replace a typed record; updates require its current revision."""
    actor = _actor(actor)
    with _store(settings, write=True) as conn:
        return _save(conn, settings, kind, data, actor, expected_revision)


def archive_record(settings: Settings, kind: str, record_id: str, *, actor: str,
                   expected_revision: int | None = None) -> dict:
    actor = _actor(actor)
    with _store(settings, write=True) as conn:
        prior = _find(conn, kind, record_id)
        if prior is None:
            raise ContextError("context record not found")
        if expected_revision is None or isinstance(expected_revision, bool) or expected_revision != prior["revision"]:
            raise ContextConflict("record changed or revision omitted; reload before archiving")
        if prior["archived"]:
            raise ContextError("record is already archived")
        for rows in _records(conn).values():
            for record in rows:
                if (kind, record_id) in _links(record):
                    raise ContextError(f"record is still referenced by {record['kind']}: {record['id']}")
        conn.execute("UPDATE context_records SET archived=1, revision=revision+1, updated_at=? WHERE id=?",
                     (_now(), record_id))
        after = _find(conn, kind, record_id)
        _decide(conn, actor=actor, operation="archive", kind=kind, record_id=record_id,
                revision=after["revision"], before=prior, after=after)
        return after


def import_records(settings: Settings, records: list[dict], *, actor: str) -> list[dict]:
    """Atomic import of explicit typed declarations, in dependency order.

    Entries use {kind, data, expected_revision?}; imported updates have the
    same optimistic revision requirement as individual edits.
    """
    actor = _actor(actor)
    if not isinstance(records, list) or not records or len(records) > 500:
        raise ContextError("import requires 1 to 500 typed records")
    order = {kind: i for i, kind in enumerate([
        "services", "mechanisms", "dependencies", "controls", "assumptions", "exposures",
        "exercises", "actions", "assessments", "observations", "impact_assessments", "saved_views",
    ])}
    if any(not isinstance(item, dict) or item.get("kind") not in order or "data" not in item for item in records):
        raise ContextError("import entries require a supported kind and data object")
    with _store(settings, write=True) as conn:
        return [_save(conn, settings, item["kind"], item["data"], actor, item.get("expected_revision"))
                for item in sorted(records, key=lambda r: order[r["kind"]])]


def _coverage(records: dict[str, list[dict]], scenarios: set[str]) -> dict:
    by_id = {r["id"]: r for rows in records.values() for r in rows}
    assessments = {(a["service_id"], a["mechanism_id"]): a for a in records["assessments"]}
    rows = []
    counts = {"unassessed": 0, "mapped": 0, "tested": 0}
    for service in records["services"]:
        for mechanism in records["mechanisms"]:
            assessment = assessments.get((service["id"], mechanism["id"]))
            status = _assessment_state(assessment, by_id, scenarios) if assessment else "unassessed"
            counts[status] += 1
            rows.append({"service_id": service["id"], "service_name": service["name"],
                         "owner": service["owner"], "mechanism_id": mechanism["id"],
                         "mechanism_name": mechanism["name"], "status": status,
                         "assessment_id": assessment["id"] if assessment else None,
                         "declared_status": assessment["status"] if assessment else None,
                         "needs_reassessment": bool(assessment and status != assessment["status"])})
    return {"denominator": len(rows), "denominator_definition": "declared active services × declared active mechanisms",
            "scope_status": "declared" if rows else "not_declared", "counts": counts, "rows": rows,
            "tested_fraction": counts["tested"] / len(rows) if rows else None,
            "interpretation": "Coverage records what was assessed, mapped or exercised. Missing records mean unassessed, not zero risk; tested does not mean safe."}


def _overdue(records: dict[str, list[dict]], today: date) -> list[dict]:
    overdue = []
    for kind, field, closed in [("actions", "due_date", {"closed", "cancelled"}),
                                ("assumptions", "expires_on", {"invalidated"})]:
        for record in records[kind]:
            if kind == "actions" and record.get("decision_type") == "accept_risk":
                expiry = record.get("acceptance_expires_on")
                if record["status"] != "cancelled" and expiry and date.fromisoformat(expiry) < today:
                    overdue.append({"kind": kind, "id": record["id"], "owner": record["owner"],
                                    "title": record["title"], "due_date": expiry, "status": record["status"],
                                    "reason": "risk_acceptance_expired"})
                continue
            if record["status"] not in closed and date.fromisoformat(record[field]) < today:
                overdue.append({"kind": kind, "id": record["id"], "owner": record["owner"],
                                "title": record.get("title") or record.get("statement"),
                                "due_date": record[field], "status": record["status"]})
    return sorted(overdue, key=lambda r: (r["due_date"], r["id"]))


def _control_state(record: dict, today: date) -> tuple[str, str | None]:
    declared = record.get("validation_status", "untested")
    if declared == "untested":
        return "untested", None
    expiry_dates = []
    try:
        if record.get("valid_until"):
            expiry_dates.append(date.fromisoformat(record["valid_until"]))
        if record.get("stale_after_days") and record.get("validated_on"):
            expiry_dates.append(date.fromisoformat(record["validated_on"]) + timedelta(days=record["stale_after_days"]))
    except (ValueError, TypeError, OverflowError):
        return "stale", None
    stale_on = min(expiry_dates) if expiry_dates else None
    return ("stale" if stale_on and stale_on < today else declared), stale_on.isoformat() if stale_on else None


def _control_diagnostics(records: list[dict], today: date) -> dict:
    counts = {state: 0 for state in ("untested", "validated", "failed", "stale")}
    rows = []
    for record in records:
        effective, stale_on = _control_state(record, today)
        counts[effective] += 1
        rows.append({"id": record["id"], "name": record["name"], "owner": record["owner"],
                     "declared_status": record["validation_status"], "effective_status": effective,
                     "stale_on": stale_on, "validity_declared": bool(record.get("valid_until") or record.get("stale_after_days"))})
    return {"as_of": today.isoformat(), "counts": counts, "records": rows,
            "interpretation": "Staleness is derived from the operator's declared validity window. Historical validation decisions are retained; no expiry rule means validity was not declared, not indefinite assurance."}


def _acceptance_state(record: dict, today: date) -> str:
    if record.get("decision_type") != "accept_risk":
        return "not_applicable"
    if record.get("status") == "cancelled":
        return "cancelled"
    if not record.get("acceptance_expires_on") or not record.get("decision_evidence") or not record.get("rationale"):
        return "not_validated"
    return "expired" if date.fromisoformat(record["acceptance_expires_on"]) < today else "active"


def _annotate_evidence_assessments(settings: Settings, records: list[dict]) -> dict:
    from .decisions import record_revision

    available = True
    try:
        evidence = _public_evidence(settings, {record["evidence_id"] for record in records})
    except ContextError:
        evidence, available = {}, False
    counts = {state: 0 for state in ("current", "changed", "missing", "unavailable")}
    families = {field: {} for field in ("source_family", "incident_family")}
    for record in records:
        target = evidence.get(record["evidence_id"])
        target_revision = record_revision(target) if target is not None else None
        state = ("unavailable" if not available else "missing" if target is None
                 else "current" if target_revision == record.get("evidence_revision") else "changed")
        record["effective_evidence_state"] = state
        record["current_evidence_review_status"] = target.get("review_status") if target else None
        record["current_evidence_revision"] = target_revision
        record["effective_verification_status"] = (
            "unverified" if not record["independently_verified"] else "verified" if state == "current" else "needs_reassessment")
        counts[state] += 1
        for field in families:
            if value := record.get(field):
                families[field].setdefault(value, []).append(record["id"])
    return {"status": "available" if available else "unavailable", "counts": counts,
            **{field + "_declarations": [{"label": label, "assessment_ids": sorted(ids)}
                                         for label, ids in sorted(groups.items())] for field, groups in families.items()},
            "interpretation": "Source and incident families, directness, contradictions and verification are named operator declarations. Matching labels do not independently establish events or corroboration; changed evidence requires reassessment."}


def context_payload(settings: Settings) -> dict:
    """Read-only dashboard data. A disabled or unused workspace creates nothing."""
    schemas = {kind: model.model_json_schema() for kind, model in RECORD_MODELS.items()}
    records = {kind: [] for kind in RECORD_MODELS}
    payload = {"enabled": settings.context_enabled, "schema_version": SCHEMA_VERSION,
               "records": records, "schemas": schemas, "coverage": _coverage(records, set()),
               "overdue": [], "changes": [], "baselines": [], "store_revision": 0,
               "control_diagnostics": _control_diagnostics([], date.today()),
               "evidence_assessment_diagnostics": _annotate_evidence_assessments(settings, []),
               "privacy_notice": "Private organization workspace; excluded from the public pipeline, LLM requests and default exports."}
    if not settings.context_enabled:
        return payload
    if not _path(settings).is_file():
        return payload
    scenarios = _scenario_ids(settings)
    with _store(settings) as conn:
        records = _records(conn)
        for record in records["controls"]:
            effective, stale_on = _control_state(record, date.today())
            record.update(effective_validation_status=effective, validation_stale_on=stale_on)
        for record in records["actions"]:
            record["effective_acceptance_status"] = _acceptance_state(record, date.today())
        payload.update({"records": records, "coverage": _coverage(records, scenarios),
                        "overdue": _overdue(records, date.today()), "store_revision": _revision(conn),
                        "control_diagnostics": _control_diagnostics(records["controls"], date.today()),
                        "evidence_assessment_diagnostics": _annotate_evidence_assessments(settings, records["evidence_assessments"])})
        payload["changes"] = [dict(row) for row in conn.execute(
            "SELECT sequence, decision_id, recorded_at, actor, operation, kind, record_id, revision "
            "FROM context_decisions ORDER BY sequence DESC LIMIT 50"
        )]
        payload["baselines"] = [dict(row) for row in conn.execute(
            "SELECT id, name, created_at, actor, store_revision, content_hash "
            "FROM context_baselines ORDER BY created_at DESC, id"
        )]
        payload["missing_scenario_refs"] = sorted(
            {s for rows in records.values() for record in rows for s in _scenario_refs(record)} - scenarios
        )
    return payload


def _research_rows(path: Path, table: str, id_field: str, *, ids: set[str] | None = None,
                   current_only: bool = False, retain_ids: set[str] | None = None,
                   maximum: int = MAX_WATCH_EVIDENCE) -> tuple[list[dict], dict]:
    """One bounded, read-only public-store snapshot. SQL identifiers are constants."""
    window = {"started_at": _now(), "completed_at": None, "status": "missing_store"}
    conn = None
    try:
        if not path.is_file():
            return [], window
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")
        columns = {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}
        if id_field not in columns:
            window["status"] = "missing_table_or_id"
            return [], window
        rows = []
        if ids is None:
            where = "WHERE COALESCE(NULLIF(trim(status), ''), 'active') = 'active'" if current_only and "status" in columns else ""
            rows = [dict(row) for row in conn.execute(
                f'SELECT * FROM "{table}" {where} ORDER BY "{id_field}" LIMIT ?', (maximum + 1,))]
            if len(rows) > maximum:
                raise ContextError(f"research watch scope exceeds {maximum} records; select fewer scenarios")
            ids = (retain_ids or set()) - {row[id_field] for row in rows}
        ordered = sorted(ids)
        for start in range(0, len(ordered), 400):
            part = ordered[start:start + 400]
            marks = ",".join("?" for _ in part)
            rows.extend(dict(row) for row in conn.execute(
                f'SELECT * FROM "{table}" WHERE "{id_field}" IN ({marks}) ORDER BY "{id_field}"', part))
        # Retained historical scenarios are allowed in addition to the current
        # scope, so a retirement is visible as a change rather than disappearance.
        if len(rows) > maximum + len(retain_ids or set()):
            raise ContextError(f"research watch scope exceeds {maximum} records; select fewer scenarios")
        if any(not isinstance(row[id_field], str) or not row[id_field].strip() for row in rows) or len({row[id_field] for row in rows}) != len(rows):
            window["status"] = "invalid_or_duplicate_ids"
            return [], window
        window["status"] = "available"
        return sorted(rows, key=lambda row: row[id_field]), window
    except sqlite3.DatabaseError:
        window["status"] = "unreadable_store"
        return [], window
    finally:
        window["completed_at"] = _now()
        if conn:
            conn.close()


def _research_refs(raw: object) -> tuple[list[str], bool]:
    if raw is None or raw == "":
        return [], False
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return [], True
    if not isinstance(raw, list) or any(not isinstance(ref, str) or not ref.strip() for ref in raw):
        return [], True
    return sorted({ref.strip() for ref in raw}), False


def _watch_scope(scenario_ids: list[str] | None) -> dict:
    if scenario_ids is not None:
        if (not isinstance(scenario_ids, list) or not 1 <= len(scenario_ids) <= MAX_WATCH_SCENARIOS
                or any(not isinstance(value, str) or not value.strip() for value in scenario_ids)):
            raise ContextError(f"a watchlist requires 1–{MAX_WATCH_SCENARIOS} explicit scenario IDs")
        scenario_ids = sorted({value.strip() for value in scenario_ids})
    return {"mode": "selected" if scenario_ids is not None else "all_current",
            "scenario_ids": scenario_ids, "maximum_current_scenarios": MAX_WATCH_SCENARIOS}


def _capture_research(settings: Settings, scope: dict, *, retain_ids: set[str] | None = None,
                      validate_selection: bool = False) -> dict:
    from .schema import EVIDENCE_EVENT_COLUMNS, SCENARIO_COLUMNS

    selected = set(scope["scenario_ids"]) if scope["mode"] == "selected" else None
    scenarios, scenario_window = _research_rows(settings.scenario_db_path, "scenarios", "scenario_id",
        ids=selected, current_only=True, retain_ids=retain_ids, maximum=MAX_WATCH_SCENARIOS)
    if validate_selection and selected is not None:
        if scenario_window["status"] != "available":
            raise ContextError("the public scenario store is unavailable; the watchlist could not be verified")
        if missing := selected - {row["scenario_id"] for row in scenarios}:
            raise ContextError(f"unknown watchlist scenario IDs: {', '.join(sorted(missing))}")
    captured = {}
    links: dict[str, list[dict]] = {}
    malformed = []
    for raw in scenarios:
        record = {field: raw.get(field) for field, _ in SCENARIO_COLUMNS if field in raw}
        record["scope_state"] = "current" if (raw.get("status") or "active").strip() == "active" else "historical"
        review = (raw.get("signpost_review_status") or "unreviewed").strip()
        record["signpost_state"] = ("historical" if record["scope_state"] == "historical" or review in {"superseded", "rejected"}
                                     else "reviewed_projection" if review == "approved" else "unreviewed")
        for field in ("evidence_refs", "signpost_evidence_refs", "signpost_checked_evidence_refs"):
            refs, invalid = _research_refs(raw.get(field))
            if invalid:
                malformed.append({"scenario_id": raw["scenario_id"], "field": field})
            if field in raw:
                record[field] = refs if not invalid else raw[field]
            for ref in refs:
                links.setdefault(ref, []).append({"scenario_id": raw["scenario_id"], "role": field,
                    "scope_state": record["scope_state"],
                    "assessment_state": record["signpost_state"] if field.startswith("signpost_") else "research_reference"})
        record["content_hash"] = _hash(record)
        captured[raw["scenario_id"]] = record
    if len(links) > MAX_WATCH_EVIDENCE:
        raise ContextError(f"research watch scope exceeds {MAX_WATCH_EVIDENCE} linked sources; select fewer scenarios")
    evidence_window = {"status": "not_required", "started_at": _now(), "completed_at": _now()}
    evidence = []
    if links:
        evidence, evidence_window = _research_rows(settings.evidence_db_path, "evidence_events", "event_id", ids=set(links))
    elif scenario_window["status"] != "available" or malformed:
        evidence_window["status"] = "unverified_scope"
    sources = {}
    for raw in evidence:
        record = {field: raw.get(field) for field, _ in EVIDENCE_EVENT_COLUMNS if field in raw}
        # Keep source metadata and content hashes, not a second copy of source
        # summaries/inferences in every review baseline.
        for field in ("independently_authored_summary", "research_inference", "provenance_note"):
            if field in record:
                record[field + "_hash"] = _hash(record.pop(field))
        record["links"] = sorted(links[raw["event_id"]], key=lambda link: (link["scenario_id"], link["role"]))
        record["review_state"] = "approved" if raw.get("review_status") in {"approved", "approved_synthetic"} else "not_approved"
        record["content_hash"] = _hash(record)
        sources[raw["event_id"]] = record
    available = scenario_window["status"] == "available" and evidence_window["status"] in {"available", "not_required"} and not malformed
    return {"status": "captured" if available else "partial" if scenario_window["status"] == "available" else "unavailable",
            "as_of": _now(), "scope": scope, "scenarios": captured, "evidence": sources,
            "read_windows": {"scenarios": scenario_window, "evidence": evidence_window},
            "malformed_references": malformed,
            "missing_evidence_ids": sorted(set(links) - sources.keys()) if evidence_window["status"] == "available" else []}


def _research_date(value: object) -> datetime | None:
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def _research_diff(before: dict, after: dict, *, kind: str, baseline_as_of: str) -> dict:
    changed = []
    for key in sorted(before.keys() & after.keys()):
        fields = [{"field": field, "before": before[key].get(field), "after": after[key].get(field)}
                  for field in sorted(before[key].keys() | after[key].keys())
                  if field != "content_hash" and before[key].get(field) != after[key].get(field)]
        if fields:
            changed.append({"id": key, "before": before[key], "after": after[key], "fields": fields})
    added = [after[key] for key in sorted(after.keys() - before.keys())]
    if kind == "evidence":
        cutoff = _research_date(baseline_as_of)
        added = [{**record, "publication_relative_to_baseline":
                  "unknown" if _research_date(record.get("source_date")) is None or cutoff is None
                  else "historical_publication" if _research_date(record.get("source_date")) <= cutoff
                  else "publication_after_baseline"} for record in added]
    removed = [before[key] for key in sorted(before.keys() - after.keys())]
    return {"status": "compared", "added": added, "changed": changed, "removed": removed,
            "counts": {"added": len(added), "changed": len(changed), "removed": len(removed)}}


def _compare_research(settings: Settings, before: dict | None) -> dict:
    if before is None:
        return {"status": "not_captured", "notes": ["This baseline predates public-research capture; no historical source state is inferred."]}
    after = _capture_research(settings, before["scope"], retain_ids=set(before["scenarios"]))
    result = {"status": "compared", "scope": before["scope"], "baseline_as_of": before["as_of"],
              "current_as_of": after["as_of"], "read_windows": {"baseline": before["read_windows"], "current": after["read_windows"]},
              "missing_evidence_ids": {"baseline": before["missing_evidence_ids"], "current": after["missing_evidence_ids"]},
              "notes": ["These are observed store snapshots in separate read windows, not a cross-store transaction or a reconstructed event-time history.",
                        "Added means newly present or linked in this scope. Publication and retrieval dates do not establish a new incident or source independence.",
                        "Removed evidence means absent from the current linked scope, including unlinking; it does not by itself establish source deletion.",
                        "Historical scenarios and unreviewed or superseded signpost projections are labeled; a change is not confirmed counterevidence."]}
    for kind in ("scenarios", "evidence"):
        allowed = {"available"} if kind == "scenarios" else {"available", "not_required"}
        baseline_ok = before["read_windows"][kind]["status"] in allowed
        current_ok = after["read_windows"][kind]["status"] in allowed
        if kind == "evidence":
            baseline_ok = baseline_ok and not before["malformed_references"]
            current_ok = current_ok and not after["malformed_references"]
        if baseline_ok and current_ok:
            result[kind] = _research_diff(before[kind], after[kind], kind=kind, baseline_as_of=before["as_of"])
        else:
            result[kind] = {"status": "unavailable" if baseline_ok else "not_captured",
                            "added": [], "changed": [], "removed": [], "counts": None}
            result["status"] = "partial"
    return result


def create_baseline(settings: Settings, name: str, *, actor: str,
                    expected_revision: int | None = None, scenario_ids: list[str] | None = None) -> dict:
    actor = _actor(actor)
    if not isinstance(name, str) or not name.strip():
        raise ContextError("baseline name is required")
    scope = _watch_scope(scenario_ids)
    with _store(settings, write=True) as conn:
        revision = _revision(conn)
        if expected_revision is not None and (isinstance(expected_revision, bool) or expected_revision != revision):
            raise ContextConflict("organization workspace changed; reload before creating the baseline")
        records = _records(conn, include_archived=True)
        research = _capture_research(settings, scope, validate_selection=True)
        snapshot = {"schema_version": SNAPSHOT_SCHEMA_VERSION, "records": records, "research": research}
        baseline = {"id": f"ctx-baseline-{uuid.uuid4().hex[:16]}", "name": name.strip(),
                    "created_at": _now(), "actor": actor, "store_revision": revision,
                    "content_hash": _hash(snapshot), "snapshot_schema_version": SNAPSHOT_SCHEMA_VERSION,
                    "research_scope": scope, "research_status": research["status"]}
        conn.execute("INSERT INTO context_baselines VALUES(?,?,?,?,?,?,?)",
                     (baseline["id"], baseline["name"], baseline["created_at"], actor, revision,
                      _json(snapshot), baseline["content_hash"]))
        _decide(conn, actor=actor, operation="baseline", kind="baselines", record_id=baseline["id"],
                revision=1, before=None, after=baseline)
        return baseline


def compare_baseline(settings: Settings, baseline_id: str) -> dict:
    with _store(settings) as conn:
        row = conn.execute("SELECT * FROM context_baselines WHERE id=?", (baseline_id,)).fetchone()
        if row is None:
            raise ContextError("organization baseline not found")
        snapshot = json.loads(row["snapshot_json"])
        if _hash(snapshot) != row["content_hash"]:
            raise ContextError("organization baseline integrity check failed")
        if snapshot.get("schema_version") not in {1, SNAPSHOT_SCHEMA_VERSION}:
            raise ContextError("unsupported organization baseline snapshot version")
        before = {r["id"]: r for rows in snapshot["records"].values() for r in rows}
        after = {r["id"]: r for rows in _records(conn, include_archived=True).values() for r in rows}
        added = [after[key] for key in sorted(after.keys() - before.keys())]
        changed = [{"id": key, "kind": after[key]["kind"], "before": before[key], "after": after[key]}
                   for key in sorted(before.keys() & after.keys()) if before[key] != after[key]]
        removed = [before[key] for key in sorted(before.keys() - after.keys())]
        return {"baseline_id": baseline_id, "baseline_name": row["name"],
                "store_revision": _revision(conn), "added": added, "changed": changed, "removed": removed,
                "counts": {"added": len(added), "changed": len(changed), "removed": len(removed)},
                "research_changes": _compare_research(settings, snapshot.get("research"))}


def export_context(settings: Settings, *, scenario_ids: list[str]) -> dict[str, list[dict]]:
    """Explicit scoped private export, with no migrations or default inclusion.

    Select direct scenario-linked records and their referenced context only.
    Dependencies are limited to the selected services' immediate outgoing
    links; expanding a dependency does not traverse the entire organization.
    """
    if not scenario_ids:
        raise ContextError("private context export requires explicit scenario selections")
    roots = set(scenario_ids)
    if not roots <= _scenario_ids(settings):
        raise ContextError("private context export references unknown scenarios")
    with _store(settings) as conn:
        records = _records(conn)
    by_id = {r["id"]: r for rows in records.values() for r in rows}
    selected = {r["id"] for r in by_id.values()
                if _scenario_refs(r) & roots and _scenario_refs(r) <= roots}
    service_ids = {target for rid in selected for kind, target in _links(by_id[rid]) if kind == "services"}
    selected |= {r["id"] for r in records["dependencies"] if r["from_service_id"] in service_ids}
    selected |= {r["id"] for r in records["controls"] if service_ids.intersection(r["service_ids"])
                 and set(r["service_ids"]) <= service_ids}
    for kind in ("assumptions", "actions", "observations", "impact_assessments"):
        for record in records[kind]:
            services = set(record.get("service_ids", [])) | ({record["service_id"]} if record.get("service_id") else set())
            linked_context = {target for linked_kind, target in _links(record)
                              if linked_kind in {"exposures", "controls", "assumptions"}}
            if (_scenario_refs(record) <= roots and services <= service_ids
                    and ((services and services & service_ids)
                         or (linked_context and linked_context <= selected))):
                selected.add(record["id"])
    selected |= {r["id"] for r in records["assessments"] if r["service_id"] in service_ids
                 and r["exposure_ids"] and set(r["exposure_ids"]) <= selected
                 and all(not (_scenario_refs(by_id[eid]) - roots)
                         for eid in r["exercise_ids"] if eid in by_id)}
    # Include only objects referenced by selected records. Records containing
    # unrelated scenario scopes are not exported in full accidentally.
    pending = list(selected)
    while pending:
        current = by_id[pending.pop()]
        for _kind, target_id in _links(current):
            if target_id in by_id and target_id not in selected:
                target = by_id[target_id]
                if _scenario_refs(target) - roots:
                    continue
                selected.add(target_id)
                pending.append(target_id)
    out = {kind: [] for kind in RECORD_MODELS if kind != "saved_views"}
    for record_id in sorted(selected):
        record = by_id[record_id]
        if _scenario_refs(record) - roots:
            # A shared exercise/action may contain prose about other scenarios;
            # omit it rather than redact identifiers while leaking its text.
            continue
        out[record["kind"]].append(record)
    _annotate_evidence_assessments(settings, out["evidence_assessments"])
    return out
