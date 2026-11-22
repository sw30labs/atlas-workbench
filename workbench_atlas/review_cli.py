"""Review evidence events, emerging risks, and scenario signpost assessments.

Packaged as ``atlas review``.  Kept importable so the legacy
``scripts/review_records.py`` entry point and the dashboard share one audited
code path.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Sequence

REVIEW_STATUSES = (
    "pending",
    "approved",
    "rejected",
    "needs_update",
    "approved_synthetic",
    "not_reviewed",
    "superseded",
    "all",
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="atlas review", description=__doc__)
    parser.add_argument(
        "--evidence-db",
        type=Path,
        help="Evidence SQLite database (default: configured ATLAS_EVIDENCE_DB)",
    )
    parser.add_argument(
        "--risks-file",
        type=Path,
        help="Emerging-risk JSON file (default: configured ATLAS_EMERGING_RISKS)",
    )
    parser.add_argument(
        "--scenario-db",
        type=Path,
        help="Scenario SQLite database (default: configured ATLAS_DB)",
    )
    commands = parser.add_subparsers(dest="action", required=True)

    list_parser = commands.add_parser("list", help="List pending review records")
    list_parser.add_argument(
        "--kind",
        choices=("event", "risk", "signpost", "merge", "all"),
        default="all",
        help="Record kind to show (default: all)",
    )
    list_parser.add_argument(
        "--status",
        choices=REVIEW_STATUSES,
        default="pending",
        help="Review status to show (default: pending)",
    )
    list_parser.add_argument("--limit", type=int)

    def add_approval_flags(command: argparse.ArgumentParser) -> None:
        command.add_argument(
            "--confirm-public-source",
            action="store_true",
            help="Attest that all cited evidence is publicly accessible",
        )
        command.add_argument(
            "--confirm-rights",
            action="store_true",
            help="Attest that access, reuse, and redistribution rights were checked",
        )
        command.add_argument("--note", default="", help="Optional reviewer note")

    approve_event = commands.add_parser(
        "approve-event",
        help="Approve one evidence event",
    )
    approve_event.add_argument("record_id", help="Event ID")
    add_approval_flags(approve_event)

    approve_risk = commands.add_parser(
        "approve-risk",
        help="Approve one emerging risk",
    )
    approve_risk.add_argument("record_id", help="Exact risk issue")
    add_approval_flags(approve_risk)

    reject_event = commands.add_parser(
        "reject-event",
        help="Reject one evidence event",
    )
    reject_event.add_argument("record_id", help="Event ID")
    reject_event.add_argument(
        "--reason",
        help="Reviewer rationale (prompted interactively when omitted)",
    )

    reject_risk = commands.add_parser(
        "reject-risk",
        help="Reject one emerging risk",
    )
    reject_risk.add_argument("record_id", help="Exact risk issue")
    reject_risk.add_argument(
        "--reason",
        help="Reviewer rationale (prompted interactively when omitted)",
    )

    approve_signpost = commands.add_parser(
        "approve-signpost",
        help="Approve one pending scenario signpost assessment",
    )
    approve_signpost.add_argument(
        "record_id",
        help="Scenario ID (not the assessment row id shown as assessment_id)",
    )
    approve_signpost.add_argument("--note", default=None, help="Optional reviewer note")
    approve_signpost.add_argument(
        "--reason",
        default=None,
        help="Same value as --note",
    )

    reject_signpost = commands.add_parser(
        "reject-signpost",
        help="Reject one pending scenario signpost assessment",
    )
    reject_signpost.add_argument(
        "record_id",
        help="Scenario ID (not the assessment row id shown as assessment_id)",
    )
    reject_signpost.add_argument(
        "--reason",
        default=None,
        help="Reviewer rationale (prompted interactively when omitted)",
    )
    reject_signpost.add_argument(
        "--note",
        default=None,
        help="Same value as --reason",
    )

    for command in (approve_event, approve_risk, reject_event, reject_risk):
        command.add_argument("--reviewer", default=os.environ.get("USER") or "local-operator",
                             help="Named reviewer recorded in the decision history")
        command.add_argument("--expected-revision", help="Reject if the inspected record changed")

    def add_merge_flags(command: argparse.ArgumentParser) -> None:
        command.add_argument(
            "--reviewer",
            default=os.environ.get("USER") or os.environ.get("USERNAME") or "",
            help="Reviewer identity (default: current user)",
        )
        command.add_argument(
            "--reason",
            help="Written rationale (prompted interactively when omitted)",
        )

    approve_merge_cmd = commands.add_parser(
        "approve-merge",
        help="Approve one pending merge proposal (applies the register merge)",
    )
    approve_merge_cmd.add_argument("record_id", help="Merge proposal ID")
    add_merge_flags(approve_merge_cmd)

    reject_merge_cmd = commands.add_parser(
        "reject-merge",
        help="Reject one pending merge proposal (records only)",
    )
    reject_merge_cmd.add_argument("record_id", help="Merge proposal ID")
    add_merge_flags(reject_merge_cmd)

    rate_memo_cmd = commands.add_parser(
        "rate-memo",
        help="Spot-audit rating for a refutation memo (P6 kill-rule input)",
    )
    rate_memo_cmd.add_argument("record_id", help="Refutation memo ID")
    rate_memo_cmd.add_argument(
        "--rating",
        type=float,
        required=True,
        help="1.0 = compound-specific/useful, 0.0 = boilerplate",
    )
    return parser


def _emit(payload: object) -> None:
    json.dump(payload, sys.stdout, indent=2, ensure_ascii=False, default=str)
    sys.stdout.write("\n")


def _rejection_reason(value: str | None) -> str:
    if value and value.strip():
        return value.strip()
    if sys.stdin.isatty():
        return input("Rejection reason: ").strip()
    return ""


def _signpost_text(reason: str | None, note: str | None) -> str:
    """`--reason` and `--note` are one field. A supplied reason wins."""
    if reason and reason.strip():
        return reason.strip()
    if note and note.strip():
        return note.strip()
    return ""


def _prompted_reason(value: str | None, prompt: str) -> str:
    if value and value.strip():
        return value.strip()
    if sys.stdin.isatty():
        return input(prompt).strip()
    return ""


def main(argv: Sequence[str] | None = None) -> int:
    from workbench_atlas.config import get_settings
    from workbench_atlas.proposals import list_proposals
    from workbench_atlas.refutations import rate_memo
    from workbench_atlas.review import (
        ReviewError,
        approve_evidence,
        approve_merge,
        approve_risk,
        approve_signpost_assessment,
        list_evidence_for_review,
        list_risks_for_review,
        list_signpost_assessments_for_review,
        reject_evidence,
        reject_merge,
        reject_risk,
        reject_signpost_assessment,
    )

    args = _build_parser().parse_args(argv)
    settings = get_settings()
    evidence_db = args.evidence_db or settings.evidence_db_path
    risks_file = args.risks_file or settings.emerging_risks_path
    scenario_db = args.scenario_db or settings.scenario_db_path

    try:
        if args.action == "list":
            status = None if args.status == "all" else args.status
            events = (
                list_evidence_for_review(
                    evidence_db,
                    review_status=status,
                    limit=args.limit,
                )
                if args.kind in {"event", "all"}
                else None
            )
            risks = (
                list_risks_for_review(
                    risks_file,
                    review_status=status,
                    limit=args.limit,
                )
                if args.kind in {"risk", "all"}
                else None
            )
            signposts = (
                list_signpost_assessments_for_review(
                    scenario_db,
                    review_status=status,
                    limit=args.limit,
                )
                if args.kind in {"signpost", "all"}
                else None
            )
            merges = (
                list_proposals(
                    scenario_db,
                    status=None if args.status == "all" else status,
                )
                if args.kind in {"merge", "all"}
                else None
            )
            if args.kind == "event":
                payload = events
            elif args.kind == "risk":
                payload = risks
            elif args.kind == "signpost":
                payload = signposts
            elif args.kind == "merge":
                payload = merges
            else:
                payload = {
                    "events": events,
                    "risks": risks,
                    "signposts": signposts,
                    "merges": merges,
                }
        elif args.action == "approve-event":
            payload = approve_evidence(
                evidence_db,
                args.record_id,
                reviewer=args.reviewer, expected_revision=args.expected_revision,
                public_source_attested=args.confirm_public_source,
                rights_attested=args.confirm_rights,
                note=args.note,
            )
        elif args.action == "approve-risk":
            payload = approve_risk(
                risks_file,
                args.record_id,
                reviewer=args.reviewer, expected_revision=args.expected_revision,
                evidence_db=evidence_db,
                public_source_attested=args.confirm_public_source,
                rights_attested=args.confirm_rights,
                note=args.note,
            )
        elif args.action == "reject-event":
            payload = reject_evidence(
                evidence_db,
                args.record_id,
                reviewer=args.reviewer, expected_revision=args.expected_revision,
                reason=_rejection_reason(args.reason),
            )
        elif args.action == "reject-risk":
            payload = reject_risk(
                risks_file,
                args.record_id,
                reviewer=args.reviewer, expected_revision=args.expected_revision,
                reason=_rejection_reason(args.reason),
            )
        elif args.action == "approve-signpost":
            payload = approve_signpost_assessment(
                scenario_db,
                args.record_id,
                evidence_db=evidence_db,
                note=_signpost_text(args.reason, args.note),
            )
        elif args.action == "approve-merge":
            payload = approve_merge(
                scenario_db,
                risks_file,
                args.record_id,
                reviewer=args.reviewer,
                reason=_prompted_reason(args.reason, "Merge reason: "),
            )
        elif args.action == "reject-merge":
            payload = reject_merge(
                scenario_db,
                args.record_id,
                reviewer=args.reviewer,
                reason=_prompted_reason(args.reason, "Rejection reason: "),
            )
        elif args.action == "rate-memo":
            payload = rate_memo(scenario_db, args.record_id, args.rating)
        else:
            payload = reject_signpost_assessment(
                scenario_db,
                args.record_id,
                reason=_rejection_reason(_signpost_text(args.reason, args.note)),
            )
    except (ReviewError, ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"review error: {exc}", file=sys.stderr)
        return 2

    _emit(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
