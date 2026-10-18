"""I govern scenario-to-external-framework applicability mappings.

MITRE ATLAS mappings are advisory overlays.  I keep the proposed claim
(scenario snapshot, pinned catalog release, technique, and rationale)
append-only, and expose one deliberately narrow state transition for a human
reviewer's decision.  I never write a scenario or a score from this module.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..guard_hook import mapping_proposal_record, validate

MITRE_ATLAS_FRAMEWORK = "mitre-atlas"
MAPPING_STATUSES = frozenset({"pending", "approved", "rejected"})
MAPPING_DECISIONS = frozenset({"approved", "rejected"})
MAPPING_APPLICABILITY = "hypothesized"
MAPPING_ORIGIN = "manual"
SCENARIO_HASH_FIELDS = (
    "title",
    "description",
    "core_risks",
    "transmission_mechanism",
    "affected_capabilities",
)
_SCENARIO_LIST_FIELDS = frozenset({"core_risks", "affected_capabilities"})
_MAX_RATIONALE_LENGTH = 4_000
_MAX_EVIDENCE_REFS = 50
_MAX_EVIDENCE_REF_LENGTH = 2_048


class MappingError(ValueError):
    """I raise this when a mapping would violate the review contract."""


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )


def _scenario_hash_value(field_name: str, value: object) -> object:
    """I normalize stored JSON without erasing meaning-bearing edits."""
    if value is None:
        return ""
    if field_name not in _SCENARIO_LIST_FIELDS or not isinstance(value, str):
        return value
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return value
    return parsed if isinstance(parsed, list) else value


def scenario_content_hash(scenario: Mapping[str, object]) -> str:
    """I hash only the scenario fields a mapping reviewer actually assessed.

    I decode JSON-backed list columns first, so SQLite's insignificant JSON
    whitespace does not make an otherwise unchanged mapping stale.
    """
    snapshot = {
        field: _scenario_hash_value(field, scenario.get(field))
        for field in SCENARIO_HASH_FIELDS
    }
    return hashlib.sha256(_canonical_json(snapshot).encode("utf-8")).hexdigest()


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {
        str(row["name"])
        for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    }


def _ensure_scenario_revision_tracking(conn: sqlite3.Connection) -> bool:
    """I install a monotonic marker that survives restoration and recreation."""
    if not _table_exists(conn, "scenarios"):
        return False
    if "scenario_revision" not in _column_names(conn, "scenarios"):
        conn.execute(
            "ALTER TABLE scenarios ADD COLUMN "
            "scenario_revision INTEGER NOT NULL DEFAULT 0"
        )
    conn.execute("""
        CREATE TABLE IF NOT EXISTS scenario_mapping_revisions (
            scenario_id TEXT PRIMARY KEY,
            revision INTEGER NOT NULL CHECK (revision >= 1),
            changed_at TEXT NOT NULL
        )
    """)
    # I seed existing rows before installing triggers, so migration itself
    # establishes revision one instead of masquerading as a scenario edit.
    conn.execute("""
        INSERT OR IGNORE INTO scenario_mapping_revisions (
            scenario_id, revision, changed_at
        )
        SELECT scenario_id,
               CASE WHEN scenario_revision >= 1 THEN scenario_revision ELSE 1 END,
               strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        FROM scenarios
        WHERE scenario_id IS NOT NULL AND trim(scenario_id) <> ''
    """)
    conn.execute("""
        UPDATE scenarios
        SET scenario_revision = COALESCE(
            (
                SELECT revision
                FROM scenario_mapping_revisions AS revisions
                WHERE revisions.scenario_id = scenarios.scenario_id
            ),
            0
        )
        WHERE scenario_revision < 1
    """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS scenario_mapping_revision_after_insert
        AFTER INSERT ON scenarios
        WHEN NEW.scenario_id IS NOT NULL AND trim(NEW.scenario_id) <> ''
        BEGIN
            INSERT INTO scenario_mapping_revisions (
                scenario_id, revision, changed_at
            ) VALUES (
                NEW.scenario_id,
                1,
                strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            )
            ON CONFLICT(scenario_id) DO UPDATE SET
                revision = scenario_mapping_revisions.revision + 1,
                changed_at = excluded.changed_at;
            UPDATE scenarios
            SET scenario_revision = (
                SELECT revision FROM scenario_mapping_revisions
                WHERE scenario_id = NEW.scenario_id
            )
            WHERE rowid = NEW.rowid;
        END
    """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS scenario_mapping_revision_after_update
        AFTER UPDATE OF
            title,
            description,
            core_risks,
            transmission_mechanism,
            affected_capabilities
        ON scenarios
        WHEN OLD.title IS NOT NEW.title
          OR OLD.description IS NOT NEW.description
          OR OLD.core_risks IS NOT NEW.core_risks
          OR OLD.transmission_mechanism IS NOT NEW.transmission_mechanism
          OR OLD.affected_capabilities IS NOT NEW.affected_capabilities
        BEGIN
            INSERT INTO scenario_mapping_revisions (
                scenario_id, revision, changed_at
            ) VALUES (
                NEW.scenario_id,
                1,
                strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            )
            ON CONFLICT(scenario_id) DO UPDATE SET
                revision = scenario_mapping_revisions.revision + 1,
                changed_at = excluded.changed_at;
            UPDATE scenarios
            SET scenario_revision = (
                SELECT revision FROM scenario_mapping_revisions
                WHERE scenario_id = NEW.scenario_id
            )
            WHERE rowid = NEW.rowid;
        END
    """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS scenario_mapping_revision_after_delete
        AFTER DELETE ON scenarios
        WHEN OLD.scenario_id IS NOT NULL AND trim(OLD.scenario_id) <> ''
        BEGIN
            INSERT INTO scenario_mapping_revisions (
                scenario_id, revision, changed_at
            ) VALUES (
                OLD.scenario_id,
                1,
                strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            )
            ON CONFLICT(scenario_id) DO UPDATE SET
                revision = scenario_mapping_revisions.revision + 1,
                changed_at = excluded.changed_at;
        END
    """)
    return True


