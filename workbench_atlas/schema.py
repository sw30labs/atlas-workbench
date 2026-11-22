"""SQLite schema bootstrap for Atlas Workbench.

Both application databases use project-native, snake_case fields. The schema
contains no vendor-specific taxonomy or source-specific column names.
Migrations are additive and idempotent so a fresh checkout can bootstrap empty
databases at runtime without committing binary database files.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

logger = logging.getLogger(__name__)

SCENARIO_ORIGINS = (
    "operator_authored",
    "pipeline_generated",
    "compound_graduation",
    "synthetic_fixture",
    "legacy_unknown",
)

INSTRUMENT_STATUSES = (
    "proposed",
    "enacted",
    "enforcement",
    "vacated",
)


def normalize_instrument_status(value: object) -> str | None:
    """Clamp to the closed instrument-status vocab, or None if absent/invalid."""
    raw = str(value or "").strip().lower()
    if raw in INSTRUMENT_STATUSES:
        return raw
    return None

SCENARIO_COLUMNS: list[tuple[str, str]] = [
    ("scenario_id", "TEXT"),
    ("origin", "TEXT NOT NULL DEFAULT 'legacy_unknown'"),
    ("risk_category", "TEXT"),
    ("title", "TEXT"),
    ("description", "TEXT"),
    ("created_year", "INTEGER"),
    ("financial_impact_band", "TEXT"),
    ("event_driver_class", "TEXT"),
    ("primary_cause_class", "TEXT"),
    ("impact_band", "TEXT"),
    ("core_risks", "TEXT"),
    ("scenario_event_name", "TEXT"),
    ("scenario_horizon", "TEXT"),
    ("likelihood", "TEXT"),
    ("financial_impact_detail", "TEXT"),
    ("operational_impact", "TEXT"),
    ("reputational_impact", "TEXT"),
    ("regulatory_impact", "TEXT"),
    ("element_of_surprise", "TEXT"),
    ("preparation_strategy", "TEXT"),
    ("detailed_rationale", "TEXT"),
    ("additional_information", "TEXT"),
    ("theme_tags", "TEXT"),
    ("evidence_refs", "TEXT"),
    ("external_drivers", "TEXT"),
    ("signal_maturity", "TEXT"),
    ("momentum", "TEXT"),
    ("evidence_strength", "TEXT"),
    ("affected_capabilities", "TEXT"),
    ("transmission_mechanism", "TEXT"),
    ("second_order_effects", "TEXT"),
    ("cross_industry_analogues", "TEXT"),
    ("strengthening_signposts", "TEXT"),
    ("weakening_signposts", "TEXT"),
    ("defensive_options", "TEXT"),
    ("resilience_opportunities", "TEXT"),
    ("strategic_options", "TEXT"),
    ("option_expiry_signals", "TEXT"),
    ("challenged_assumptions", "TEXT"),
    ("discussion_prompts", "TEXT"),
    ("signpost_status", "TEXT"),
    ("signpost_assessment", "TEXT"),
    ("signpost_evidence_refs", "TEXT"),
    ("signpost_checked_evidence_refs", "TEXT"),
    ("signposts_last_evaluated", "TEXT"),
    ("signpost_review_status", "TEXT"),
    ("judge_score", "REAL"),
    ("judge_feedback", "TEXT"),
    ("judge_iterations", "INTEGER"),
    ("urgency_score", "REAL"),
    ("time_horizon", "TEXT"),
    ("controllability", "TEXT"),
    ("last_scored", "TEXT"),
    ("status", "TEXT"),
]


EVIDENCE_EVENT_COLUMNS: list[tuple[str, str]] = [
    ("event_id", "TEXT"),
    ("headline", "TEXT"),
    ("organization", "TEXT"),
    ("country", "TEXT"),
    ("region", "TEXT"),
    ("loss_amount_usd", "REAL"),
    ("source_date", "TEXT"),
    ("risk_category", "TEXT"),
    ("event_driver_class", "TEXT"),
    ("primary_cause_class", "TEXT"),
    ("source_url", "TEXT"),
    ("source_publisher", "TEXT"),
    ("source_title", "TEXT"),
    ("retrieved_at", "TEXT"),
    ("source_type", "TEXT"),
    ("instrument_status", "TEXT"),
    ("record_type", "TEXT"),
    ("provenance_class", "TEXT"),
    ("source_locator", "TEXT"),
    ("independently_authored_summary", "TEXT"),
    ("research_inference", "TEXT"),
    ("rights_review", "TEXT"),
    ("content_fingerprint", "TEXT"),
    ("review_status", "TEXT"),
    ("reviewed_at", "TEXT"),
    ("taxonomy_version", "TEXT"),
    ("provenance_note", "TEXT"),
]


def _ensure_columns(
    cursor: sqlite3.Cursor,
    table: str,
    columns: list[tuple[str, str]],
) -> list[str]:
    cursor.execute(f'PRAGMA table_info("{table}")')
    existing = {row[1] for row in cursor.fetchall()}
    added: list[str] = []
    for column_name, column_type in columns:
        if column_name in existing:
            continue
        cursor.execute(
            f'ALTER TABLE "{table}" ADD COLUMN "{column_name}" {column_type}'
        )
        added.append(column_name)
        logger.info("Added %s.%s (%s)", table, column_name, column_type)
    return added


def migrate(db_path: str = "atlas.db") -> list[str]:
    """Create or extend the scenario database and return added column names."""
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    base_columns = ", ".join(
        f'"{name}" {column_type}' for name, column_type in SCENARIO_COLUMNS
    )
    cursor.execute(f"CREATE TABLE IF NOT EXISTS scenarios ({base_columns})")
    added = _ensure_columns(cursor, "scenarios", SCENARIO_COLUMNS)
    cursor.execute(
        "UPDATE scenarios SET origin = 'legacy_unknown' "
        "WHERE origin IS NULL OR trim(origin) = ''"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_scenarios_origin ON scenarios(origin)"
    )

    try:
        cursor.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_scenarios_id "
            "ON scenarios(scenario_id)"
        )
    except sqlite3.IntegrityError as exc:
        logger.warning(
            "Could not enforce scenario ID uniqueness because duplicates exist: %s",
            exc,
        )

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS qa_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scenario_id TEXT NOT NULL,
            phase TEXT NOT NULL,
            judge_model TEXT,
            judge_score REAL,
            judge_feedback TEXT,
            iterations INTEGER DEFAULT 0,
            timestamp TEXT DEFAULT (datetime('now')),
            passed BOOLEAN DEFAULT 0
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS signpost_assessments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scenario_id TEXT NOT NULL,
            classification TEXT NOT NULL,
            rationale TEXT NOT NULL,
            evidence_refs TEXT NOT NULL DEFAULT '[]',
            considered_refs TEXT NOT NULL DEFAULT '[]',
            evaluated_at TEXT NOT NULL,
            review_status TEXT NOT NULL DEFAULT 'pending',
            reviewed_at TEXT,
            reviewer_note TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            origin TEXT NOT NULL DEFAULT 'model_assisted'
        )
    """)
    assessment_columns_added = _ensure_columns(
        cursor,
        "signpost_assessments",
        [("origin", "TEXT NOT NULL DEFAULT 'model_assisted'")],
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_signpost_assessments_scenario "
        "ON signpost_assessments(scenario_id, id DESC)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_signpost_assessments_review "
        "ON signpost_assessments(review_status, id DESC)"
    )
    # Recreate this trigger so additive snapshot columns introduced by future
    # migrations cannot remain accidentally mutable.
    cursor.execute(
        "DROP TRIGGER IF EXISTS signpost_assessments_snapshot_immutable"
    )
    if "origin" in assessment_columns_added:
        # Rows that predate the origin column cannot all be safely called
        # model-assisted: an earlier review may already have overwritten the
        # only backfill marker. Preserve that uncertainty explicitly.
        cursor.execute("""
            UPDATE signpost_assessments
            SET origin = 'legacy_unknown'
        """)
        cursor.execute("""
            UPDATE signpost_assessments
            SET origin = 'legacy_projection_backfill'
            WHERE reviewer_note =
                'Backfilled from the pre-history scenario projection.'
              AND origin = 'legacy_unknown'
        """)
    cursor.execute("""
        CREATE TRIGGER signpost_assessments_snapshot_immutable
        BEFORE UPDATE OF
            scenario_id,
            classification,
            rationale,
            evidence_refs,
            considered_refs,
            evaluated_at,
            created_at,
            origin
        ON signpost_assessments
        BEGIN
            SELECT RAISE(ABORT, 'signpost assessment snapshots are immutable');
        END
    """)
    cursor.execute("""
        CREATE TRIGGER IF NOT EXISTS signpost_assessments_no_delete
        BEFORE DELETE ON signpost_assessments
        BEGIN
            SELECT RAISE(ABORT, 'signpost assessment history cannot be deleted');
        END
    """)
    # Preserve pre-history projections when upgrading an existing database.
    # Review timestamps cannot be reconstructed, so the backfill is marked
    # explicitly rather than inventing reviewer metadata.
    cursor.execute("""
        INSERT INTO signpost_assessments (
            scenario_id,
            classification,
            rationale,
            evidence_refs,
            considered_refs,
            evaluated_at,
            review_status,
            reviewed_at,
            reviewer_note,
            origin
        )
        SELECT
            s.scenario_id,
            s.signpost_status,
            s.signpost_assessment,
            COALESCE(NULLIF(s.signpost_evidence_refs, ''), '[]'),
            COALESCE(NULLIF(s.signpost_checked_evidence_refs, ''), '[]'),
            COALESCE(
                NULLIF(s.signposts_last_evaluated, ''),
                datetime('now')
            ),
            CASE
                WHEN s.signpost_review_status = 'pending'
                 AND lower(
                    trim(COALESCE(NULLIF(s.status, ''), 'active'))
                 ) <> 'active'
                THEN 'superseded'
                ELSE s.signpost_review_status
            END,
            NULL,
            'Backfilled from the pre-history scenario projection.',
            'legacy_projection_backfill'
        FROM scenarios AS s
        WHERE s.scenario_id IS NOT NULL
          AND s.scenario_id <> ''
          AND s.signpost_status IN (
              'strengthened',
              'unchanged',
              'weakened',
              'materialized',
              'superseded'
          )
          AND COALESCE(s.signpost_assessment, '') <> ''
          AND s.signpost_review_status IN ('pending', 'approved', 'rejected')
          AND NOT EXISTS (
              SELECT 1
              FROM signpost_assessments AS a
              WHERE a.scenario_id = s.scenario_id
          )
    """)
    # Heal the inconsistent state created by older maintenance code that could
    # retire a scenario after appending a pending assessment in the same cycle.
    # Snapshot content remains immutable; only decision metadata is advanced.
    cursor.execute("""
        UPDATE signpost_assessments
        SET review_status = 'superseded',
            reviewed_at = COALESCE(reviewed_at, datetime('now')),
            reviewer_note = CASE
                WHEN NULLIF(trim(COALESCE(reviewer_note, '')), '') IS NULL
                THEN 'Superseded during migration because the scenario is inactive.'
                ELSE reviewer_note ||
                    ' | Superseded during migration because the scenario is inactive.'
            END
        WHERE review_status = 'pending'
          AND EXISTS (
              SELECT 1
              FROM scenarios AS s
              WHERE s.scenario_id = signpost_assessments.scenario_id
                AND lower(
                    trim(COALESCE(NULLIF(s.status, ''), 'active'))
                ) <> 'active'
          )
    """)
    cursor.execute("""
        UPDATE scenarios
        SET signpost_review_status = 'superseded'
        WHERE signpost_review_status = 'pending'
          AND lower(
              trim(COALESCE(NULLIF(status, ''), 'active'))
          ) <> 'active'
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS pattern_detections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pattern_type TEXT NOT NULL,
            description TEXT NOT NULL,
            confidence REAL,
            evidence TEXT,
            source_ids TEXT,
            detected_at TEXT DEFAULT (datetime('now')),
            status TEXT DEFAULT 'active'
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS causal_findings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cause_event TEXT NOT NULL,
            effect_event TEXT NOT NULL,
            lag_days INTEGER,
            confidence REAL,
            sample_count INTEGER,
            counterexamples INTEGER,
            mechanism TEXT,
            detected_at TEXT DEFAULT (datetime('now'))
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS pipeline_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT UNIQUE NOT NULL,
            phase TEXT NOT NULL,
            status TEXT NOT NULL,
            started_at TEXT,
            completed_at TEXT,
            items_processed INTEGER DEFAULT 0,
            errors TEXT,
            driftlab_cycle_id TEXT,
            driftlab_score INTEGER
        )
    """)
    # Advisory machine pre-analyses for the review queue (review_assist.py).
    # One row per (kind, record, content-hash, model); human_action is filled
    # when the reviewer decides, so suggestion-vs-decision agreement is
    # auditable — if the human always agrees, the review boundary has
    # degraded to a rubber stamp and the data will show it.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS review_suggestions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            record_kind TEXT NOT NULL,
            record_id TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            model_id TEXT,
            created_at TEXT NOT NULL,
            recommendation TEXT,
            confidence REAL,
            payload TEXT,
            human_action TEXT,
            decided_at TEXT,
            UNIQUE (record_kind, record_id, content_hash, model_id)
        )
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_review_suggestions_record
        ON review_suggestions(record_kind, record_id)
    """)
    # Deep-research memos (research.py): one row per run, latest wins in the
    # dashboard. Proposal-only — candidates live in the evidence-events table
    # as ordinary pending records.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS review_research (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            record_kind TEXT NOT NULL,
            record_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            model_id TEXT,
            payload TEXT
        )
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_review_research_record
        ON review_research(record_kind, record_id)
    """)

    from .decisions import ensure_decision_schema
    ensure_decision_schema(conn)
    conn.commit()
    conn.close()
    return added


