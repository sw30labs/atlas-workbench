"""Frozen private baseline review packs and scheduler-friendly change digests."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from contingency_atlas import __version__
from contingency_atlas.config import get_settings
from contingency_atlas.context import compare_baseline
from contingency_atlas.exports import EXPORT_SCHEMA_VERSION, FORMATS, ExportError, _json_bytes, render_snapshot

MAX_REPORT_BYTES = 512 * 1024 * 1024
MAX_RECEIPT_BYTES = 4096
_DIGEST = re.compile(r"[a-f0-9]{64}\Z")


def _encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")

def _hash(value):
    return hashlib.sha256(_encoded(value)).hexdigest()

def _changes(section, *, kind=None):
    rows = []
    for change in ("added", "removed"):
        for record in section.get(change, []):
            rows.append({"record_id": record.get("id") or record.get("scenario_id") or record.get("event_id"),
                         "record_type": kind or record.get("kind"), "change": change,
                         "field": "record", "before": record if change == "removed" else None,
                         "after": record if change == "added" else None})
    for record in section.get("changed", []):
        before, after = record["before"], record["after"]
        fields = record.get("fields") or [{"field": key, "before": before.get(key), "after": after.get(key)}
                   for key in sorted(before.keys() | after.keys())
                   if key not in {"revision", "updated_at"} and before.get(key) != after.get(key)]
        for field in fields:
            rows.append({"record_id": record["id"], "record_type": kind or record.get("kind"),
                         "change": "changed", **field})
    return rows


def build_assessment_snapshot(settings, *, baseline_id, include_context=False):
    """Capture an explicit baseline comparison, including private organization data.

    This is deliberately separate from scenario-linked catalog exports: the
    organization baseline covers the whole declared workspace. It never widens
    a scenario selection through an implicit baseline attachment.
    """
    if include_context is not True:
        raise ExportError("A baseline report contains private organization data; explicitly include organization context.")
    if not isinstance(baseline_id, str) or not baseline_id.strip() or len(baseline_id) > 200:
        raise ExportError("Choose a saved organization baseline.")
    started = datetime.now(timezone.utc).isoformat()
    comparison = compare_baseline(settings, baseline_id)
    research = comparison.get("research_changes", {})
    tables = {name: [] for name in ("scenarios", "risks", "compound_scenarios", "evidence_events", "relationships")}
    tables["organization_review_summary"] = [{
        "title": comparison["baseline_name"], "baseline_id": baseline_id,
        "organization_revision": comparison["store_revision"],
        "organization_changes": comparison["counts"],
        "research_status": research.get("status", "not_captured"),
        "research_scope": research.get("scope"),
        "baseline_as_of": research.get("baseline_as_of"), "current_as_of": research.get("current_as_of"),
        "read_windows": research.get("read_windows"),
        "missing_evidence_ids": research.get("missing_evidence_ids"),
    }]
    tables["organization_changes"] = _changes(comparison)
    tables["organization_research_changes"] = []
    for kind in ("scenarios", "evidence"):
        part = research.get(kind, {})
        tables["organization_research_changes"].extend(_changes(part, kind=kind))
    tables["organization_review_notes"] = [{"note": text} for text in research.get("notes", [])]
    if research.get("status") != "compared":
        tables["organization_review_notes"].append({"note": "Research changes are incomplete or were not captured. Empty differences do not establish no change."})
    # Change identity excludes capture timestamps. A scheduled unchanged digest
    # can reuse the previous report without silently rewriting it.
    digest = _hash({"baseline_id": baseline_id, "organization_changes": tables["organization_changes"],
                    "research_changes": tables["organization_research_changes"],
                    "research_status": research.get("status"),
                    "missing_evidence_ids": research.get("missing_evidence_ids")})
    checksum = hashlib.sha256(_json_bytes(tables)).hexdigest()
    changed = bool(tables["organization_changes"] or tables["organization_research_changes"])
    notice = "Private organization review. These are observed changes since a named baseline, not approvals, confirmed incidents or calibrated forecasts."
    manifest = {
        "schema_version": EXPORT_SCHEMA_VERSION, "application_version": __version__,
        "snapshot_id": "assessment-" + checksum[:16], "generated_at": datetime.now(timezone.utc).isoformat(),
        "read_started_at": started, "consistency": "Organization comparison and separate research read windows; no cross-store atomic snapshot.",
        "selection": {"baseline_id": baseline_id, "include_context": True, "organization_scope": "entire baseline workspace"},
        "record_counts": {key: len(rows) for key, rows in tables.items()}, "tables_sha256": checksum,
        "table_checksums": {key: hashlib.sha256(_json_bytes(rows)).hexdigest() for key, rows in tables.items()},
        "classification": "includes_organization_context", "notice": notice,
        "warnings": [notice], "missing_optional_tables": [], "taxonomy_versions": [], "algorithm_versions": [],
        "change_digest": digest, "has_changes": changed, "research_status": research.get("status", "not_captured"),
    }
    return {"manifest": manifest, "tables": tables}


def _receipt_path(output):
    return output.with_name("." + output.name + ".receipt.json")


def _regular_file(path):
    """Open exactly this regular file without following a planted symlink."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise ExportError("The digest report or completion receipt is unavailable; use a new output directory after inspection.") from exc
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise ExportError("The digest report and completion receipt must be regular files.")
    return os.fdopen(descriptor, "rb")


