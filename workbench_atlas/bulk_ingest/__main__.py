"""CLI: ``python -m workbench_atlas.bulk_ingest <folder>`` and ``... submit <batch.json>``."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from ..config import get_settings


def _table(title: str, rows: dict[str, Any]) -> list[str]:
    lines = [title]
    if not rows:
        return lines + ["  (none)"]
    width = max(len(k) for k in rows)
    for key, value in rows.items():
        if isinstance(value, dict):
            value = "  ".join(f"{k}={v}" for k, v in value.items())
        lines.append(f"  {key.ljust(width)}  {value}")
    return lines


def format_summary(batch: dict[str, Any]) -> str:
    counts = batch["counts"]
    lines = [f"Batch {batch['batch_id']} ({batch['status']}) — {counts['files']} files",
             f"Output: {batch.get('out_dir') or '(not written)'}", ""]
    lines += _table("Files by route", counts["files_by_route"]) + [""]
    lines += _table("Files by flag", counts["files_by_flag"]) + [""]
    lines += _table("Records by contract", counts["records_by_contract"]) + [""]
    lines += _table("Issues by severity", counts["issues_by_severity"]) + [""]
    lines.append("Nothing was imported or approved. Review the batch, then use the submit subcommand.")
    return "\n".join(lines)


def _run(argv: list[str]) -> int:
    from .orchestrator import run_batch

    parser = argparse.ArgumentParser(prog="python -m workbench_atlas.bulk_ingest",
                                     description="Stage a folder of organization documents for review.")
    parser.add_argument("folder")
    parser.add_argument("--out", help="output directory (default: <workspace>/generated/bulk_ingest/<batch_id>)")
    parser.add_argument("--llm", action="store_true", help="enable the local LLM lane (narrative + tiebreaks)")
    parser.add_argument("--json", action="store_true", help="print the batch summary as JSON")
    parser.add_argument("--actor", default="local operator", help="operator preparing the batch")
    parser.add_argument("--synthetic", action="store_true", help="label staged records classification=synthetic")
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args(argv)
    result = run_batch(get_settings(), args.folder, actor=args.actor, use_llm=args.llm, out_dir=args.out,
                       max_workers=args.workers, classification="synthetic" if args.synthetic else "organization")
    if args.json:
        print(json.dumps(result.summary(), indent=2))
    else:
        print(format_summary({**result.batch, "out_dir": result.out_dir}))
    return 0


def _submit(argv: list[str]) -> int:
    from .submit import SubmitError, load_batch, plan_submission, submit_batch

    parser = argparse.ArgumentParser(prog="python -m workbench_atlas.bulk_ingest submit",
                                     description="Import selected org-context records through the review gate.")
    parser.add_argument("batch")
    parser.add_argument("--actor", required=True, help="named reviewer recorded on every decision")
    parser.add_argument("--select", required=True, help="'all' (ready records only) or comma-separated ids")
    parser.add_argument("--yes", action="store_true", help="confirm without the interactive prompt")
    args = parser.parse_args(argv)
    selection: str | list[str] = "all" if args.select.strip() == "all" else \
        [s.strip() for s in args.select.split(",") if s.strip()]
    try:
        batch, _ = load_batch(args.batch)
        plan = plan_submission(batch, selection)
    except (SubmitError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"Batch {plan['batch_id']}: {plan['selected']} record(s) will be written by {args.actor!r}")
    for step in plan["payloads"]:
        print(f"  {step['file']}")
        for record in step["records"]:
            print(f"    {record['kind']:<20} {record['id']}  {record['label']}")
    for warning in plan["warnings"]:
        print(f"  warning: {warning}")
    if plan["skipped"]:
        print(f"  {len(plan['skipped'])} record(s) not selected (needs_review or invalid)")
    if not plan["payloads"]:
        print("Nothing to write.")
        return 1
    if not args.yes:
        if not sys.stdin.isatty():
            print("Refusing to write without --yes in a non-interactive session.", file=sys.stderr)
            return 2
        if input("Type 'yes' to write these records: ").strip().lower() != "yes":
            print("Aborted; nothing was written.")
            return 1
    try:
        result = submit_batch(get_settings(), args.batch, selection=selection, actor=args.actor)
    except (SubmitError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"Imported {result['imported_count']} record(s) into the private organization workspace.")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "submit":
        return _submit(argv[1:])
    return _run(argv)


if __name__ == "__main__":
    raise SystemExit(main())