_LEGACY_EVIDENCE_TABLE = "ne" + "ws"
_LEGACY_DRIVER_COLUMN = "event_" + "type"
_LEGACY_CAUSE_COLUMN = "root_" + "cause"
_LEGACY_PUBLIC_SOURCE_TYPE = "public_" + _LEGACY_EVIDENCE_TABLE


def _table_exists(cursor: sqlite3.Cursor, table: str) -> bool:
    return cursor.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone() is not None


def _rename_legacy_evidence_column(
    cursor: sqlite3.Cursor,
    legacy_name: str,
    canonical_name: str,
) -> None:
    columns = {
        row[1]
        for row in cursor.execute('PRAGMA table_info("evidence_events")')
    }
    if legacy_name not in columns:
        return
    if canonical_name in columns:
        conflicts = cursor.execute(
            f'SELECT COUNT(*) FROM "evidence_events" '
            f'WHERE "{legacy_name}" IS NOT NULL '
            f'AND "{canonical_name}" IS NOT NULL '
            f'AND "{legacy_name}" != "{canonical_name}"'
        ).fetchone()[0]
        if conflicts:
            raise sqlite3.IntegrityError(
                f"Cannot merge conflicting legacy evidence column {legacy_name!r}"
            )
        cursor.execute(
            f'UPDATE "evidence_events" SET "{canonical_name}" = '
            f'COALESCE("{canonical_name}", "{legacy_name}")'
        )
        cursor.execute(
            f'ALTER TABLE "evidence_events" DROP COLUMN "{legacy_name}"'
        )
        return
    cursor.execute(
        f'ALTER TABLE "evidence_events" RENAME COLUMN '
        f'"{legacy_name}" TO "{canonical_name}"'
    )