def migrate_mapping_proposals(db_path: str | Path) -> None:
    """I create the additive, idempotent mapping-proposal store.

    I keep the catalog migration separate so an operator can inspect an empty
    review queue before importing ATLAS, while proposal intake still fails
    closed unless its scenario and pinned catalog technique resolve.
    """
    conn = _connect(db_path)
    try:
        revision_tracking = _ensure_scenario_revision_tracking(conn)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS framework_mapping_proposals (
                proposal_id TEXT PRIMARY KEY,
                scenario_id TEXT NOT NULL,
                scenario_hash TEXT NOT NULL,
                scenario_revision INTEGER NOT NULL,
                framework TEXT NOT NULL,
                content_version TEXT NOT NULL,
                technique_id TEXT NOT NULL,
                applicability TEXT NOT NULL,
                rationale TEXT NOT NULL,
                evidence_refs_json TEXT NOT NULL DEFAULT '[]',
                origin TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                reviewer TEXT,
                reason TEXT,
                decided_at TEXT,
                created_at TEXT NOT NULL,
                CHECK (applicability = 'hypothesized'),
                CHECK (origin = 'manual'),
                CHECK (scenario_revision >= 1),
                CHECK (status IN ('pending', 'approved', 'rejected'))
            )
        """)
        if "scenario_revision" not in _column_names(
            conn, "framework_mapping_proposals"
        ):
            conn.execute(
                "ALTER TABLE framework_mapping_proposals ADD COLUMN "
                "scenario_revision INTEGER NOT NULL DEFAULT 0"
            )
        if revision_tracking:
            # I can safely initialize pre-marker proposals only to the current
            # known revision; all subsequent mutations are monotonic.
            conn.execute("""
                UPDATE framework_mapping_proposals
                SET scenario_revision = COALESCE(
                    (
                        SELECT scenario_revision
                        FROM scenarios
                        WHERE scenarios.scenario_id =
                            framework_mapping_proposals.scenario_id
                    ),
                    0
                )
                WHERE scenario_revision < 1
            """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_framework_mapping_review
            ON framework_mapping_proposals(status, created_at DESC)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_framework_mapping_scenario
            ON framework_mapping_proposals(scenario_id, status)
        """)
        # I use this partial uniqueness constraint as the concurrency guard, so
        # two intake requests cannot create duplicate live review work.
        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS
                idx_framework_mapping_pending_identity
            ON framework_mapping_proposals(
                scenario_id, framework, content_version, technique_id
            )
            WHERE status = 'pending'
        """)
        conn.execute(
            "DROP TRIGGER IF EXISTS framework_mapping_proposals_claim_immutable"
        )
        conn.execute("""
            CREATE TRIGGER
                framework_mapping_proposals_claim_immutable
            BEFORE UPDATE OF
                proposal_id,
                scenario_id,
                scenario_hash,
                scenario_revision,
                framework,
                content_version,
                technique_id,
                applicability,
                rationale,
                evidence_refs_json,
                origin,
                created_at
            ON framework_mapping_proposals
            BEGIN
                SELECT RAISE(
                    ABORT,
                    'framework mapping proposal claims are immutable'
                );
            END
        """)
        # I enforce review-state invariants in SQLite as well as Python so a
        # maintenance script cannot bypass the proposal-only boundary.
        conn.execute(
            "DROP TRIGGER IF EXISTS framework_mapping_proposals_pending_insert"
        )
        conn.execute("""
            CREATE TRIGGER
                framework_mapping_proposals_pending_insert
            BEFORE INSERT ON framework_mapping_proposals
            WHEN NEW.scenario_revision IS NULL
              OR NEW.scenario_revision < 1
              OR NEW.status <> 'pending'
              OR NEW.reviewer IS NOT NULL
              OR NEW.reason IS NOT NULL
              OR NEW.decided_at IS NOT NULL
            BEGIN
                SELECT RAISE(
                    ABORT,
                    'framework mapping proposals must enter pending and undecided'
                );
            END
        """)
        conn.execute(
            "DROP TRIGGER IF EXISTS "
            "framework_mapping_proposals_decision_transition"
        )
        conn.execute("""
            CREATE TRIGGER
                framework_mapping_proposals_decision_transition
            BEFORE UPDATE OF status, reviewer, reason, decided_at
            ON framework_mapping_proposals
            WHEN NOT (
                OLD.status = 'pending'
                AND OLD.reviewer IS NULL
                AND OLD.reason IS NULL
                AND OLD.decided_at IS NULL
                AND NEW.status IN ('approved', 'rejected')
                AND NULLIF(trim(COALESCE(NEW.reviewer, '')), '') IS NOT NULL
                AND NULLIF(trim(COALESCE(NEW.reason, '')), '') IS NOT NULL
                AND NULLIF(trim(COALESCE(NEW.decided_at, '')), '') IS NOT NULL
            )
            BEGIN
                SELECT RAISE(
                    ABORT,
                    'framework mapping decisions require one complete pending transition'
                );
            END
        """)
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS framework_mapping_proposals_no_delete
            BEFORE DELETE ON framework_mapping_proposals
            BEGIN
                SELECT RAISE(
                    ABORT,
                    'framework mapping proposal history cannot be deleted'
                );
            END
        """)
        conn.commit()
    finally:
        conn.close()


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        is not None
    )


