"""Database operations for atlas.db and event_evidence.db.

All operations are parameterized SQL — no string interpolation.
Both databases use SQLite with WAL mode for concurrent reads.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .guard_hook import scenario_record, validate
from .provenance import url_dedup_variants
from .schema import EVIDENCE_EVENT_COLUMNS, SCENARIO_COLUMNS, SCENARIO_ORIGINS

logger = logging.getLogger(__name__)

SCENARIO_COLUMN_NAMES = frozenset(name for name, _ in SCENARIO_COLUMNS)
EVIDENCE_EVENT_COLUMN_NAMES = frozenset(
    name for name, _ in EVIDENCE_EVENT_COLUMNS
)
SIGNPOST_ASSESSMENT_STATUSES = frozenset(
    {"strengthened", "unchanged", "weakened", "materialized", "superseded"}
)
SIGNPOST_DIRECTIONAL_STATUSES = SIGNPOST_ASSESSMENT_STATUSES - {"unchanged"}


def quote_ident(name: str) -> str:
    """Safely quote a SQL identifier (doubles embedded quotes)."""
    return '"' + str(name).replace('"', '""') + '"'


@contextmanager
def connect(db_path: str) -> Iterator[sqlite3.Connection]:
    """Context manager for SQLite connections with WAL mode."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
    finally:
        conn.close()


# ── Scenario operations (atlas.db) ──


def upsert_scenario(db_path: str, scenario: dict[str, Any]) -> bool:
    """Insert or update a scenario by ``scenario_id``.

    Returns True if a new row was inserted, False if updated.

    Every scenario writer (phase 3, compound graduation, demo seed) passes
    the ontology guard here: a REJECT raises ``GuardRejected`` before any
    write (no-op when ``ATLAS_GUARD=off``).
    """
    scenario = dict(scenario)
    origin_provided = "origin" in scenario
    unknown = set(scenario) - SCENARIO_COLUMN_NAMES
    if unknown:
        raise ValueError(f"Unknown scenario field(s): {sorted(unknown)}")
    sid = scenario["scenario_id"]
    if not sid:
        raise ValueError("scenario_id must be non-empty")
    if origin_provided and scenario["origin"] not in SCENARIO_ORIGINS:
        raise ValueError(
            f"origin must be one of {SCENARIO_ORIGINS}, got {scenario['origin']!r}"
        )
    with connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT origin FROM scenarios WHERE scenario_id = ?", (sid,)
        )
        existing = cursor.fetchone()
        exists = existing is not None

        # Provenance defaults apply only to new records. An ordinary partial
        # upsert must not silently relabel an existing pipeline or compound
        # record as operator-authored merely because the caller omitted origin.
        guarded = dict(scenario)
        if not origin_provided:
            if exists:
                guarded["origin"] = existing["origin"] or "legacy_unknown"
            else:
                scenario["origin"] = "operator_authored"
                guarded["origin"] = "operator_authored"
        validate(
            [scenario_record(guarded)], seam="upsert_scenario"
        ).raise_if_rejected("upsert_scenario")

        if exists:
            # Build UPDATE for all provided columns
            cols = list(scenario.keys())
            if cols == ["scenario_id"]:
                return False
            set_clause = ", ".join(
                f"{quote_ident(c)} = ?" for c in cols if c != "scenario_id"
            )
            vals = [scenario[c] for c in cols if c != "scenario_id"]
            vals.append(sid)
            cursor.execute(
                f"UPDATE scenarios SET {set_clause} WHERE scenario_id = ?", vals
            )
        else:
            cols = list(scenario.keys())
            placeholders = ", ".join("?" for _ in cols)
            col_names = ", ".join(quote_ident(c) for c in cols)
            cursor.execute(
                f"INSERT INTO scenarios ({col_names}) VALUES ({placeholders})",
                list(scenario.values()),
            )
        conn.commit()
    return not exists


def get_scenario(db_path: str, scenario_id: str) -> dict[str, Any] | None:
    """Get a single scenario by ID."""
    with connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM scenarios WHERE scenario_id = ?", (scenario_id,)
        )
        row = cursor.fetchone()
        return dict(row) if row else None


