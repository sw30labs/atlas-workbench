#!/usr/bin/env python3
"""Validate scouting sources or explicitly collect pending review proposals."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Sequence


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)

    validate = commands.add_parser(
        "validate",
        help="Validate a registry without using the network",
    )
    validate.add_argument("--registry", type=Path)

    collect = commands.add_parser(
        "collect",
        help="Collect proposal-only source packets (network opt-in required)",
    )
    collect.add_argument("--registry", type=Path)
    collect.add_argument(
        "--enable-network",
        action="store_true",
        help="Explicitly authorize bounded public-feed requests for this run",
    )
    collect.add_argument(
        "--source",
        dest="sources",
        action="append",
        help="Registry source ID to collect; repeat to select multiple",
    )
    collect.add_argument("--output", type=Path)
    collect.add_argument("--timeout-seconds", type=float, default=10.0)
    collect.add_argument("--max-response-bytes", type=int, default=1_000_000)
    collect.add_argument("--max-items-per-source", type=int, default=20)
    collect.add_argument("--max-total-items", type=int, default=100)
    collect.add_argument("--max-sources", type=int, default=10)
    collect.add_argument("--max-redirects", type=int, default=2)
    return parser


def _emit(value: object) -> None:
    json.dump(value, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")


def _write_json_atomic(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def main(argv: Sequence[str] | None = None) -> int:
    from contingency_atlas.scouting import (
        NetworkOptInRequired,
        RegistryValidationError,
        ScoutingError,
        ScoutingLimits,
        collect_proposals,
        load_registry,
        registry_summary,
    )

    args = _build_parser().parse_args(argv)
    try:
        registry = load_registry(args.registry)
        if args.action == "validate":
            _emit(registry_summary(registry))
            return 0
        limits = ScoutingLimits(
            timeout_seconds=args.timeout_seconds,
            max_response_bytes=args.max_response_bytes,
            max_items_per_source=args.max_items_per_source,
            max_total_items=args.max_total_items,
            max_sources=args.max_sources,
            max_redirects=args.max_redirects,
        )
        report = collect_proposals(
            registry,
            allow_network=args.enable_network,
            source_ids=args.sources,
            limits=limits,
        )
        if args.output:
            _write_json_atomic(args.output, report)
            _emit(
                {
                    "output": str(args.output),
                    "proposal_count": report["proposal_count"],
                    "error_count": report["error_count"],
                    "human_review_required": True,
                    "evidence_records_created": 0,
                }
            )
        else:
            _emit(report)
        return 1 if report["error_count"] else 0
    except (
        NetworkOptInRequired,
        RegistryValidationError,
        ScoutingError,
        OSError,
    ) as exc:
        print(f"scouting error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
