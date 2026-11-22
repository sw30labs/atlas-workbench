#!/usr/bin/env python3
"""Preview or apply deterministic cleanup to a local scenario catalog.

The default is read-only. With ``--apply``, the command normalizes categories
and recomputes Phase 4 prioritization scores in the configured local database.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    import argparse

    from workbench_atlas.config import get_settings
    from workbench_atlas.schema import migrate
    from workbench_atlas.phases.phase4_prioritize import (
        run_prioritization_cycle,
        get_prioritized_portfolio,
    )
    from workbench_atlas.phases.category_cleanup import apply_category_cleanup

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually write category changes and scores to the DB "
        "(default: dry-run preview only)",
    )
    args = parser.parse_args()

    s = get_settings()
    db = str(s.scenario_db_path)
    print(f"Using scenario database: {db}")
    print(f"Absolute scenario database path: {s.scenario_db_path}")
    print(f"DriftLab baseline: {s.driftlab_baseline_path} exists={s.driftlab_baseline_path.is_file()}")
    print(f"auto_retire={s.auto_retire}")

    migrate(db)

    print("\n=== Category cleanup ===")
    dry = apply_category_cleanup(db, dry_run=True)
    print(f"Proposed changes: {len(dry.changed)} (unchanged {dry.unchanged})")
    for ch in dry.changed[:15]:
        print(f"  {ch.scenario_id[:40]:40s} {ch.old_category:15s} → {ch.new_category:15s} ({ch.matched_rule})")
    if len(dry.changed) > 15:
        print(f"  ... and {len(dry.changed) - 15} more")

    if not args.apply:
        print("\nDry run only — re-run with --apply to write these changes.")
        return 0

    applied = apply_category_cleanup(db, dry_run=False)
    print(f"Applied: {applied.applied}, errors: {len(applied.errors)}")

    print("\n=== Phase 4 prioritization (score local catalog) ===")
    pr = run_prioritization_cycle(db, client=None)
    print(f"Scored: {pr.updated}, errors: {len(pr.errors)}")
    top = get_prioritized_portfolio(db, top_n=10)
    print("Top 10 by urgency:")
    for p in top:
        print(
            f"  {p.get('urgency_score'):6}  "
            f"{p.get('risk_category', ''):15s}  "
            f"{str(p.get('scenario_id'))[:50]}"
        )

    return 0 if not applied.errors and not pr.errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