def list_scenarios(
    db_path: str,
    *,
    category: str | None = None,
    status: str | None = "active",
    limit: int | None = None,
    order_by: str = "scenario_id",
) -> list[dict[str, Any]]:
    """List scenarios with optional filtering."""
    query = 'SELECT * FROM scenarios'
    clauses = []
    params = []
    if status:
        # Treat NULL as active (legacy/migrated rows). Exact match for other statuses.
        if status == "active":
            clauses.append('(status = ? OR status IS NULL)')
        else:
            clauses.append("status = ?")
        params.append(status)
    if category:
        clauses.append("risk_category = ?")
        params.append(category)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    # Allowlist ORDER BY to prevent identifier injection if order_by is ever user-supplied
    allowed_order = {
        "scenario_id", "risk_category", "title", "created_year",
        "impact_band", "urgency_score", "judge_score", "status",
        "financial_impact_band", "signal_maturity", "momentum",
        "evidence_strength", "signpost_status",
    }
    if order_by not in allowed_order:
        order_by = "scenario_id"
    query += f" ORDER BY {quote_ident(order_by)}"
    if limit is not None:
        limit = int(limit)
        if limit < 0:
            limit = 0
        query += f" LIMIT {limit}"
    with connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(query, params)
        return [dict(row) for row in cursor.fetchall()]


def update_scenario_fields(
    db_path: str, scenario_id: str, fields: dict[str, Any]
) -> bool:
    """Update specific fields on a scenario. Returns True if row existed."""
    if not fields:
        return True
    unknown = set(fields) - SCENARIO_COLUMN_NAMES
    if unknown:
        raise ValueError(f"Unknown scenario field(s): {sorted(unknown)}")
    if "origin" in fields and fields["origin"] not in SCENARIO_ORIGINS:
        raise ValueError(
            f"origin must be one of {SCENARIO_ORIGINS}, got {fields['origin']!r}"
        )
    set_parts = []
    params = []
    for col, val in fields.items():
        set_parts.append(f'{quote_ident(col)} = ?')
        params.append(val)
    params.append(scenario_id)
    with connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            f'UPDATE scenarios SET {", ".join(set_parts)} '
            "WHERE scenario_id = ?",
            params,
        )
        affected = cursor.rowcount
        conn.commit()
    return affected > 0


def retire_scenario(db_path: str, scenario_id: str, reason: str = "") -> bool:
    """Mark a scenario as retired without wiping existing additional_information.

    No-op (returns False) if the scenario is missing or already retired, so
    repeated maintenance cycles don't stack retirement notes or re-count.
    Any pending signpost interpretation is atomically removed from the active
    review queue while its immutable snapshot remains in history.
    """
    with connect(db_path) as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM scenarios WHERE scenario_id = ?",
                (scenario_id,),
            ).fetchone()
            if (
                existing is None
                or str(existing["status"] or "").lower() == "retired"
            ):
                conn.rollback()
                return False
            note = f"Retired: {reason}" if reason else "Retired"
            if existing["additional_information"]:
                note = f"{existing['additional_information']}\n{note}"
            retired_at = now_iso()
            conn.execute(
                """
                UPDATE signpost_assessments
                SET review_status = 'superseded',
                    reviewed_at = ?,
                    reviewer_note = ?
                WHERE scenario_id = ? AND review_status = 'pending'
                """,
                (
                    retired_at,
                    "Superseded because the scenario was retired at "
                    f"{retired_at}.",
                    scenario_id,
                ),
            )
            updated = conn.execute(
                """
                UPDATE scenarios
                SET status = 'retired',
                    additional_information = ?,
                    signpost_review_status = CASE
                        WHEN signpost_review_status = 'pending'
                        THEN 'superseded'
                        ELSE signpost_review_status
                    END
                WHERE scenario_id = ?
                """,
                (note, scenario_id),
            )
            conn.commit()
            return updated.rowcount == 1
        except Exception:
            conn.rollback()
            raise


# ── Immutable signpost-assessment history ──


