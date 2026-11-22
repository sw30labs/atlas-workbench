#!/usr/bin/env python3
"""Explicitly upgrade existing local SQLite stores to canonical schemas.

The migrations are transactional and idempotent. Missing database files are
left absent so this command never seeds or creates an operator store.

Usage: python scripts/migrate_runtime_stores.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from workbench_atlas.config import get_settings  # noqa: E402
from workbench_atlas.schema import migrate, migrate_evidence_events  # noqa: E402


def migrate_existing_runtime_stores(
    scenario_db: Path,
    evidence_db: Path,
) -> dict[str, list[str] | None]:
    """Migrate existing stores and return added columns; skip missing files."""
    result: dict[str, list[str] | None] = {
        "scenario": None,
        "evidence": None,
    }
    if scenario_db.is_file():
        result["scenario"] = migrate(str(scenario_db))
    if evidence_db.is_file():
        result["evidence"] = migrate_evidence_events(str(evidence_db))
    return result


def main() -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scenario-db",
        type=Path,
        default=settings.scenario_db_path,
        help="scenario SQLite path (default: configured ATLAS_DB)",
    )
    parser.add_argument(
        "--evidence-db",
        type=Path,
        default=settings.evidence_db_path,
        help="evidence SQLite path (default: configured ATLAS_EVIDENCE_DB)",
    )
    args = parser.parse_args()

    result = migrate_existing_runtime_stores(
        args.scenario_db.expanduser().resolve(),
        args.evidence_db.expanduser().resolve(),
    )
    for label, path in (
        ("scenario", args.scenario_db),
        ("evidence", args.evidence_db),
    ):
        added = result[label]
        if added is None:
            print(f"{label}: skipped missing store {path}")
        else:
            print(f"{label}: migrated {path} ({len(added)} columns added)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