def _require_table(conn: sqlite3.Connection, table: str, purpose: str) -> None:
    if not _table_exists(conn, table):
        raise MappingError(f"{purpose} is not initialized (missing {table})")


def _normalize_evidence_refs(evidence_refs: Sequence[object] | None) -> list[str]:
    if evidence_refs is None:
        return []
    if isinstance(evidence_refs, (str, bytes)):
        raise MappingError("evidence_refs must be a sequence, not a string")
    normalized: list[str] = []
    for raw in evidence_refs:
        ref = str(raw).strip()
        if not ref:
            continue
        if len(ref) > _MAX_EVIDENCE_REF_LENGTH:
            raise MappingError(
                f"evidence reference exceeds {_MAX_EVIDENCE_REF_LENGTH} characters"
            )
        if ref not in normalized:
            normalized.append(ref)
    if len(normalized) > _MAX_EVIDENCE_REFS:
        raise MappingError(
            f"a mapping may contain at most {_MAX_EVIDENCE_REFS} evidence references"
        )
    return normalized


def _select_release(
    conn: sqlite3.Connection,
    content_version: str | None,
) -> str:
    _require_table(conn, "framework_releases", "MITRE ATLAS catalog")
    if content_version is not None:
        version = str(content_version).strip()
        if not version:
            raise MappingError("content_version must be non-empty when supplied")
        row = conn.execute(
            """
            SELECT content_version
            FROM framework_releases
            WHERE framework = ? AND content_version = ?
            """,
            (MITRE_ATLAS_FRAMEWORK, version),
        ).fetchone()
        if row is None:
            raise MappingError(f"MITRE ATLAS release not found: {version}")
        return version

    rows = conn.execute(
        """
        SELECT content_version
        FROM framework_releases
        WHERE framework = ? AND is_active = 1
        ORDER BY content_version
        """,
        (MITRE_ATLAS_FRAMEWORK,),
    ).fetchall()
    if not rows:
        raise MappingError("MITRE ATLAS has no active catalog release")
    if len(rows) != 1:
        raise MappingError("MITRE ATLAS catalog has more than one active release")
    return str(rows[0]["content_version"])