def _signpost_ref_list(value: object, field_name: str) -> list[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{field_name} must contain a JSON list") from exc
    if not isinstance(value, (list, tuple, set)):
        raise ValueError(f"{field_name} must be a list")
    normalized: list[str] = []
    for item in value:
        ref = str(item).strip()
        if ref and ref not in normalized:
            normalized.append(ref)
    return normalized


def _projection_matches_assessment(
    scenario: sqlite3.Row,
    assessment: sqlite3.Row,
) -> None:
    scenario_id = str(assessment["scenario_id"])
    if (scenario["signpost_status"] or "") != assessment["classification"]:
        raise ValueError(
            f"Scenario {scenario_id} signpost status no longer matches "
            "the latest assessment"
        )
    if (scenario["signpost_assessment"] or "") != assessment["rationale"]:
        raise ValueError(
            f"Scenario {scenario_id} signpost rationale no longer matches "
            "the latest assessment"
        )
    projected_refs = _signpost_ref_list(
        scenario["signpost_evidence_refs"] or "[]",
        "scenario signpost_evidence_refs",
    )
    assessment_refs = _signpost_ref_list(
        assessment["evidence_refs"],
        "assessment evidence_refs",
    )
    if projected_refs != assessment_refs:
        raise ValueError(
            f"Scenario {scenario_id} evidence references no longer match "
            "the latest assessment"
        )
    checked_refs = set(_signpost_ref_list(
        scenario["signpost_checked_evidence_refs"] or "[]",
        "scenario signpost_checked_evidence_refs",
    ))
    considered_refs = set(_signpost_ref_list(
        assessment["considered_refs"],
        "assessment considered_refs",
    ))
    if not considered_refs.issubset(checked_refs):
        raise ValueError(
            f"Scenario {scenario_id} checked references no longer cover "
            "the latest assessment"
        )
    if (
        scenario["signposts_last_evaluated"] or ""
    ) != assessment["evaluated_at"]:
        raise ValueError(
            f"Scenario {scenario_id} evaluation time no longer matches "
            "the latest assessment"
        )
    if (scenario["signpost_review_status"] or "") != "pending":
        raise ValueError(
            f"Scenario {scenario_id} projection is not pending review"
        )


def record_signpost_assessment(
    db_path: str,
    scenario_id: str,
    *,
    classification: str,
    rationale: str,
    evidence_refs: list[str],
    considered_refs: list[str],
    evaluated_at: str,
) -> dict[str, Any]:
    """Atomically append an immutable assessment and update its projection."""
    scenario_id = str(scenario_id).strip()
    classification = str(classification).strip().lower()
    rationale = str(rationale).strip()
    evaluated_at = str(evaluated_at).strip()
    if not scenario_id:
        raise ValueError("scenario_id must be non-empty")
    if classification not in SIGNPOST_ASSESSMENT_STATUSES:
        raise ValueError(f"Invalid signpost classification: {classification}")
    if not rationale:
        raise ValueError("Signpost assessment rationale must be non-empty")
    if not evaluated_at:
        raise ValueError("Signpost assessment evaluated_at must be non-empty")
    exact_refs = _signpost_ref_list(evidence_refs, "evidence_refs")
    packet_refs = _signpost_ref_list(considered_refs, "considered_refs")
    if not packet_refs:
        raise ValueError("Signpost assessment requires considered evidence")
    if classification in SIGNPOST_DIRECTIONAL_STATUSES and not exact_refs:
        raise ValueError(
            f"{classification} signpost assessment requires exact evidence"
        )
    if not set(exact_refs).issubset(set(packet_refs)):
        raise ValueError(
            "Signpost evidence_refs must be a subset of considered_refs"
        )

    with connect(db_path) as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            scenario = conn.execute(
                "SELECT * FROM scenarios WHERE scenario_id = ?",
                (scenario_id,),
            ).fetchone()
            if scenario is None:
                raise ValueError(f"Scenario not found: {scenario_id}")
            lifecycle = str(scenario["status"] or "active").strip().lower()
            if lifecycle != "active":
                raise ValueError(
                    f"Scenario {scenario_id} is not active"
                )

            checked_refs = _signpost_ref_list(
                scenario["signpost_checked_evidence_refs"] or "[]",
                "signpost_checked_evidence_refs",
            )
            for ref in packet_refs:
                if ref not in checked_refs:
                    checked_refs.append(ref)

            conn.execute(
                """
                UPDATE signpost_assessments
                SET review_status = 'superseded',
                    reviewed_at = NULL,
                    reviewer_note = ?
                WHERE scenario_id = ? AND review_status = 'pending'
                """,
                (
                    "Superseded by a newer model-assisted assessment at "
                    f"{evaluated_at}.",
                    scenario_id,
                ),
            )
            cursor = conn.execute(
                """
                INSERT INTO signpost_assessments (
                    scenario_id,
                    classification,
                    rationale,
                    evidence_refs,
                    considered_refs,
                    evaluated_at,
                    review_status
                )
                VALUES (?, ?, ?, ?, ?, ?, 'pending')
                """,
                (
                    scenario_id,
                    classification,
                    rationale,
                    to_json_str(exact_refs),
                    to_json_str(packet_refs),
                    evaluated_at,
                ),
            )
            assessment_id = int(cursor.lastrowid)
            updated = conn.execute(
                """
                UPDATE scenarios
                SET signpost_status = ?,
                    signpost_assessment = ?,
                    signpost_evidence_refs = ?,
                    signpost_checked_evidence_refs = ?,
                    signposts_last_evaluated = ?,
                    signpost_review_status = 'pending'
                WHERE scenario_id = ?
                """,
                (
                    classification,
                    rationale,
                    to_json_str(exact_refs),
                    to_json_str(checked_refs),
                    evaluated_at,
                    scenario_id,
                ),
            )
            if updated.rowcount != 1:
                raise ValueError(
                    f"Scenario disappeared during assessment: {scenario_id}"
                )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM signpost_assessments WHERE id = ?",
                (assessment_id,),
            ).fetchone()
            return dict(row) if row else {}
        except Exception:
            conn.rollback()
            raise


