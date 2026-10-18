"""I store and read versioned external frameworks in SQLite.

I store upstream catalogs as immutable, release-scoped snapshots.  Activation
changes only which snapshot is selected by default; it never rewrites a prior
release or a scenario.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import Any

MITRE_ATLAS = "mitre-atlas"
MAX_SEARCH_LIMIT = 100
MAX_SEARCH_QUERY_LENGTH = 256
TECHNIQUE_MATURITIES = frozenset({"Feasible", "Demonstrated", "Realized"})
TECHNIQUE_PLATFORMS = frozenset(
    {"Predictive AI", "Generative AI", "Agentic AI", "Enterprise"}
)


def _connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def migrate_frameworks(db_path: str | Path) -> None:
    """I create the additive, idempotent external-framework catalog schema."""

    conn = _connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS framework_releases (
                framework TEXT NOT NULL,
                content_version TEXT NOT NULL,
                format_version TEXT NOT NULL,
                release_date TEXT,
                source_url TEXT NOT NULL,
                source_sha256 TEXT NOT NULL,
                imported_at TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 0
                    CHECK (is_active IN (0, 1)),
                is_sealed INTEGER NOT NULL DEFAULT 0
                    CHECK (is_sealed IN (0, 1)),
                PRIMARY KEY (framework, content_version)
            );

            CREATE UNIQUE INDEX IF NOT EXISTS idx_framework_releases_active
                ON framework_releases(framework)
                WHERE is_active = 1;

            CREATE TABLE IF NOT EXISTS framework_objects (
                framework TEXT NOT NULL,
                content_version TEXT NOT NULL,
                object_id TEXT NOT NULL,
                object_type TEXT NOT NULL
                    CHECK (object_type IN (
                        'tactic', 'technique', 'mitigation', 'case-study'
                    )),
                name TEXT NOT NULL,
                description TEXT,
                maturity TEXT,
                platforms_json TEXT NOT NULL DEFAULT '[]',
                object_hash TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                PRIMARY KEY (framework, content_version, object_id),
                FOREIGN KEY (framework, content_version)
                    REFERENCES framework_releases(framework, content_version)
            );

            CREATE INDEX IF NOT EXISTS idx_framework_objects_search
                ON framework_objects(
                    framework, content_version, object_type, name, object_id
                );

            CREATE TABLE IF NOT EXISTS framework_relationships (
                framework TEXT NOT NULL,
                content_version TEXT NOT NULL,
                relationship_key TEXT NOT NULL,
                source_id TEXT NOT NULL,
                relationship_type TEXT NOT NULL
                    CHECK (relationship_type IN (
                        'sequences', 'achieves', 'specializes',
                        'mitigates', 'employs'
                    )),
                target_id TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                PRIMARY KEY (framework, content_version, relationship_key),
                FOREIGN KEY (framework, content_version)
                    REFERENCES framework_releases(framework, content_version)
            );

            CREATE INDEX IF NOT EXISTS idx_framework_relationships_source
                ON framework_relationships(
                    framework, content_version, source_id, relationship_type
                );

            CREATE INDEX IF NOT EXISTS idx_framework_relationships_target
                ON framework_relationships(
                    framework, content_version, target_id, relationship_type
                );

            CREATE TRIGGER IF NOT EXISTS framework_objects_no_update
            BEFORE UPDATE ON framework_objects
            BEGIN
                SELECT RAISE(ABORT, 'framework objects are immutable');
            END;

            CREATE TRIGGER IF NOT EXISTS framework_objects_no_delete
            BEFORE DELETE ON framework_objects
            BEGIN
                SELECT RAISE(ABORT, 'framework objects are immutable');
            END;

            CREATE TRIGGER IF NOT EXISTS framework_relationships_no_update
            BEFORE UPDATE ON framework_relationships
            BEGIN
                SELECT RAISE(ABORT, 'framework relationships are immutable');
            END;

            CREATE TRIGGER IF NOT EXISTS framework_relationships_no_delete
            BEFORE DELETE ON framework_relationships
            BEGIN
                SELECT RAISE(ABORT, 'framework relationships are immutable');
            END;

            CREATE TRIGGER IF NOT EXISTS framework_release_provenance_immutable
            BEFORE UPDATE OF
                framework,
                content_version,
                format_version,
                release_date,
                source_url,
                source_sha256,
                imported_at
            ON framework_releases
            BEGIN
                SELECT RAISE(ABORT, 'framework release provenance is immutable');
            END;

            CREATE TRIGGER IF NOT EXISTS framework_releases_no_delete
            BEFORE DELETE ON framework_releases
            BEGIN
                SELECT RAISE(ABORT, 'framework releases cannot be deleted');
            END;
            """
        )
        columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(framework_releases)")
        }
        if "is_sealed" not in columns:
            # I seal releases imported before this additive migration so each
            # recorded source hash continues to identify a fixed snapshot.
            conn.execute(
                """
                ALTER TABLE framework_releases
                ADD COLUMN is_sealed INTEGER NOT NULL DEFAULT 1
                    CHECK (is_sealed IN (0, 1))
                """
            )
        conn.executescript(
            """
            CREATE TRIGGER IF NOT EXISTS framework_objects_sealed_release
            BEFORE INSERT ON framework_objects
            WHEN COALESCE((
                SELECT is_sealed FROM framework_releases
                WHERE framework = NEW.framework
                  AND content_version = NEW.content_version
            ), 1) = 1
            BEGIN
                SELECT RAISE(ABORT, 'framework release is sealed');
            END;

            CREATE TRIGGER IF NOT EXISTS framework_relationships_sealed_release
            BEFORE INSERT ON framework_relationships
            WHEN COALESCE((
                SELECT is_sealed FROM framework_releases
                WHERE framework = NEW.framework
                  AND content_version = NEW.content_version
            ), 1) = 1
            BEGIN
                SELECT RAISE(ABORT, 'framework release is sealed');
            END;

            CREATE TRIGGER IF NOT EXISTS framework_release_seal_immutable
            BEFORE UPDATE OF is_sealed ON framework_releases
            WHEN OLD.is_sealed = 1 AND NEW.is_sealed != 1
            BEGIN
                SELECT RAISE(ABORT, 'framework release seal is immutable');
            END;

            CREATE TRIGGER IF NOT EXISTS framework_release_insert_active_sealed
            BEFORE INSERT ON framework_releases
            WHEN NEW.is_active = 1 AND NEW.is_sealed != 1
            BEGIN
                SELECT RAISE(ABORT, 'active framework release must be sealed');
            END;

            CREATE TRIGGER IF NOT EXISTS framework_release_update_active_sealed
            BEFORE UPDATE OF is_active, is_sealed ON framework_releases
            WHEN NEW.is_active = 1 AND NEW.is_sealed != 1
            BEGIN
                SELECT RAISE(ABORT, 'active framework release must be sealed');
            END;
            """
        )
        conn.commit()
    finally:
        conn.close()


