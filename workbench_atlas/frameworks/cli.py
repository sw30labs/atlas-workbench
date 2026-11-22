"""I provide command-line management for external-framework catalogs."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .mitre_atlas import DEFAULT_MAX_BYTES, import_mitre_atlas
from .store import activate_release, framework_status


def _print(value: Any, *, stream: Any | None = None) -> None:
    print(
        json.dumps(value, indent=2, sort_keys=True),
        file=sys.stdout if stream is None else stream,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="atlas-framework",
        description="Import and manage offline external-framework catalogs.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    importer = subparsers.add_parser(
        "mitre-import", help="Import a local MITRE ATLAS v6 YAML release."
    )
    importer.add_argument("--db", default="atlas.db", help="Scenario database path.")
    importer.add_argument("--input", required=True, type=Path, help="Local YAML path.")
    importer.add_argument(
        "--source-url",
        required=True,
        help=(
            "Operator-asserted provenance URL recorded with these bytes; "
            "I do not fetch it."
        ),
    )
    importer.add_argument(
        "--expected-sha256",
        help="Optional 64-hex SHA-256 pin that I verify before parsing.",
    )
    importer.add_argument(
        "--activate", action="store_true", help="Make this release active after import."
    )
    importer.add_argument(
        "--max-bytes",
        type=int,
        default=DEFAULT_MAX_BYTES,
        help=argparse.SUPPRESS,
    )

    status = subparsers.add_parser("status", help="Show installed catalog releases.")
    status.add_argument("--db", default="atlas.db", help="Scenario database path.")

    activate = subparsers.add_parser("activate", help="Activate an imported release.")
    activate.add_argument("version", help="Imported content version.")
    activate.add_argument("--db", default="atlas.db", help="Scenario database path.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "mitre-import":
            result = import_mitre_atlas(
                args.db,
                args.input,
                args.source_url,
                activate=args.activate,
                max_bytes=args.max_bytes,
                expected_sha256=args.expected_sha256,
            )
        elif args.command == "status":
            result = framework_status(args.db)
        else:
            result = activate_release(args.db, args.version)
        _print(result)
        return 0
    except (OSError, ValueError, KeyError) as exc:
        _print({"error": str(exc)}, stream=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover - console entry point
    raise SystemExit(main())