def _require_technique(
    conn: sqlite3.Connection,
    content_version: str,
    technique_id: str,
) -> sqlite3.Row:
    _require_table(conn, "framework_objects", "MITRE ATLAS catalog")
    row = conn.execute(
        """
        SELECT *
        FROM framework_objects
        WHERE framework = ? AND content_version = ? AND object_id = ?
        """,
        (MITRE_ATLAS_FRAMEWORK, content_version, technique_id),
    ).fetchone()
    if row is None:
        raise MappingError(
            f"MITRE ATLAS technique {technique_id} does not exist in "
            f"release {content_version}"
        )
    if str(row["object_type"]) != "technique":
        raise MappingError(
            f"MITRE ATLAS object {technique_id} is {row['object_type']}, "
            "not a technique"
        )
    return row


def _proposal_from_row(row: sqlite3.Row | Mapping[str, object]) -> dict[str, Any]:
    out = dict(row)
    raw_refs = out.pop("evidence_refs_json", "[]")
    try:
        refs = json.loads(str(raw_refs or "[]"))
    except json.JSONDecodeError:
        refs = []
    out["evidence_refs"] = refs if isinstance(refs, list) else []
    return out


def create_mapping_proposal(
    db_path: str | Path,
    scenario_id: str,
    technique_id: str,
    rationale: str,
    *,
    content_version: str | None = None,
    evidence_refs: Sequence[object] | None = None,
) -> dict[str, Any]:
    """I create one manual, hypothesized mapping pinned to an ATLAS release.

    I return the existing pending proposal when its live mapping identity is
    repeated.  I never let a decided row block a later re-proposal.
    """
    scenario_id = str(scenario_id).strip()
    technique_id = str(technique_id).strip()
    rationale = str(rationale).strip()
    if not scenario_id:
        raise MappingError("scenario_id is required")
    if not technique_id:
        raise MappingError("technique_id is required")
    if not rationale:
        raise MappingError("mapping rationale is required")
    if len(rationale) > _MAX_RATIONALE_LENGTH:
        raise MappingError(
            f"mapping rationale exceeds {_MAX_RATIONALE_LENGTH} characters"
        )
    refs = _normalize_evidence_refs(evidence_refs)

    migrate_mapping_proposals(db_path)
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        _require_table(conn, "scenarios", "scenario database")
        scenario = conn.execute(
            "SELECT * FROM scenarios WHERE scenario_id = ?",
            (scenario_id,),
        ).fetchone()
        if scenario is None:
            raise MappingError(f"Scenario not found: {scenario_id}")
        version = _select_release(conn, content_version)
        _require_technique(conn, version, technique_id)

        existing = conn.execute(
            """
            SELECT *
            FROM framework_mapping_proposals
            WHERE scenario_id = ?
              AND framework = ?
              AND content_version = ?
              AND technique_id = ?
              AND status = 'pending'
            """,
            (scenario_id, MITRE_ATLAS_FRAMEWORK, version, technique_id),
        ).fetchone()
        if existing is not None:
            conn.commit()
            return _proposal_from_row(existing)

        proposal_id = f"map-{uuid.uuid4().hex[:12]}"
        created_at = _now()
        staged = {
            "proposal_id": proposal_id,
            "scenario_id": scenario_id,
            "scenario_hash": scenario_content_hash(dict(scenario)),
            "scenario_revision": int(scenario["scenario_revision"]),
            "framework": MITRE_ATLAS_FRAMEWORK,
            "content_version": version,
            "technique_id": technique_id,
            "applicability": MAPPING_APPLICABILITY,
            "rationale": rationale,
            "evidence_refs": refs,
            "origin": MAPPING_ORIGIN,
            "status": "pending",
            "created_at": created_at,
        }
        gate = validate(
            [mapping_proposal_record(staged)],
            seam="create_mapping_proposal",
        )
        gate.raise_if_rejected("create_mapping_proposal")
        conn.execute(
            """
            INSERT INTO framework_mapping_proposals (
                proposal_id,
                scenario_id,
                scenario_hash,
                scenario_revision,
                framework,
                content_version,
                technique_id,
                applicability,
                rationale,
                evidence_refs_json,
                origin,
                status,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
            """,
            (
                staged["proposal_id"],
                staged["scenario_id"],
                staged["scenario_hash"],
                staged["scenario_revision"],
                staged["framework"],
                staged["content_version"],
                staged["technique_id"],
                staged["applicability"],
                staged["rationale"],
                _canonical_json(refs),
                staged["origin"],
                staged["created_at"],
            ),
        )
        row = conn.execute(
            "SELECT * FROM framework_mapping_proposals WHERE proposal_id = ?",
            (proposal_id,),
        ).fetchone()
        conn.commit()
        if row is None:  # pragma: no cover - SQLite insert invariant
            raise MappingError(f"Could not retrieve new mapping {proposal_id}")
        return _proposal_from_row(row)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_mapping_proposal(
    db_path: str | Path,
    proposal_id: str,
    *,
    include_context: bool = False,
) -> dict[str, Any] | None:
    """I return one proposal, optionally enriched from its pinned catalog."""
    if not Path(db_path).is_file():
        return None
    migrate_mapping_proposals(db_path)
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM framework_mapping_proposals WHERE proposal_id = ?",
            (str(proposal_id),),
        ).fetchone()
        if row is None:
            return None
        proposal = _proposal_from_row(row)
        return _enrich_mapping(conn, proposal) if include_context else proposal
    finally:
        conn.close()