def _activate_in_connection(
    conn: sqlite3.Connection,
    content_version: str,
    framework: str,
) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT * FROM framework_releases
        WHERE framework = ? AND content_version = ?
        """,
        (framework, content_version),
    ).fetchone()
    if row is None:
        raise KeyError(
            f"Unknown {framework} content version: {content_version}"
        )
    if not bool(row["is_sealed"]):
        raise ValueError(
            f"Cannot activate unsealed {framework} release: {content_version}"
        )
    conn.execute(
        "UPDATE framework_releases SET is_active = 0 WHERE framework = ?",
        (framework,),
    )
    conn.execute(
        """
        UPDATE framework_releases SET is_active = 1
        WHERE framework = ? AND content_version = ?
        """,
        (framework, content_version),
    )
    result = dict(row)
    result["is_active"] = True
    return result


def activate_release(
    db_path: str | Path,
    content_version: str,
    framework: str = MITRE_ATLAS,
) -> dict[str, Any]:
    """I activate an imported release while retaining every older snapshot."""

    framework = str(framework).strip()
    content_version = str(content_version).strip()
    if not framework or not content_version:
        raise ValueError("framework and content_version must be non-empty")
    migrate_frameworks(db_path)
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        result = _activate_in_connection(conn, content_version, framework)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _release_counts(
    conn: sqlite3.Connection,
    framework: str,
    content_version: str,
) -> dict[str, int]:
    counts = {
        "tactics": 0,
        "techniques": 0,
        "mitigations": 0,
        "case_studies": 0,
        "relationships": 0,
    }
    plural = {
        "tactic": "tactics",
        "technique": "techniques",
        "mitigation": "mitigations",
        "case-study": "case_studies",
    }
    for row in conn.execute(
        """
        SELECT object_type, COUNT(*) AS n
        FROM framework_objects
        WHERE framework = ? AND content_version = ?
        GROUP BY object_type
        """,
        (framework, content_version),
    ):
        counts[plural[str(row["object_type"])]] = int(row["n"])
    relationship_row = conn.execute(
        """
        SELECT COUNT(*) AS n FROM framework_relationships
        WHERE framework = ? AND content_version = ?
        """,
        (framework, content_version),
    ).fetchone()
    counts["relationships"] = int(relationship_row["n"])
    return counts


def framework_status(
    db_path: str | Path,
    framework: str = MITRE_ATLAS,
) -> dict[str, Any]:
    """I return installed releases and an easy-to-consume active summary."""

    migrate_frameworks(db_path)
    conn = _connect(db_path)
    try:
        releases: list[dict[str, Any]] = []
        for row in conn.execute(
            """
            SELECT * FROM framework_releases
            WHERE framework = ?
            ORDER BY imported_at DESC, content_version DESC
            """,
            (framework,),
        ):
            release = dict(row)
            release["is_active"] = bool(release["is_active"])
            release["is_sealed"] = bool(release["is_sealed"])
            release["counts"] = _release_counts(
                conn, framework, str(release["content_version"])
            )
            releases.append(release)

        active = next((row for row in releases if row["is_active"]), None)
        status: dict[str, Any] = {
            "framework": framework,
            "installed": bool(releases),
            "available": active is not None,
            "active_release": active["content_version"] if active else None,
            "active": active,
            "releases": releases,
        }
        if active:
            status.update(
                {
                    "content_version": active["content_version"],
                    "format_version": active["format_version"],
                    "source_sha256": active["source_sha256"],
                    "counts": active["counts"],
                }
            )
        else:
            status["counts"] = {
                "tactics": 0,
                "techniques": 0,
                "mitigations": 0,
                "case_studies": 0,
                "relationships": 0,
            }
        return status
    finally:
        conn.close()


def _selected_version(
    conn: sqlite3.Connection,
    framework: str,
    content_version: str | None,
) -> str | None:
    if content_version is not None:
        version = str(content_version).strip()
        if not version:
            raise ValueError("content_version must be non-empty when supplied")
        row = conn.execute(
            """
            SELECT 1 FROM framework_releases
            WHERE framework = ? AND content_version = ?
            """,
            (framework, version),
        ).fetchone()
        return version if row else None
    row = conn.execute(
        """
        SELECT content_version FROM framework_releases
        WHERE framework = ? AND is_active = 1
        """,
        (framework,),
    ).fetchone()
    return str(row[0]) if row else None


def _decode_json(raw: str, fallback: Any) -> Any:
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return fallback


def _object_dict(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["platforms"] = _decode_json(item.pop("platforms_json"), [])
    item["payload"] = _decode_json(item.pop("payload_json"), {})
    if item["object_type"] == "technique":
        item["technique_id"] = item["object_id"]
    return item


def get_object(
    db_path: str | Path,
    object_id: str,
    *,
    framework: str = MITRE_ATLAS,
    content_version: str | None = None,
) -> dict[str, Any] | None:
    """I get one object from an explicit release or the active release."""

    migrate_frameworks(db_path)
    conn = _connect(db_path)
    try:
        version = _selected_version(conn, framework, content_version)
        if version is None:
            return None
        row = conn.execute(
            """
            SELECT * FROM framework_objects
            WHERE framework = ? AND content_version = ? AND object_id = ?
            """,
            (framework, version, str(object_id)),
        ).fetchone()
        return _object_dict(row) if row else None
    finally:
        conn.close()


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def search_techniques(
    db_path: str | Path,
    query: str = "",
    platform: str | None = None,
    maturity: str | None = None,
    limit: int = 20,
    *,
    framework: str = MITRE_ATLAS,
    content_version: str | None = None,
) -> list[dict[str, Any]]:
    """I search one release with bounded, parameterized filters."""

    query = str(query).strip()
    if len(query) > MAX_SEARCH_QUERY_LENGTH:
        raise ValueError(
            f"query exceeds {MAX_SEARCH_QUERY_LENGTH} characters"
        )
    if platform is not None and platform not in TECHNIQUE_PLATFORMS:
        raise ValueError(f"Unknown MITRE ATLAS platform: {platform}")
    if maturity is not None and maturity not in TECHNIQUE_MATURITIES:
        raise ValueError(f"Unknown MITRE ATLAS maturity: {maturity}")
    try:
        bounded_limit = int(limit)
    except (TypeError, ValueError) as exc:
        raise ValueError("limit must be an integer") from exc
    if bounded_limit < 1:
        return []
    bounded_limit = min(bounded_limit, MAX_SEARCH_LIMIT)

    migrate_frameworks(db_path)
    conn = _connect(db_path)
    try:
        version = _selected_version(conn, framework, content_version)
        if version is None:
            return []
        clauses = [
            "framework = ?",
            "content_version = ?",
            "object_type = 'technique'",
        ]
        params: list[Any] = [framework, version]
        if query:
            like = f"%{_escape_like(query)}%"
            clauses.append(
                "(object_id LIKE ? ESCAPE '\\' COLLATE NOCASE "
                "OR name LIKE ? ESCAPE '\\' COLLATE NOCASE "
                "OR description LIKE ? ESCAPE '\\' COLLATE NOCASE)"
            )
            params.extend([like, like, like])
        if maturity is not None:
            clauses.append("maturity = ?")
            params.append(maturity)
        if platform is not None:
            clauses.append(
                "EXISTS (SELECT 1 FROM json_each(platforms_json) "
                "WHERE json_each.value = ?)"
            )
            params.append(platform)
        params.append(bounded_limit)
        rows = conn.execute(
            "SELECT * FROM framework_objects WHERE "
            + " AND ".join(clauses)
            + " ORDER BY name COLLATE NOCASE, object_id LIMIT ?",
            params,
        ).fetchall()
        return [_object_dict(row) for row in rows]
    finally:
        conn.close()


def _related_objects(
    conn: sqlite3.Connection,
    *,
    framework: str,
    content_version: str,
    technique_id: str,
    relationship_type: str,
    technique_is_source: bool,
) -> list[dict[str, Any]]:
    join_id = "r.target_id" if technique_is_source else "r.source_id"
    technique_column = "r.source_id" if technique_is_source else "r.target_id"
    rows = conn.execute(
        f"""
        SELECT o.*, r.relationship_key, r.metadata_json
        FROM framework_relationships AS r
        JOIN framework_objects AS o
          ON o.framework = r.framework
         AND o.content_version = r.content_version
         AND o.object_id = {join_id}
        WHERE r.framework = ?
          AND r.content_version = ?
          AND r.relationship_type = ?
          AND {technique_column} = ?
        ORDER BY o.name COLLATE NOCASE, o.object_id, r.relationship_key
        """,
        (framework, content_version, relationship_type, technique_id),
    ).fetchall()

    # I preserve every procedure relationship when a case study employs the
    # same technique several times, while returning the object only once.
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        object_id = str(row["object_id"])
        if object_id not in grouped:
            grouped[object_id] = _object_dict(row)
            grouped[object_id].pop("relationship_key", None)
            grouped[object_id].pop("metadata_json", None)
            grouped[object_id]["relationships"] = []
        grouped[object_id]["relationships"].append(
            {
                "relationship_key": row["relationship_key"],
                **_decode_json(row["metadata_json"], {}),
            }
        )
    return list(grouped.values())


def _objects_for_ids(
    conn: sqlite3.Connection,
    *,
    framework: str,
    content_version: str,
    object_ids: Iterable[str],
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for object_id in object_ids:
        row = conn.execute(
            """
            SELECT * FROM framework_objects
            WHERE framework = ? AND content_version = ? AND object_id = ?
            """,
            (framework, content_version, object_id),
        ).fetchone()
        if row:
            items.append(_object_dict(row))
    return items


def technique_context(
    db_path: str | Path,
    version: str,
    technique_id: str,
    framework: str = MITRE_ATLAS,
) -> dict[str, Any] | None:
    """I resolve a technique plus its tactics, defenses, and case studies."""

    migrate_frameworks(db_path)
    conn = _connect(db_path)
    try:
        row = conn.execute(
            """
            SELECT * FROM framework_objects
            WHERE framework = ? AND content_version = ?
              AND object_id = ? AND object_type = 'technique'
            """,
            (framework, str(version), str(technique_id)),
        ).fetchone()
        if row is None:
            return None
        result = _object_dict(row)
        result["tactics"] = _related_objects(
            conn,
            framework=framework,
            content_version=str(version),
            technique_id=str(technique_id),
            relationship_type="achieves",
            technique_is_source=True,
        )
        result["mitigations"] = _related_objects(
            conn,
            framework=framework,
            content_version=str(version),
            technique_id=str(technique_id),
            relationship_type="mitigates",
            technique_is_source=False,
        )
        case_studies = _related_objects(
            conn,
            framework=framework,
            content_version=str(version),
            technique_id=str(technique_id),
            relationship_type="employs",
            technique_is_source=False,
        )
        result["case_studies"] = case_studies
        result["related_case_studies"] = case_studies

        parent_rows = conn.execute(
            """
            SELECT target_id FROM framework_relationships
            WHERE framework = ? AND content_version = ?
              AND source_id = ? AND relationship_type = 'specializes'
            ORDER BY target_id
            """,
            (framework, str(version), str(technique_id)),
        ).fetchall()
        parents = _objects_for_ids(
            conn,
            framework=framework,
            content_version=str(version),
            object_ids=[str(item["target_id"]) for item in parent_rows],
        )
        result["parent_technique"] = parents[0] if parents else None

        child_rows = conn.execute(
            """
            SELECT source_id FROM framework_relationships
            WHERE framework = ? AND content_version = ?
              AND target_id = ? AND relationship_type = 'specializes'
            ORDER BY source_id
            """,
            (framework, str(version), str(technique_id)),
        ).fetchall()
        result["subtechniques"] = _objects_for_ids(
            conn,
            framework=framework,
            content_version=str(version),
            object_ids=[str(item["source_id"]) for item in child_rows],
        )
        return result
    finally:
        conn.close()


def resolve_technique(
    db_path: str | Path,
    content_version: str,
    technique_id: str,
    framework: str = MITRE_ATLAS,
) -> dict[str, Any] | None:
    """I provide a compatibility alias for technique resolution."""

    return technique_context(db_path, content_version, technique_id, framework)
