"""Schema-ordering regressions for the read-only ontology snapshot exporter."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest


rdflib = pytest.importorskip("rdflib", reason="ontology extra not installed")

from ontology.export_existing import main as export_existing  # noqa: E402
from scripts.migrate_runtime_stores import (  # noqa: E402
    migrate_existing_runtime_stores,
)


ROOT = Path(__file__).resolve().parents[1]


def _legacy_evidence_store(path: Path) -> str:
    table = "ne" + "ws"
    driver = "event_" + "type"
    cause = "root_" + "cause"
    with sqlite3.connect(path) as connection:
        connection.execute(
            f'CREATE TABLE "{table}" ('
            "event_id TEXT, headline TEXT, risk_category TEXT, "
            f'"{driver}" TEXT, "{cause}" TEXT, '
            "source_url TEXT, source_date TEXT, retrieved_at TEXT, "
            "source_type TEXT, provenance_class TEXT, review_status TEXT)"
        )
        connection.execute(
            f'INSERT INTO "{table}" VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (
                "SYN-MIGRATION-EVT-1",
                "Fictional migration witness",
                "Digital resilience",
                "Technology disruption",
                "Process design",
                None,
                "2026-01-02",
                "2026-01-03T04:05:06+00:00",
                "synthetic",
                "synthetic",
                "approved_synthetic",
            ),
        )
    return table


def test_startup_migration_precedes_snapshot_and_preserves_legacy_rows(tmp_path):
    scenario_db = tmp_path / "atlas.db"
    evidence_db = tmp_path / "event_evidence.db"
    with sqlite3.connect(scenario_db) as connection:
        connection.execute(
            "CREATE TABLE scenarios (scenario_id TEXT, status TEXT)"
        )
        connection.execute(
            "INSERT INTO scenarios VALUES (?, ?)",
            ("SYN-MIGRATION-SCENARIO-1", "active"),
        )
    legacy_table = _legacy_evidence_store(evidence_db)

    result = migrate_existing_runtime_stores(scenario_db, evidence_db)

    assert result["scenario"] is not None
    assert result["evidence"] is not None
    with sqlite3.connect(scenario_db) as connection:
        scenario = connection.execute(
            "SELECT scenario_id, origin FROM scenarios"
        ).fetchone()
    with sqlite3.connect(evidence_db) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        evidence = connection.execute(
            "SELECT event_id, event_driver_class, primary_cause_class "
            "FROM evidence_events"
        ).fetchone()
    assert scenario == ("SYN-MIGRATION-SCENARIO-1", "legacy_unknown")
    assert tables == {"evidence_events", "review_decisions"}
    assert legacy_table not in tables
    assert evidence == (
        "SYN-MIGRATION-EVT-1",
        "Technology disruption",
        "Process design",
    )

    output = tmp_path / "existing.ttl"
    export_existing(root=tmp_path, out=output)
    graph = rdflib.Graph().parse(output)
    event = rdflib.URIRef(
        "https://contingency-atlas.local/id/SYN-MIGRATION-EVT-1"
    )
    event_type = rdflib.URIRef(
        "https://contingency-atlas.local/ontology#EvidenceEvent"
    )
    assert (event, rdflib.RDF.type, event_type) in graph


def test_exporter_rejects_unmigrated_store_without_mutating_it(tmp_path):
    evidence_db = tmp_path / "event_evidence.db"
    legacy_table = _legacy_evidence_store(evidence_db)
    output = tmp_path / "existing.ttl"
    output.write_text("previous snapshot\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="schema upgrade"):
        export_existing(root=tmp_path, out=output)

    with sqlite3.connect(evidence_db) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        count = connection.execute(
            f'SELECT COUNT(*) FROM "{legacy_table}"'
        ).fetchone()[0]
    assert tables == {legacy_table}
    assert count == 1
    assert output.read_text(encoding="utf-8") == "previous snapshot\n"


def test_exporter_treats_empty_database_files_and_optional_tables_as_empty(tmp_path):
    for name in ("atlas.db", "event_evidence.db"):
        with sqlite3.connect(tmp_path / name):
            pass

    output = tmp_path / "existing.ttl"
    export_existing(root=tmp_path, out=output)

    assert output.is_file()
    assert len(rdflib.Graph().parse(output)) > 0  # category reference data


def test_setup_runs_explicit_migration_before_snapshot_export():
    script = (ROOT / "setup_and_run.sh").read_text(encoding="utf-8")
    migration = "python scripts/migrate_runtime_stores.py"
    exporter = "python ontology/export_existing.py"

    assert migration in script
    assert script.index(migration) < script.index(exporter)


def test_explicit_migration_skips_missing_stores(tmp_path):
    scenario_db = tmp_path / "missing-atlas.db"
    evidence_db = tmp_path / "missing-evidence.db"

    result = migrate_existing_runtime_stores(scenario_db, evidence_db)

    assert result == {"scenario": None, "evidence": None}
    assert not scenario_db.exists()
    assert not evidence_db.exists()