def get_latest_signpost_assessment(
    db_path: str,
    scenario_id: str,
) -> dict[str, Any] | None:
    """Return the newest immutable assessment snapshot for a scenario."""
    with connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT *
            FROM signpost_assessments
            WHERE scenario_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (scenario_id,),
        ).fetchone()
        return dict(row) if row else None


def list_signpost_assessments(
    db_path: str,
    *,
    review_status: str | None = "pending",
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """List assessment history with lightweight scenario context."""
    query = """
        SELECT
            a.*,
            s.title AS scenario_title,
            s.status AS scenario_status,
            s.signpost_review_status AS projection_review_status
        FROM signpost_assessments AS a
        LEFT JOIN scenarios AS s ON s.scenario_id = a.scenario_id
    """
    params: list[Any] = []
    clauses: list[str] = []
    if review_status is not None:
        clauses.append("a.review_status = ?")
        params.append(review_status)
    if review_status == "pending":
        clauses.append("s.scenario_id IS NOT NULL")
        clauses.append(
            "lower(trim(COALESCE(NULLIF(s.status, ''), 'active'))) = 'active'"
        )
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY a.id DESC"
    if limit is not None:
        query += " LIMIT ?"
        params.append(max(0, int(limit)))
    with connect(db_path) as conn:
        rows = []
        for row in conn.execute(query, params).fetchall():
            item = dict(row)
            # `id` is the signpost_assessments row. Callers that copy the
            # first identifier into approve-signpost need scenario_id.
            # assessment_id names the row without removing id.
            if "id" in item:
                item["assessment_id"] = item["id"]
            rows.append(item)
        return rows


def decide_signpost_assessment(
    db_path: str,
    scenario_id: str,
    *,
    assessment_id: int,
    decision: str,
    reviewed_at: str,
    reviewer_note: str,
    audit_message: str,
    evidence_db_path: str | None = None,
) -> dict[str, Any]:
    """Atomically decide the latest pending snapshot and its projection."""
    decision = str(decision).strip().lower()
    if decision not in {"approved", "rejected"}:
        raise ValueError(f"Invalid signpost review decision: {decision}")
    reviewed_at = str(reviewed_at).strip()
    if not reviewed_at:
        raise ValueError("Signpost review requires reviewed_at")
    if decision == "approved":
        if not evidence_db_path or not Path(evidence_db_path).is_file():
            raise ValueError(
                "Signpost approval requires an initialized evidence database"
            )

    with connect(db_path) as conn:
        try:
            if decision == "approved":
                conn.execute(
                    "ATTACH DATABASE ? AS signpost_evidence_store",
                    (str(evidence_db_path),),
                )
            conn.execute("BEGIN IMMEDIATE")
            assessment = conn.execute(
                """
                SELECT *
                FROM signpost_assessments
                WHERE scenario_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (scenario_id,),
            ).fetchone()
            if assessment is None:
                raise ValueError(
                    f"Scenario {scenario_id} has no signpost assessment history"
                )
            if int(assessment["id"]) != int(assessment_id):
                raise ValueError(
                    f"Scenario {scenario_id} has a newer signpost assessment"
                )
            if assessment["review_status"] != "pending":
                raise ValueError(
                    f"Scenario {scenario_id} has no pending signpost assessment"
                )
            if decision == "approved":
                exact_refs = _signpost_ref_list(
                    assessment["evidence_refs"],
                    "assessment evidence_refs",
                )
                considered_refs = _signpost_ref_list(
                    assessment["considered_refs"],
                    "assessment considered_refs",
                )
                classification = str(assessment["classification"] or "")
                if (
                    classification in SIGNPOST_DIRECTIONAL_STATUSES
                    and not exact_refs
                ):
                    raise ValueError(
                        f"{classification} signpost assessment requires "
                        "exact evidence refs"
                    )
                if not considered_refs:
                    raise ValueError(
                        "Signpost assessment requires considered evidence refs"
                    )
                if not set(exact_refs).issubset(set(considered_refs)):
                    raise ValueError(
                        "Signpost evidence_refs must be a subset of "
                        "considered_refs"
                    )
                approved_rows = conn.execute(
                    """
                    SELECT event_id, source_url
                    FROM signpost_evidence_store.evidence_events
                    WHERE review_status IN ('approved', 'approved_synthetic')
                    """
                ).fetchall()
                approved_refs = {
                    str(value).strip()
                    for row in approved_rows
                    for value in (row["event_id"], row["source_url"])
                    if str(value or "").strip()
                }
                unresolved = [
                    ref for ref in considered_refs
                    if ref not in approved_refs
                ]
                if unresolved:
                    raise ValueError(
                        "Signpost assessment references must still resolve "
                        "to approved evidence by event ID or exact source URL: "
                        + ", ".join(unresolved)
                    )
            scenario = conn.execute(
                "SELECT * FROM scenarios WHERE scenario_id = ?",
                (scenario_id,),
            ).fetchone()
            if scenario is None:
                raise ValueError(f"Scenario not found: {scenario_id}")
            lifecycle = str(scenario["status"] or "active").strip().lower()
            if lifecycle != "active":
                raise ValueError(
                    f"Scenario {scenario_id} is not active and cannot remain "
                    "in the signpost review queue"
                )
            _projection_matches_assessment(scenario, assessment)

            history_update = conn.execute(
                """
                UPDATE signpost_assessments
                SET review_status = ?,
                    reviewed_at = ?,
                    reviewer_note = ?
                WHERE id = ? AND review_status = 'pending'
                """,
                (
                    decision,
                    reviewed_at,
                    str(reviewer_note).strip(),
                    assessment_id,
                ),
            )
            if history_update.rowcount != 1:
                raise ValueError(
                    f"Scenario {scenario_id} assessment changed during review"
                )
            existing_note = str(
                scenario["additional_information"] or ""
            ).strip()
            combined_note = (
                f"{existing_note}\n{audit_message}"
                if existing_note
                else audit_message
            )
            projection_update = conn.execute(
                """
                UPDATE scenarios
                SET signpost_review_status = ?,
                    additional_information = ?
                WHERE scenario_id = ?
                """,
                (decision, combined_note, scenario_id),
            )
            if projection_update.rowcount != 1:
                raise ValueError(
                    f"Scenario disappeared during review: {scenario_id}"
                )
            conn.commit()
            updated = conn.execute(
                "SELECT * FROM scenarios WHERE scenario_id = ?",
                (scenario_id,),
            ).fetchone()
            return dict(updated) if updated else {}
        except Exception:
            conn.rollback()
            raise


# ── Evidence-event operations (event_evidence.db) ──


def add_evidence_event(db_path: str, event: dict[str, Any]) -> bool:
    """Add an evidence event.

    Returns True if new, or False when ``event_id`` or ``source_url`` already
    exists. All writes use the neutral public-source schema.
    """
    unknown = set(event) - EVIDENCE_EVENT_COLUMN_NAMES
    if unknown:
        raise ValueError(f"Unknown evidence-event field(s): {sorted(unknown)}")
    if not event.get("event_id"):
        raise ValueError("event_id must be non-empty")
    with connect(db_path) as conn:
        cursor = conn.cursor()
        event_id = event.get("event_id", "")
        source_url = event.get("source_url", "")
        if event_id:
            cursor.execute(
                "SELECT COUNT(*) FROM evidence_events WHERE event_id = ?",
                (event_id,),
            )
            if cursor.fetchone()[0] > 0:
                return False
        if source_url:
            # Ingest canonicalizes source URLs, but rows stored before that
            # keep their raw form. Match both so re-fetching a document whose
            # URL contains a space dedups against its own older row instead of
            # landing a second copy under a new fingerprint-derived id.
            variants = url_dedup_variants(source_url)
            placeholders = ", ".join("?" for _ in variants)
            cursor.execute(
                "SELECT COUNT(*) FROM evidence_events "
                f"WHERE source_url IN ({placeholders})",
                variants,
            )
            if cursor.fetchone()[0] > 0:
                return False
        # Insert
        cols = list(event.keys())
        placeholders = ", ".join("?" for _ in cols)
        col_names = ", ".join(quote_ident(c) for c in cols)
        cursor.execute(
            f"INSERT INTO evidence_events ({col_names}) VALUES ({placeholders})",
            list(event.values()),
        )
        conn.commit()
    return True


def get_evidence_event(db_path: str, event_id: str) -> dict[str, Any] | None:
    """Return one evidence event by ``event_id``, or ``None`` when absent."""
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM evidence_events WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        return dict(row) if row else None


def update_evidence_event_fields(
    db_path: str,
    event_id: str,
    fields: dict[str, Any],
) -> bool:
    """Update allowlisted fields on one evidence event.

    ``event_id`` is an immutable identifier. The return value reports whether
    the target record existed.
    """
    if not fields:
        return get_evidence_event(db_path, event_id) is not None
    unknown = set(fields) - EVIDENCE_EVENT_COLUMN_NAMES
    if unknown:
        raise ValueError(f"Unknown evidence-event field(s): {sorted(unknown)}")
    if "event_id" in fields:
        raise ValueError("event_id is immutable")

    assignments = ", ".join(f"{quote_ident(name)} = ?" for name in fields)
    values = [fields[name] for name in fields]
    values.append(event_id)
    with connect(db_path) as conn:
        cursor = conn.execute(
            f"UPDATE evidence_events SET {assignments} WHERE event_id = ?",
            values,
        )
        conn.commit()
        return cursor.rowcount > 0


def list_evidence_events(
    db_path: str,
    *,
    category: str | None = None,
    organization: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int | None = None,
    approved_only: bool = False,
) -> list[dict[str, Any]]:
    """Query evidence events with optional filters."""
    clauses = []
    params = []
    if category:
        clauses.append("risk_category = ?")
        params.append(category)
    if organization:
        clauses.append("organization = ?")
        params.append(organization)
    if date_from:
        clauses.append("source_date >= ?")
        params.append(date_from)
    if date_to:
        clauses.append("source_date <= ?")
        params.append(date_to)
    if approved_only:
        clauses.append("review_status IN (?, ?)")
        params.extend(("approved", "approved_synthetic"))
    query = "SELECT * FROM evidence_events"
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY source_date DESC"
    if limit is not None:
        limit = max(0, int(limit))
        query += f" LIMIT {limit}"
    with connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(query, params)
        return [dict(row) for row in cursor.fetchall()]


def get_evidence_event_stats(db_path: str) -> dict[str, Any]:
    """Aggregate statistics for the evidence-event database."""
    with connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM evidence_events")
        total = cursor.fetchone()[0]
        cursor.execute(
            "SELECT risk_category, COUNT(*) FROM evidence_events "
            "GROUP BY risk_category ORDER BY COUNT(*) DESC"
        )
        categories = {row[0] or "Unknown": row[1] for row in cursor.fetchall()}
        cursor.execute(
            "SELECT organization, COUNT(*) FROM evidence_events "
            "GROUP BY organization ORDER BY COUNT(*) DESC LIMIT 10"
        )
        organizations = {row[0] or "Unknown": row[1] for row in cursor.fetchall()}
        cursor.execute(
            "SELECT MIN(source_date), MAX(source_date) FROM evidence_events"
        )
        date_range = cursor.fetchone()
        cursor.execute(
            "SELECT AVG(loss_amount_usd), SUM(loss_amount_usd) "
            "FROM evidence_events "
            "WHERE loss_amount_usd IS NOT NULL"
        )
        loss_stats = cursor.fetchone()
    return {
        "total": total,
        "categories": categories,
        "top_organizations": organizations,
        "date_range": [date_range[0], date_range[1]],
        "avg_loss": loss_stats[0],
        "total_loss": loss_stats[1],
    }


# ── QA log operations ──


def log_qa(
    db_path: str,
    scenario_id: str,
    phase: str,
    judge_model: str,
    judge_score: float,
    judge_feedback: str,
    iterations: int,
    passed: bool,
) -> int:
    """Log a QA evaluation. Returns the log ID."""
    with connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO qa_logs
            (scenario_id, phase, judge_model, judge_score, judge_feedback,
             iterations, passed)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (scenario_id, phase, judge_model, judge_score,
             judge_feedback, iterations, passed),
        )
        log_id = cursor.lastrowid
        conn.commit()
    return log_id


def list_qa_scenario_ids(db_path: str) -> list[str]:
    """Distinct scenario IDs that have ever been judged (including rejects)."""
    with connect(db_path) as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT DISTINCT scenario_id FROM qa_logs")
        except sqlite3.OperationalError:
            return []
        return [row[0] for row in cursor.fetchall() if row[0]]


# ── Pipeline run tracking ──


def log_pipeline_run(
    db_path: str,
    run_id: str,
    phase: str,
    status: str,
    started_at: str | None = None,
    completed_at: str | None = None,
    items_processed: int = 0,
    errors: str | None = None,
    driftlab_cycle_id: str | None = None,
    driftlab_score: int | None = None,
) -> int:
    """Log a pipeline run. Upserts on run_id (UNIQUE). Returns the row id."""
    with connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO pipeline_runs
            (run_id, phase, status, started_at, completed_at,
             items_processed, errors, driftlab_cycle_id, driftlab_score)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
              phase=excluded.phase,
              status=excluded.status,
              started_at=excluded.started_at,
              completed_at=excluded.completed_at,
              items_processed=excluded.items_processed,
              errors=excluded.errors,
              driftlab_cycle_id=excluded.driftlab_cycle_id,
              driftlab_score=excluded.driftlab_score
            """,
            (run_id, phase, status, started_at, completed_at,
             items_processed, errors, driftlab_cycle_id, driftlab_score),
        )
        log_id = cursor.lastrowid
        conn.commit()
    return log_id


# ── JSON helpers ──


def to_json_str(obj: Any) -> str:
    """Serialize to JSON string (for TEXT columns)."""
    return json.dumps(obj, ensure_ascii=False, default=str)


def from_json_str(s: str | None) -> Any:
    """Deserialize from JSON string."""
    if not s:
        return None
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return None


def now_iso() -> str:
    """Current UTC timestamp in ISO format."""
    return datetime.now(timezone.utc).isoformat()