def list_mapping_proposals(
    db_path: str | Path,
    *,
    status: str | None = None,
    scenario_id: str | None = None,
    content_version: str | None = None,
    include_context: bool = False,
) -> list[dict[str, Any]]:
    """I list mapping proposals newest first using exact filters."""
    if status is not None and status not in MAPPING_STATUSES:
        raise MappingError(f"invalid mapping status {status!r}")
    if not Path(db_path).is_file():
        return []
    migrate_mapping_proposals(db_path)
    clauses: list[str] = []
    params: list[str] = []
    if status is not None:
        clauses.append("status = ?")
        params.append(status)
    if scenario_id is not None:
        clauses.append("scenario_id = ?")
        params.append(str(scenario_id))
    if content_version is not None:
        clauses.append("content_version = ?")
        params.append(str(content_version))
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            f"""
            SELECT * FROM framework_mapping_proposals
            {where}
            ORDER BY created_at DESC, proposal_id DESC
            """,
            params,
        ).fetchall()
        proposals = [_proposal_from_row(row) for row in rows]
        if include_context:
            proposals = [_enrich_mapping(conn, row) for row in proposals]
        return proposals
    finally:
        conn.close()


def decide_mapping_proposal(
    db_path: str | Path,
    proposal_id: str,
    *,
    decision: str,
    reviewer: str,
    reason: str,
    decided_at: str | None = None,
) -> dict[str, Any]:
    """I atomically write only decision fields on one pending proposal."""
    if decision not in MAPPING_DECISIONS:
        raise MappingError(f"invalid mapping decision {decision!r}")
    reviewer = str(reviewer).strip()
    reason = str(reason).strip()
    if not reviewer:
        raise MappingError("mapping decisions require a reviewer identity")
    if not reason:
        raise MappingError("mapping decisions require a written reason")
    decided_at = str(decided_at or _now())

    migrate_mapping_proposals(db_path)
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        pending = conn.execute(
            """
            SELECT * FROM framework_mapping_proposals
            WHERE proposal_id = ? AND status = 'pending'
            """,
            (str(proposal_id),),
        ).fetchone()
        if pending is None:
            existing = conn.execute(
                """
                SELECT status FROM framework_mapping_proposals
                WHERE proposal_id = ?
                """,
                (str(proposal_id),),
            ).fetchone()
            if existing is None:
                raise MappingError(f"Mapping proposal not found: {proposal_id}")
            raise MappingError(
                f"Mapping proposal {proposal_id} is not pending "
                f"(status: {existing['status']})"
            )
        if decision == "approved":
            _validate_pending_approval(conn, pending)
        cur = conn.execute(
            """
            UPDATE framework_mapping_proposals
            SET status = ?, reviewer = ?, reason = ?, decided_at = ?
            WHERE proposal_id = ? AND status = 'pending'
            """,
            (decision, reviewer, reason, decided_at, str(proposal_id)),
        )
        if cur.rowcount != 1:
            # I use BEGIN IMMEDIATE to prevent another connection from winning
            # this transition; this branch therefore means a local invariant
            # failed rather than an ordinary review race.
            raise MappingError(
                f"Mapping proposal {proposal_id} could not be decided"
            )
        row = conn.execute(
            "SELECT * FROM framework_mapping_proposals WHERE proposal_id = ?",
            (str(proposal_id),),
        ).fetchone()
        conn.commit()
        if row is None:  # pragma: no cover - guarded UPDATE invariant
            raise MappingError(f"Mapping proposal not found: {proposal_id}")
        return _proposal_from_row(row)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _validate_pending_approval(
    conn: sqlite3.Connection,
    proposal: sqlite3.Row,
) -> None:
    """I revalidate mutable references inside the approval transaction."""
    if str(proposal["framework"]) != MITRE_ATLAS_FRAMEWORK:
        raise MappingError(
            f"unsupported framework on mapping proposal: {proposal['framework']}"
        )
    _require_table(conn, "scenarios", "scenario database")
    scenario = conn.execute(
        "SELECT * FROM scenarios WHERE scenario_id = ?",
        (str(proposal["scenario_id"]),),
    ).fetchone()
    if scenario is None:
        raise MappingError(
            f"Mapped scenario no longer exists: {proposal['scenario_id']}"
        )
    if mapping_state(dict(proposal), dict(scenario)) != "current":
        raise MappingError(
            f"Scenario {proposal['scenario_id']} changed after mapping proposal "
            "creation; create a new proposal for the current scenario"
        )
    version = _select_release(conn, str(proposal["content_version"]))
    _require_technique(conn, version, str(proposal["technique_id"]))


