"""Seed a local workspace from the conspicuously fictional demo corpus.

This is the engine behind ``atlas demo`` and ``scripts/seed_demo.py``.  It
writes only to an explicit output directory, never to the source checkout, and
it never touches a governed operator register unless that directory *is* the
register location (which it refuses by default).

The fixture corpus ships twice on purpose:

* ``examples/`` in a source checkout (the reviewed, human-readable copy), and
* ``workbench_atlas/fixtures/`` inside the wheel (so ``atlas demo`` works
  after ``pip install`` without a checkout).

``tests/test_demo_release.py`` keeps the two copies identical.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

DEMO_FILES = (
    "demo_evidence.json",
    "demo_emerging_risks.json",
    "demo_scenarios.json",
)

# Scenario columns stored as JSON text in SQLite.
JSON_TEXT_FIELDS = {
    "affected_capabilities",
    "challenged_assumptions",
    "core_risks",
    "cross_industry_analogues",
    "defensive_options",
    "detailed_rationale",
    "discussion_prompts",
    "evidence_refs",
    "external_drivers",
    "option_expiry_signals",
    "preparation_strategy",
    "resilience_opportunities",
    "second_order_effects",
    "signpost_checked_evidence_refs",
    "signpost_evidence_refs",
    "strategic_options",
    "strengthening_signposts",
    "theme_tags",
    "weakening_signposts",
}


class DemoError(RuntimeError):
    """Raised when the demo corpus cannot be found or written."""


def fixtures_dir() -> Path:
    """Locate the demo fixtures: env override, checkout, then packaged copy."""
    candidates: list[Path] = []
    if override := os.environ.get("ATLAS_FIXTURES"):
        candidates.append(Path(override).expanduser())
    package = Path(__file__).resolve().parent
    candidates.append(package.parent / "examples")
    candidates.append(package / "fixtures")
    for candidate in candidates:
        if all((candidate / name).is_file() for name in DEMO_FILES):
            return candidate
    searched = ", ".join(str(path) for path in candidates)
    raise DemoError(f"Demo fixtures not found. Looked in: {searched}")


def _load_fixture(directory: Path, name: str) -> list[dict[str, Any]]:
    path = directory / name
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DemoError(f"Cannot read demo fixture {path}: {exc}") from exc
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise DemoError(f"{path} must contain a JSON array of objects")
    return value


def _integrity_check(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        result = connection.execute("PRAGMA integrity_check").fetchone()
    if not result or result[0] != "ok":
        raise DemoError(f"SQLite integrity check failed for {path}: {result}")


def is_seeded(output_dir: Path) -> bool:
    """True when a previous ``atlas demo`` left both stores behind."""
    output_dir = Path(output_dir).expanduser()
    return (output_dir / "atlas.db").is_file() and (
        output_dir / "event_evidence.db"
    ).is_file()


def seed_into(
    output_dir: Path | str,
    *,
    reset: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    """Write the fictional demo corpus into ``output_dir``.

    Args:
        output_dir: destination directory; created when missing.
        reset: delete the two generated demo databases first.
        force: allow writing into the source checkout root.  Almost never
            wanted: it can overwrite an operator-managed register.

    Returns:
        A JSON-serializable summary of what was written.
    """
    from workbench_atlas.db import add_evidence_event, to_json_str, upsert_scenario
    from workbench_atlas.schema import migrate, migrate_evidence_events

    target = Path(output_dir).expanduser().resolve()
    checkout = Path(__file__).resolve().parent.parent
    if target == checkout and not force:
        raise DemoError(
            "Refusing to seed into the source checkout root: the demo corpus "
            "would overwrite an operator-managed register. Use the default "
            "data directory, or pass force=True if you really mean it."
        )

    target.mkdir(parents=True, exist_ok=True)
    scenario_db = target / "atlas.db"
    evidence_db = target / "event_evidence.db"
    emerging_output = target / "emerging_risks.json"
    scenario_export = target / "scenario_export.json"

    if reset:
        for path in (scenario_db, evidence_db):
            path.unlink(missing_ok=True)
            path.with_name(path.name + "-wal").unlink(missing_ok=True)
            path.with_name(path.name + "-shm").unlink(missing_ok=True)

    source = fixtures_dir()
    evidence = _load_fixture(source, "demo_evidence.json")
    emerging = _load_fixture(source, "demo_emerging_risks.json")
    scenarios = _load_fixture(source, "demo_scenarios.json")

    migrate(str(scenario_db))
    migrate_evidence_events(str(evidence_db))

    inserted_evidence = sum(
        add_evidence_event(str(evidence_db), event) for event in evidence
    )
    inserted_scenarios = 0
    updated_scenarios = 0
    for fixture in scenarios:
        row = {
            key: to_json_str(value) if key in JSON_TEXT_FIELDS else value
            for key, value in fixture.items()
        }
        if upsert_scenario(str(scenario_db), row):
            inserted_scenarios += 1
        else:
            updated_scenarios += 1

    emerging_output.write_text(
        json.dumps(emerging, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    scenario_export.write_text(
        json.dumps(scenarios, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    _integrity_check(scenario_db)
    _integrity_check(evidence_db)

    return {
        "output_dir": str(target),
        "fixtures": str(source),
        "scenario_database": str(scenario_db),
        "evidence_database": str(evidence_db),
        "emerging_risks": str(emerging_output),
        "scenario_export": str(scenario_export),
        "scenario_count": len(scenarios),
        "scenario_inserted": inserted_scenarios,
        "scenario_updated": updated_scenarios,
        "evidence_count": len(evidence),
        "evidence_inserted": inserted_evidence,
        "synthetic_only": True,
    }