def _verify_existing(output, *, change_digest, format):
    """A pathname alone cannot establish that an earlier write completed."""
    with _regular_file(_receipt_path(output)) as stream:
        if os.fstat(stream.fileno()).st_size > MAX_RECEIPT_BYTES:
            raise ExportError("The digest completion receipt is invalid.")
        raw = stream.read(MAX_RECEIPT_BYTES + 1)
    try:
        receipt = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ExportError("The digest completion receipt is invalid.") from exc
    fields = {"schema_version", "filename", "change_digest", "format", "artifact_size", "artifact_sha256"}
    if (not isinstance(receipt, dict) or set(receipt) != fields
            or type(receipt["schema_version"]) is not int or receipt["schema_version"] != 1
            or receipt["filename"] != output.name or receipt["change_digest"] != change_digest
            or receipt["format"] != format or type(receipt["artifact_size"]) is not int
            or not 1 <= receipt["artifact_size"] <= MAX_REPORT_BYTES
            or not isinstance(receipt["artifact_sha256"], str) or not _DIGEST.fullmatch(receipt["artifact_sha256"])):
        raise ExportError("The digest completion receipt does not match the requested report.")
    checksum = hashlib.sha256()
    with _regular_file(output) as stream:
        before = os.fstat(stream.fileno())
        if before.st_size != receipt["artifact_size"]:
            raise ExportError("The existing digest failed its size integrity check; it was not treated as complete.")
        remaining = receipt["artifact_size"]
        while remaining:
            chunk = stream.read(min(1024 * 1024, remaining))
            if not chunk:
                raise ExportError("The existing digest is incomplete.")
            checksum.update(chunk)
            remaining -= len(chunk)
        after = os.fstat(stream.fileno())
        if stream.read(1) or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ExportError("The existing digest changed during verification.")
    if checksum.hexdigest() != receipt["artifact_sha256"]:
        raise ExportError("The existing digest failed its checksum integrity check; it was not treated as complete.")


def _stage(output_directory, content):
    descriptor, name = tempfile.mkstemp(prefix=".atlas-digest-stage-", dir=output_directory)
    path = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path


def _publish_digest(output, content, *, change_digest, format, allow_existing):
    """Publish a complete report and its completion receipt without overwrites.

    A crash between the two publications leaves a visible incomplete pair;
    future calls refuse to report success until the operator investigates it.
    """
    if not content or len(content) > MAX_REPORT_BYTES:
        raise ExportError("A digest report must contain between 1 byte and 512 MB; select a smaller review scope.")
    receipt_path = _receipt_path(output)
    # lexists also detects dangling symlinks and rejects them before staging.
    if os.path.lexists(output) or os.path.lexists(receipt_path):
        if not allow_existing:
            raise ExportError("This change digest already exists. Choose a new output directory to prepare another copy.")
        _verify_existing(output, change_digest=change_digest, format=format)
        return False
    receipt = {"schema_version": 1, "filename": output.name, "change_digest": change_digest,
               "format": format, "artifact_size": len(content), "artifact_sha256": hashlib.sha256(content).hexdigest()}
    staged_report = staged_receipt = None
    published_report = False
    try:
        staged_report = _stage(output.parent, content)
        staged_receipt = _stage(output.parent, _json_bytes(receipt))
        try:
            # Linking on the same filesystem publishes a fully fsynced inode
            # atomically and fails if another writer already published the name.
            os.link(staged_report, output)
            published_report = True
        except FileExistsError:
            if not allow_existing:
                raise ExportError("This change digest was created concurrently; no file was overwritten.") from None
            _verify_existing(output, change_digest=change_digest, format=format)
            return False
        os.link(staged_receipt, receipt_path)
        return True
    except BaseException:
        # Remove only our own newly published report, never an existing or
        # concurrently substituted file. A process kill may leave it for explicit
        # inspection; the verifier will reject the missing completion receipt.
        if published_report and staged_report is not None:
            try:
                current, staged = output.lstat(), staged_report.stat()
                if (current.st_dev, current.st_ino) == (staged.st_dev, staged.st_ino):
                    output.unlink()
            except OSError:
                pass
        raise
    finally:
        for staged in (staged_report, staged_receipt):
            if staged is not None:
                staged.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Prepare a private baseline digest without sending messages or running a model.")
    parser.add_argument("--baseline-id", required=True)
    parser.add_argument("--include-context", action="store_true", help="Explicitly authorize private organization content in this output")
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--format", choices=FORMATS, default="pdf")
    parser.add_argument("--only-changes", action="store_true", help="Skip verified unchanged comparisons and already prepared change digests")
    args = parser.parse_args(argv)
    try:
        snapshot = build_assessment_snapshot(get_settings(), baseline_id=args.baseline_id, include_context=args.include_context)
        manifest = snapshot["manifest"]
        if args.only_changes and not manifest["has_changes"] and manifest["research_status"] == "compared":
            print("No verified changes since this baseline.")
            return 0
        artifact = render_snapshot(snapshot, format=args.format)
        args.output_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        output = args.output_directory / ("atlas-digest-" + manifest["change_digest"] + "." + artifact.filename.rsplit(".", 1)[-1])
        created = _publish_digest(output, artifact.content, change_digest=manifest["change_digest"],
                                  format=args.format, allow_existing=args.only_changes)
        print(f"Prepared private review pack: {output}" if created else
              "This change digest has already been prepared and its integrity was verified.")
        return 0
    except (ValueError, OSError) as exc:
        print(f"Digest failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