def mapping_state(
    mapping: Mapping[str, object],
    scenario: Mapping[str, object],
) -> str:
    """I return ``current`` when the claim matches today's scenario."""
    try:
        mapped_revision = int(mapping.get("scenario_revision") or 0)
        current_revision = int(scenario.get("scenario_revision") or 0)
    except (TypeError, ValueError):
        return "stale"
    hash_matches = str(mapping.get("scenario_hash") or "") == (
        scenario_content_hash(scenario)
    )
    revision_matches = mapped_revision >= 1 and mapped_revision == current_revision
    return "current" if hash_matches and revision_matches else "stale"


def is_mapping_current(
    mapping: Mapping[str, object],
    scenario: Mapping[str, object],
) -> bool:
    """I expose a Boolean companion for coverage counters."""
    return mapping_state(mapping, scenario) == "current"


def _load_json_list(value: object) -> list[Any]:
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(str(value or "[]"))
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


def _relationship_objects(
    conn: sqlite3.Connection,
    *,
    content_version: str,
    relationship_type: str,
    anchor_id: str,
    anchor_column: str,
    object_column: str,
    expected_type: str,
    id_key: str,
) -> list[dict[str, Any]]:
    """I group related objects while retaining every procedure relationship."""
    if anchor_column not in {"source_id", "target_id"}:
        raise AssertionError("invalid relationship anchor column")
    if object_column not in {"source_id", "target_id"}:
        raise AssertionError("invalid relationship object column")
    rows = conn.execute(
        f"""
        SELECT
            o.object_id,
            o.name,
            o.description,
            o.maturity,
            o.platforms_json,
            r.relationship_key,
            r.metadata_json
        FROM framework_relationships AS r
        JOIN framework_objects AS o
          ON o.framework = r.framework
         AND o.content_version = r.content_version
         AND o.object_id = r.{object_column}
        WHERE r.framework = ?
          AND r.content_version = ?
          AND r.relationship_type = ?
          AND r.{anchor_column} = ?
          AND o.object_type = ?
        ORDER BY o.object_id, r.relationship_key
        """,
        (
            MITRE_ATLAS_FRAMEWORK,
            content_version,
            relationship_type,
            anchor_id,
            expected_type,
        ),
    ).fetchall()
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        object_id = str(row["object_id"])
        if object_id not in grouped:
            item: dict[str, Any] = {
                id_key: object_id,
                "name": row["name"],
                "description": row["description"] or "",
                "relationships": [],
            }
            if row["maturity"] is not None:
                item["maturity"] = row["maturity"]
            platforms = _load_json_list(row["platforms_json"])
            if platforms:
                item["platforms"] = platforms
            grouped[object_id] = item
        try:
            metadata = json.loads(str(row["metadata_json"] or "{}"))
        except json.JSONDecodeError:
            metadata = {}
        relationship = {"relationship_key": row["relationship_key"]}
        if isinstance(metadata, dict):
            relationship.update(metadata)
        grouped[object_id]["relationships"].append(relationship)
    return list(grouped.values())