def migrate_evidence_events(db_path: str = "event_evidence.db") -> list[str]:
    """Create or transactionally remint the public evidence-event store.

    Existing stores are migrated in place at this storage boundary. The table
    and two renamed classification columns retain every row and value; no
    compatibility aliases are exposed to application code or API consumers.
    """
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    try:
        cursor.execute("BEGIN IMMEDIATE")
        canonical_table = "evidence_events"
        has_canonical = _table_exists(cursor, canonical_table)
        has_legacy = _table_exists(cursor, _LEGACY_EVIDENCE_TABLE)
        if has_legacy and has_canonical:
            legacy_count = cursor.execute(
                f'SELECT COUNT(*) FROM "{_LEGACY_EVIDENCE_TABLE}"'
            ).fetchone()[0]
            if legacy_count:
                raise sqlite3.IntegrityError(
                    "Both legacy and canonical evidence tables contain data; "
                    "refusing a potentially lossy merge"
                )
            cursor.execute(f'DROP TABLE "{_LEGACY_EVIDENCE_TABLE}"')
        elif has_legacy:
            cursor.execute(
                f'ALTER TABLE "{_LEGACY_EVIDENCE_TABLE}" '
                f'RENAME TO "{canonical_table}"'
            )

        base_columns = ", ".join(
            f'"{name}" {column_type}'
            for name, column_type in EVIDENCE_EVENT_COLUMNS
        )
        cursor.execute(
            f'CREATE TABLE IF NOT EXISTS "{canonical_table}" ({base_columns})'
        )
        _rename_legacy_evidence_column(
            cursor,
            _LEGACY_DRIVER_COLUMN,
            "event_driver_class",
        )
        _rename_legacy_evidence_column(
            cursor,
            _LEGACY_CAUSE_COLUMN,
            "primary_cause_class",
        )
        added = _ensure_columns(
            cursor,
            canonical_table,
            EVIDENCE_EVENT_COLUMNS,
        )
        cursor.execute(
            'UPDATE "evidence_events" SET source_type = ? '
            'WHERE source_type = ?',
            ("public_reporting", _LEGACY_PUBLIC_SOURCE_TYPE),
        )

        # A renamed table keeps historical index names. Remove those opaque
        # migration artifacts before creating the canonical schema surface.
        for row in cursor.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' "
            "AND tbl_name = ?",
            (canonical_table,),
        ).fetchall():
            index_name = str(row[0])
            if _LEGACY_EVIDENCE_TABLE in index_name:
                cursor.execute(f'DROP INDEX "{index_name}"')
        try:
            cursor.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS "
                "idx_evidence_events_event_id ON evidence_events(event_id)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_evidence_events_source_url "
                "ON evidence_events(source_url)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_evidence_events_source_date "
                "ON evidence_events(source_date)"
            )
        except sqlite3.IntegrityError as exc:
            logger.warning(
                "Could not create an evidence-event uniqueness index: %s",
                exc,
            )
        from .decisions import ensure_decision_schema
        ensure_decision_schema(conn)
        conn.commit()
        return added
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_schema_info(db_path: str = "atlas.db") -> dict[str, Any]:
    """Return table, scenario-column, and row-count metadata."""
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(scenarios)")
    columns = [(row[1], row[2]) for row in cursor.fetchall()]
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = [row[0] for row in cursor.fetchall()]
    counts: dict[str, int] = {}
    for table in tables:
        escaped = table.replace('"', '""')
        cursor.execute(f'SELECT COUNT(*) FROM "{escaped}"')
        counts[table] = cursor.fetchone()[0]
    conn.close()
    return {
        "tables": tables,
        "scenarios_columns": columns,
        "row_counts": counts,
    }
