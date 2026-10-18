#!/usr/bin/env python3
"""Backwards-compatible entry point for the review CLI.

Prefer ``atlas review``.  The implementation lives in
``contingency_atlas.review_cli`` so the dashboard, the package, and this
script share one audited code path.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from contingency_atlas.review_cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