def _release_provenance(
    conn: sqlite3.Connection,
    content_version: str,
) -> dict[str, Any]:
    """I resolve provenance from the mapping's pinned release, never active state."""
    _require_table(conn, "framework_releases", "MITRE ATLAS catalog")
    release = conn.execute(
        """
        SELECT content_version, format_version, release_date,
               source_url, source_sha256
        FROM framework_releases
        WHERE framework = ? AND content_version = ?
        """,
        (MITRE_ATLAS_FRAMEWORK, content_version),
    ).fetchone()
    if release is None:
        raise MappingError(f"MITRE ATLAS release not found: {content_version}")
    return dict(release)


def resolve_mapping_context(
    db_path: str | Path,
    content_version: str,
    technique_id: str,
) -> dict[str, Any]:
    """I resolve one pinned technique and its display relationships."""
    conn = _connect(db_path)
    try:
        return _resolve_mapping_context(conn, content_version, technique_id)
    finally:
        conn.close()


def _resolve_mapping_context(
    conn: sqlite3.Connection,
    content_version: str,
    technique_id: str,
) -> dict[str, Any]:
    _require_table(conn, "framework_relationships", "MITRE ATLAS catalog")
    technique = _require_technique(conn, content_version, technique_id)
    context = {
        "technique_id": technique["object_id"],
        "name": technique["name"],
        "description": technique["description"] or "",
        "technique_maturity": technique["maturity"],
        "platforms": _load_json_list(technique["platforms_json"]),
        # I preserve the official ATLAS v6 directions here: techniques achieve
        # tactics, while mitigations and case studies point to techniques.
        "tactics": _relationship_objects(
            conn,
            content_version=content_version,
            relationship_type="achieves",
            anchor_id=technique_id,
            anchor_column="source_id",
            object_column="target_id",
            expected_type="tactic",
            id_key="tactic_id",
        ),
        "mitigations": _relationship_objects(
            conn,
            content_version=content_version,
            relationship_type="mitigates",
            anchor_id=technique_id,
            anchor_column="target_id",
            object_column="source_id",
            expected_type="mitigation",
            id_key="mitigation_id",
        ),
        "related_case_studies": _relationship_objects(
            conn,
            content_version=content_version,
            relationship_type="employs",
            anchor_id=technique_id,
            anchor_column="target_id",
            object_column="source_id",
            expected_type="case-study",
            id_key="case_study_id",
        ),
    }
    context.update(_release_provenance(conn, content_version))
    return context


