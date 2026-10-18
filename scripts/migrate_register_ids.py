#!/usr/bin/env python3
"""One-time migration: persist a stable ``risk_id`` on every register record.

Ontology constraint C-03. The register had no id field, so every join key was
recomputed from the free-text title: the scorer and ``compound_graduate``
derived slugs on each load, the guard minted an id per run, and
``review_suggestions.record_id`` stored the title itself. Editing a title
silently moved the record's identity and orphaned everything pointing at it —
the dangling compound references behind C-02.

The id written here is the lossless title slug, which is what
``compound_scenarios.risk_ids``, the scorer and ``atlas:slugId`` already join
on, so persisting it changes no key that resolves today. Afterwards every
reader prefers the stored value and a title edit no longer moves identity.

Idempotent: records that already carry a ``risk_id`` are left untouched.

    python scripts/migrate_register_ids.py            # report only
    python scripts/migrate_register_ids.py --apply    # write
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from contingency_atlas.review import register_lock  # noqa: E402
from contingency_atlas.scoring.inventory import slugify  # noqa: E402


def assign_ids(register: list[dict]) -> list[tuple[dict, str, bool]]:
    """Return (record, risk_id, is_new) triples.

    Collision suffixes follow the order the readers use, so an id assigned
    here matches the key those readers derive for the same record today.
    """
    seen: dict[str, int] = {}
    out: list[tuple[dict, str, bool]] = []
    for record in register:
        title = record.get("issue") or record.get("title") or "untitled-risk"
        base = slugify(title)
        n = seen.get(base, 0)
        seen[base] = n + 1
        derived = base if n == 0 else f"{base}-{n + 1}"
        existing = str(record.get("risk_id") or "").strip()
        out.append((record, existing or derived, not existing))
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--register",
        type=Path,
        default=ROOT / "emerging_risks.json",
        help="register path (default: emerging_risks.json)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write the file; without it the migration only reports",
    )
    args = parser.parse_args()

    # The register is mutated read-modify-write by the review workflow and by
    # phase 2; hold the same advisory lock they do for the whole span.
    with register_lock(args.register):
        return _migrate(args)


def _migrate(args) -> int:
    register = json.loads(args.register.read_text())
    if not isinstance(register, list):
        print(f"{args.register} is not a list of records", file=sys.stderr)
        return 1

    assignments = assign_ids(register)
    pending = [(rec, rid) for rec, rid, is_new in assignments if is_new]
    ids = [rid for _, rid, _ in assignments]

    duplicates = sorted({rid for rid in ids if ids.count(rid) > 1})
    if duplicates:
        print("refusing to migrate — these ids are not unique:", file=sys.stderr)
        for rid in duplicates:
            print(f"  {rid}", file=sys.stderr)
        return 1

    print(f"register: {args.register} ({len(register)} records)")
    print(f"already carry a risk_id: {len(register) - len(pending)}")
    print(f"to assign: {len(pending)}")
    for record, rid in pending:
        print(f"  {rid}  <-  {record.get('issue') or record.get('title')!r}")

    if not pending:
        print("nothing to do")
        return 0
    if not args.apply:
        print("\ndry run — pass --apply to write")
        return 0

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup_dir = ROOT / "generated" / "backups" / f"pre-id-migration-{stamp}"
    backup_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(args.register, backup_dir / args.register.name)
    print(f"\nbackup: {backup_dir / args.register.name}")

    for record, rid, is_new in assignments:
        if is_new:
            record["risk_id"] = rid

    # Byte-for-byte the format save_emerging_risks writes, so the migration
    # does not show up as a whole-file reformat in the next backup diff.
    tmp = args.register.with_suffix(args.register.suffix + ".tmp")
    tmp.write_text(json.dumps(register, indent=2, ensure_ascii=False))
    tmp.replace(args.register)
    print(f"wrote {args.register} ({len(pending)} ids assigned)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
