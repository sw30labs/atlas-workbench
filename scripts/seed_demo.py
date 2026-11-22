#!/usr/bin/env python3
"""Backwards-compatible entry point for the demo seeder.

Prefer ``atlas demo``.  This wrapper keeps the historical
``python scripts/seed_demo.py`` invocation working for existing automation.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from workbench_atlas.demo import DemoError, seed_into  # noqa: E402

# Historical name used by automation and tests.
seed = seed_into


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Seed local runtime files from the synthetic demo fixtures."
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "generated" / "demo",
        help="destination directory (default: generated/demo)",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="replace only the two generated demo databases before seeding",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="allow seeding into the repository root (almost never wanted)",
    )
    args = parser.parse_args()
    try:
        result = seed_into(args.output_dir, reset=args.reset, force=args.force)
    except DemoError as exc:
        print(f"seed_demo: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