def _enrich_mapping(
    conn: sqlite3.Connection,
    mapping: Mapping[str, object],
) -> dict[str, Any]:
    enriched = dict(mapping)
    enriched.update(
        _resolve_mapping_context(
            conn,
            str(mapping["content_version"]),
            str(mapping["technique_id"]),
        )
    )
    enriched["mapped_release"] = mapping["content_version"]
    return enriched


def approved_mappings_for_scenarios(
    db_path: str | Path,
    scenario_rows: Iterable[Mapping[str, object]],
) -> dict[str, list[dict[str, Any]]]:
    """I return approved, enriched overlays grouped by supplied scenario ID.

    I keep stale approvals visible with ``state='stale'`` for audit and display;
    callers can treat only ``state='current'`` as present-day coverage.  I
    never return pending or rejected proposals from this boundary.
    """
    scenarios: dict[str, Mapping[str, object]] = {}
    for scenario in scenario_rows:
        scenario_id = str(scenario.get("scenario_id") or "").strip()
        if scenario_id:
            scenarios[scenario_id] = scenario
    grouped: dict[str, list[dict[str, Any]]] = {
        scenario_id: [] for scenario_id in scenarios
    }
    if not scenarios or not Path(db_path).is_file():
        return grouped

    migrate_mapping_proposals(db_path)
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT * FROM framework_mapping_proposals
            WHERE framework = ? AND status = 'approved'
            ORDER BY scenario_id, content_version, technique_id, created_at
            """,
            (MITRE_ATLAS_FRAMEWORK,),
        ).fetchall()
        for row in rows:
            proposal = _proposal_from_row(row)
            scenario_id = str(proposal["scenario_id"])
            scenario = scenarios.get(scenario_id)
            if scenario is None:
                continue
            enriched = _enrich_mapping(conn, proposal)
            enriched["state"] = mapping_state(proposal, scenario)
            grouped[scenario_id].append(enriched)
        return grouped
    finally:
        conn.close()
