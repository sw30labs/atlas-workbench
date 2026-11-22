"""Manual review workflow for evidence events and emerging-risk records.

Approval is deliberately explicit. Real records cannot become approved unless
the reviewer attests both that the cited evidence is publicly accessible and
that reuse/redistribution rights have been checked. Synthetic records already
marked ``approved_synthetic`` are immutable through this workflow.

This module also hosts the merge-proposal decisions (P8, ADR-014):
``approve_merge``/``reject_merge`` are the only write path for proposal
state, and approval performs the register merge under ``register_lock``
with provenance stamps — single reviewer plus a written reason, no
attestations (citations keep the two-attestation flow).
"""

from __future__ import annotations

import contextlib
import ipaddress
import json
import re
import sqlite3
import time
from datetime import date, datetime, timezone
from pathlib import Path
from collections.abc import Sequence
from typing import Any

try:  # POSIX advisory locking; absent on Windows (lock degrades to no-op)
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None  # type: ignore[assignment]

from .db import (
    SIGNPOST_ASSESSMENT_STATUSES,
    SIGNPOST_DIRECTIONAL_STATUSES,
    decide_signpost_assessment,
    get_evidence_event,
    get_latest_signpost_assessment,
    list_evidence_events,
    list_signpost_assessments,
    update_evidence_event_fields,
)
from .phases.phase2_risks import load_emerging_risks, save_emerging_risks
from .provenance import is_approved_record, is_public_http_url
from .schema import INSTRUMENT_STATUSES
from .decisions import append_decision, decision_event, record_revision


APPROVED_SYNTHETIC = "approved_synthetic"
PUBLIC_PROVENANCE_CLASSES = frozenset({"public_primary", "public_secondary"})
_SHA256 = re.compile(r"^[0-9a-f]{64}$", re.I)
_URL_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*://", re.I)
_WINDOWS_PATH = re.compile(r"^[a-z]:[\\/]", re.I)

_EVENT_REQUIRED_TEXT = (
    "event_id",
    "headline",
    "source_publisher",
    "source_title",
    "source_url",
    "source_date",
    "retrieved_at",
    "source_type",
    "record_type",
    "provenance_class",
    "independently_authored_summary",
    "content_fingerprint",
    "taxonomy_version",
    "provenance_note",
)


class ReviewError(ValueError):
    """Raised when a requested review action is invalid or unsafe."""


REGISTER_LOCK_SUFFIX = ".lock"
_LOCK_POLL_SECONDS = 0.15


@contextlib.contextmanager
def register_lock(path: str | Path, *, timeout: float = 5.0):
    """Cross-process advisory lock for emerging-risk register mutations.

    The register is a JSON file mutated read-modify-write by two writers:
    this review workflow (CLI and dashboard) and phase 2's monitoring
    cycle. Holding an ``flock`` on a sidecar ``<register>.lock`` for the
    whole load→save span prevents the two from silently losing each
    other's updates. The lock is released automatically if the holding
    process dies. On platforms without ``fcntl`` (Windows) this degrades
    to a no-op and single-writer discipline is on the operator.
    """
    if fcntl is None:  # pragma: no cover - Windows fallback
        yield
        return
    lock_path = Path(str(path) + REGISTER_LOCK_SUFFIX)
    handle = open(lock_path, "a+")
    deadline = time.monotonic() + max(0.0, timeout)
    try:
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise ReviewError(
                        "The emerging-risk register is locked by another "
                        "process (a running monitoring cycle or another "
                        "reviewer). Try again shortly."
                    ) from None
                time.sleep(_LOCK_POLL_SECONDS)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _text(value: object) -> str:
    return str(value).strip() if value is not None else ""


def _append_note(existing: object, message: str) -> str:
    current = _text(existing)
    return f"{current}\n{message}" if current else message


def _require_attestations(
    *,
    public_source_attested: bool,
    rights_attested: bool,
) -> None:
    missing: list[str] = []
    if not public_source_attested:
        missing.append("public-source")
    if not rights_attested:
        missing.append("rights")
    if missing:
        raise ReviewError(
            "Approval requires explicit " + " and ".join(missing) + " attestation"
        )


def _ensure_not_synthetic(record: dict[str, Any], label: str) -> None:
    if _text(record.get("review_status")) == APPROVED_SYNTHETIC:
        raise ReviewError(f"{label} is approved synthetic data and is immutable")


def _require_evidence_db(db_path: str | Path) -> None:
    if not Path(db_path).is_file():
        raise ReviewError(f"Evidence database does not exist: {db_path}")


def _require_scenario_db(db_path: str | Path) -> None:
    if not Path(db_path).is_file():
        raise ReviewError(f"Scenario database does not exist: {db_path}")


def _get_evidence(db_path: str | Path, event_id: str) -> dict[str, Any] | None:
    _require_evidence_db(db_path)
    try:
        return get_evidence_event(str(db_path), event_id)
    except sqlite3.Error as exc:
        raise ReviewError(
            f"Evidence database is not initialized or readable: {db_path}: {exc}"
        ) from exc


def _update_evidence(
    db_path: str | Path,
    event_id: str,
    fields: dict[str, Any],
) -> bool:
    try:
        return update_evidence_event_fields(str(db_path), event_id, fields)
    except sqlite3.Error as exc:
        raise ReviewError(f"Could not update evidence event {event_id}: {exc}") from exc


def _valid_utc_access_time(value: object) -> bool:
    raw = _text(value)
    if not raw:
        return False
    try:
        if len(raw) == 10:
            date.fromisoformat(raw)
            return True
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() == timezone.utc.utcoffset(
        parsed
    )


def _valid_source_locator(value: object) -> bool:
    """Accept document locators while excluding private URLs and local paths."""
    raw = _text(value)
    if not raw:
        return True
    if len(raw) > 500 or any(ord(character) < 32 for character in raw):
        return False
    if raw.startswith(("/", "./", "../", "~", "\\")) or _WINDOWS_PATH.match(raw):
        return False
    if _URL_SCHEME.match(raw):
        return is_public_http_url(raw)
    if raw.lower().startswith(("file:", "data:", "javascript:")):
        return False
    if re.match(r"^[^/\s:@]+:[^/\s@]+@", raw):
        return False

    # Also reject bare private hosts such as ``localhost:8000/page``.
    candidate = re.split(r"[:/\s]", raw, maxsplit=1)[0].strip("[]").rstrip(".").lower()
    if candidate == "localhost" or candidate.endswith(
        (".local", ".internal", ".corp", ".intranet")
    ):
        return False
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        return True
    return address.is_global


def validate_evidence_for_approval(record: dict[str, Any]) -> None:
    """Validate required public-source metadata for one evidence event."""
    missing = [name for name in _EVENT_REQUIRED_TEXT if not _text(record.get(name))]
    if missing:
        raise ReviewError(f"Evidence is missing required metadata: {', '.join(missing)}")
    if record.get("record_type") != "event":
        raise ReviewError("Evidence record_type must be 'event'")
    if record.get("provenance_class") not in PUBLIC_PROVENANCE_CLASSES:
        raise ReviewError(
            "Evidence provenance_class must be public_primary or public_secondary"
        )
    if _text(record.get("source_type")).lower() == "synthetic":
        raise ReviewError("Synthetic evidence cannot be approved as a public source")
    if _text(record.get("source_type")).lower() == "regulatory_release":
        if _text(record.get("instrument_status")).lower() not in INSTRUMENT_STATUSES:
            raise ReviewError(
                "regulatory_release evidence requires instrument_status "
                "proposed, enacted, enforcement, or vacated"
            )
    if not is_public_http_url(record.get("source_url")):
        raise ReviewError("Evidence source_url must be a canonical public HTTP(S) URL")
    if not _valid_source_locator(record.get("source_locator")):
        raise ReviewError(
            "Evidence source_locator must be document text or a public HTTP(S) URL"
        )
    try:
        parsed_source_date = date.fromisoformat(_text(record.get("source_date")))
    except ValueError:
        raise ReviewError("Evidence source_date must use YYYY-MM-DD") from None
    if parsed_source_date.isoformat() != _text(record.get("source_date")):
        raise ReviewError("Evidence source_date must use YYYY-MM-DD")
    if not _valid_utc_access_time(record.get("retrieved_at")):
        raise ReviewError("Evidence retrieved_at must be a UTC date or timestamp")
    if not _SHA256.fullmatch(_text(record.get("content_fingerprint"))):
        raise ReviewError("Evidence content_fingerprint must be a SHA-256 hex digest")


def list_evidence_for_review(
    db_path: str | Path,
    *,
    review_status: str | None = "pending",
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """List evidence records, optionally filtered by review status."""
    _require_evidence_db(db_path)
    try:
        records = list_evidence_events(str(db_path))
    except sqlite3.Error as exc:
        raise ReviewError(
            f"Evidence database is not initialized or readable: {db_path}: {exc}"
        ) from exc
    if review_status is not None:
        records = [
            record
            for record in records
            if _text(record.get("review_status")) == review_status
        ]
    if limit is not None:
        records = records[: max(0, int(limit))]
    return records


def _check_revision(record: dict[str, Any], expected_revision: str | None) -> None:
    if expected_revision is not None and record_revision(record) != expected_revision:
        raise ReviewError("This record changed after you opened it. Refresh and review the latest revision.")


def _decide_evidence(db_path: str | Path, event_id: str, *, action: str,
                     reason: str, reviewer: str, expected_revision: str | None,
                     public_source_attested: bool = False,
                     rights_attested: bool = False) -> dict[str, Any]:
    _require_evidence_db(db_path)
    if action == "approve":
        _require_attestations(public_source_attested=public_source_attested,
                              rights_attested=rights_attested)
    elif not _text(reason):
        raise ReviewError("Evidence rejection requires a reason")
    conn = sqlite3.connect(str(db_path), timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM evidence_events WHERE event_id=?", (event_id,)).fetchone()
        if row is None:
            raise ReviewError(f"Evidence event not found: {event_id}")
        record = dict(row)
        _check_revision(record, expected_revision)
        _ensure_not_synthetic(record, f"Evidence event {event_id}")
        if action == "approve":
            validate_evidence_for_approval(record)
            message = "Manual review approved public-source access and rights attestations."
            if _text(reason):
                message += f" Reviewer note: {_text(reason)}"
        else:
            message = f"Manual review rejected this evidence. Reason: {_text(reason)}"
        fields = {
            "review_status": "approved" if action == "approve" else "rejected",
            "reviewed_at": _now(),
            "provenance_note": _append_note(record.get("provenance_note"), message),
        }
        if action == "approve":
            fields["rights_review"] = "attested_for_public_use"
        updated = {**record, **fields}
        event = decision_event(kind="event", record_id=event_id, actor=reviewer,
                               action=action, reason=reason or message,
                               before=record, after=updated,
                               attestations={"public_source": public_source_attested,
                                             "rights": rights_attested})
        assignments = ", ".join(f'"{name}"=?' for name in fields)
        conn.execute(f"UPDATE evidence_events SET {assignments} WHERE event_id=?",
                     [*fields.values(), event_id])
        append_decision(conn, event, record, updated)
        conn.commit()
        return updated
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def approve_evidence(db_path: str | Path, event_id: str, *,
                     public_source_attested: bool = False,
                     rights_attested: bool = False, note: str = "",
                     reviewer: str = "local-operator",
                     expected_revision: str | None = None) -> dict[str, Any]:
    """Approve evidence atomically with the inspected revision and decision history."""
    return _decide_evidence(db_path, event_id, action="approve", reason=note,
                           reviewer=reviewer, expected_revision=expected_revision,
                           public_source_attested=public_source_attested,
                           rights_attested=rights_attested)


def reject_evidence(db_path: str | Path, event_id: str, *, reason: str,
                    reviewer: str = "local-operator",
                    expected_revision: str | None = None) -> dict[str, Any]:
    """Reject evidence without losing intervening decisions."""
    return _decide_evidence(db_path, event_id, action="reject", reason=reason,
                           reviewer=reviewer, expected_revision=expected_revision)


def _load_risk(
    risks_path: str | Path,
    issue: str,
) -> tuple[list[dict[str, Any]], int, dict[str, Any]]:
    risks = load_emerging_risks(risks_path)
    matches = [
        index
        for index, risk in enumerate(risks)
        if isinstance(risk, dict) and _text(risk.get("issue")) == issue
    ]
    if not matches:
        raise ReviewError(f"Emerging risk not found: {issue}")
    if len(matches) > 1:
        raise ReviewError(f"Emerging risk issue is not unique: {issue}")
    index = matches[0]
    return risks, index, risks[index]


def validate_risk_for_approval(
    record: dict[str, Any],
    *,
    evidence_db: str | Path | None = None,
) -> list[str]:
    """Validate a risk and return canonical approved evidence event IDs."""
    required = ("issue", "description", "source_type", "provenance_note")
    missing = [name for name in required if not _text(record.get(name))]
    if missing:
        raise ReviewError(f"Risk is missing required metadata: {', '.join(missing)}")
    if _text(record.get("source_type")).lower() == "synthetic":
        raise ReviewError("Synthetic risk cannot be approved as public research")
    refs = record.get("evidence_refs")
    if not isinstance(refs, list) or not refs:
        raise ReviewError(
            "Risk evidence_refs must be a non-empty list"
        )
    if evidence_db is None:
        raise ReviewError(
            "Risk approval requires an initialized evidence database"
        )
    _require_evidence_db(evidence_db)
    try:
        candidates = list_evidence_events(str(evidence_db), approved_only=True)
    except sqlite3.Error as exc:
        raise ReviewError(
            f"Evidence database is not initialized or readable: {evidence_db}: {exc}"
        ) from exc
    by_id = {
        _text(item.get("event_id")): item
        for item in candidates
        if _text(item.get("event_id"))
    }
    by_url = {
        _text(item.get("source_url")): item
        for item in candidates
        if _text(item.get("source_url"))
    }

    unresolved: list[str] = []
    canonical_ids: list[str] = []
    for value in refs:
        ref = _text(value)
        evidence = by_url.get(ref) if is_public_http_url(ref) else by_id.get(ref)
        if (
            evidence is None
            or evidence.get("review_status") != "approved"
            or evidence.get("rights_review") != "attested_for_public_use"
            or not _valid_utc_access_time(evidence.get("reviewed_at"))
        ):
            unresolved.append(ref or repr(value))
            continue
        try:
            validate_evidence_for_approval(evidence)
        except ReviewError:
            unresolved.append(ref or repr(value))
            continue
        event_id = _text(evidence.get("event_id"))
        if event_id not in canonical_ids:
            canonical_ids.append(event_id)
    if unresolved:
        raise ReviewError(
            "Risk evidence_refs must resolve to fully approved evidence records "
            "with public provenance by event ID or exact source URL: "
            + ", ".join(unresolved)
        )
    return canonical_ids


def _report_check(key: str, label: str, ok: bool, detail: str = "") -> dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "status": "pass" if ok else "fail",
        "detail": detail,
    }


def evidence_validation_report(record: dict[str, Any]) -> dict[str, Any]:
    """Non-raising checklist mirror of :func:`validate_evidence_for_approval`.

    Same checks, but every check is evaluated and returned instead of
    raising on the first failure — this feeds the dashboard's traffic
    lights. Keep in step with the raising validator; the test suite
    asserts the two agree on approvability.
    """
    checks: list[dict[str, Any]] = []
    missing = [name for name in _EVENT_REQUIRED_TEXT if not _text(record.get(name))]
    checks.append(_report_check(
        "metadata", "Required metadata complete", not missing,
        ("missing: " + ", ".join(missing)) if missing else "all fields present",
    ))
    checks.append(_report_check(
        "record_type", "Record type is 'event'",
        record.get("record_type") == "event", _text(record.get("record_type")) or "—",
    ))
    checks.append(_report_check(
        "provenance_class", "Public provenance class",
        record.get("provenance_class") in PUBLIC_PROVENANCE_CLASSES,
        _text(record.get("provenance_class")) or "—",
    ))
    synthetic = _text(record.get("source_type")).lower() == "synthetic"
    immutable = _text(record.get("review_status")) == APPROVED_SYNTHETIC
    checks.append(_report_check(
        "not_synthetic", "Not synthetic data", not (synthetic or immutable),
        "immutable approved_synthetic record" if immutable
        else (_text(record.get("source_type")) or "—"),
    ))
    checks.append(_report_check(
        "source_url", "Canonical public source URL",
        is_public_http_url(record.get("source_url")),
        _text(record.get("source_url"))[:140] or "—",
    ))
    checks.append(_report_check(
        "source_locator", "Source locator is public/document text",
        _valid_source_locator(record.get("source_locator")),
        _text(record.get("source_locator"))[:140] or "(empty)",
    ))
    raw_date = _text(record.get("source_date"))
    try:
        date_ok = date.fromisoformat(raw_date).isoformat() == raw_date
    except ValueError:
        date_ok = False
    checks.append(_report_check(
        "source_date", "Source date is YYYY-MM-DD", date_ok, raw_date or "—",
    ))
    checks.append(_report_check(
        "retrieved_at", "Retrieval time is UTC",
        _valid_utc_access_time(record.get("retrieved_at")),
        _text(record.get("retrieved_at")) or "—",
    ))
    checks.append(_report_check(
        "content_fingerprint", "SHA-256 content fingerprint",
        bool(_SHA256.fullmatch(_text(record.get("content_fingerprint")))),
        (_text(record.get("content_fingerprint"))[:16] + "…")
        if record.get("content_fingerprint") else "—",
    ))
    source_type = _text(record.get("source_type")).lower()
    status = _text(record.get("instrument_status")).lower()
    instrument_ok = (
        source_type != "regulatory_release" or status in INSTRUMENT_STATUSES
    )
    checks.append(_report_check(
        "instrument_status",
        "Instrument status on regulatory_release",
        instrument_ok,
        status or "missing",
    ))
    blockers = [c["label"] for c in checks if c["status"] != "pass"]
    return {
        "kind": "event",
        "approvable": not blockers,
        "checks": checks,
        "refs": [],
        "blockers": blockers,
    }


def _classify_risk_refs(
    record: dict[str, Any],
    evidence_db: str | Path | None,
) -> list[dict[str, Any]]:
    """Classify each evidence ref of a risk against the evidence store.

    Statuses: ``approved`` (resolves, counts toward approval),
    ``approved_synthetic``/``pending``/``rejected`` (exists but is not an
    approved public record), ``unattested`` (approved but missing rights
    attestation or a valid review timestamp), ``invalid`` (approved but
    fails evidence validation), ``missing`` (no matching record).
    """
    refs = record.get("evidence_refs")
    if not isinstance(refs, list):
        return []
    try:
        candidates = list_evidence_events(str(evidence_db)) if evidence_db else []
    except sqlite3.Error:
        candidates = []
    by_id = {_text(i.get("event_id")): i for i in candidates if _text(i.get("event_id"))}
    by_url = {_text(i.get("source_url")): i for i in candidates if _text(i.get("source_url"))}

    details: list[dict[str, Any]] = []
    for value in refs:
        ref = _text(value)
        row = by_url.get(ref) if is_public_http_url(ref) else by_id.get(ref)
        entry: dict[str, Any] = {"ref": ref or repr(value), "event_id": None, "headline": None}
        if row is None:
            entry.update(status="missing", detail="no evidence record with this ID/URL")
            details.append(entry)
            continue
        entry["event_id"] = _text(row.get("event_id")) or None
        entry["headline"] = _text(row.get("headline")) or None
        status = _text(row.get("review_status")) or "unreviewed"
        if status != "approved":
            entry.update(status=status, detail=f"evidence review_status is {status!r}")
            details.append(entry)
            continue
        if (
            row.get("rights_review") != "attested_for_public_use"
            or not _valid_utc_access_time(row.get("reviewed_at"))
        ):
            entry.update(status="unattested",
                         detail="approved but missing rights attestation or review time")
            details.append(entry)
            continue
        try:
            validate_evidence_for_approval(row)
        except ReviewError as exc:
            entry.update(status="invalid", detail=str(exc))
            details.append(entry)
            continue
        entry.update(status="approved", detail="resolves to fully approved evidence")
        details.append(entry)
    return details


def risk_validation_report(
    record: dict[str, Any],
    *,
    evidence_db: str | Path | None = None,
) -> dict[str, Any]:
    """Non-raising checklist mirror of :func:`validate_risk_for_approval`.

    Every check is evaluated (nothing raises) so the dashboard can show
    traffic lights and per-ref resolution *before* the reviewer decides.
    Keep in step with the raising validator; the test suite asserts the
    two agree on approvability.
    """
    checks: list[dict[str, Any]] = []
    required = ("issue", "description", "source_type", "provenance_note")
    missing = [name for name in required if not _text(record.get(name))]
    checks.append(_report_check(
        "metadata", "Required metadata complete", not missing,
        ("missing: " + ", ".join(missing)) if missing else "all fields present",
    ))
    synthetic = _text(record.get("source_type")).lower() == "synthetic"
    immutable = _text(record.get("review_status")) == APPROVED_SYNTHETIC
    checks.append(_report_check(
        "not_synthetic", "Not synthetic data", not (synthetic or immutable),
        "immutable approved_synthetic record" if immutable
        else ("synthetic risks are demo data and cannot be approved as public research"
              if synthetic else _text(record.get("source_type")) or "—"),
    ))
    refs = record.get("evidence_refs")
    refs_listed = isinstance(refs, list) and bool(refs)
    checks.append(_report_check(
        "refs_listed", "Evidence refs present", refs_listed,
        f"{len(refs)} ref(s)" if refs_listed else "evidence_refs must be a non-empty list",
    ))
    db_ok = evidence_db is not None and Path(evidence_db).is_file()
    checks.append(_report_check(
        "evidence_db", "Evidence database available", db_ok,
        str(evidence_db) if evidence_db else "no evidence database configured",
    ))
    ref_details = _classify_risk_refs(record, evidence_db) if db_ok else []
    unresolved = [r for r in ref_details if r["status"] != "approved"]
    checks.append(_report_check(
        "refs_resolve", "All refs resolve to approved evidence",
        refs_listed and db_ok and not unresolved,
        ("all refs approved" if refs_listed and db_ok and not unresolved else
         f"{len(unresolved)} of {len(ref_details)} ref(s) do not resolve"),
    ))
    blockers = [c["label"] for c in checks if c["status"] != "pass"]
    return {
        "kind": "risk",
        "approvable": not blockers,
        "checks": checks,
        "refs": ref_details,
        "blockers": blockers,
    }


def list_risks_for_review(
    risks_path: str | Path,
    *,
    review_status: str | Sequence[str] | None = "pending",
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """List emerging risks, optionally filtered by review status.

    Accepts several statuses so a caller can queue every state that is waiting
    on a human, not just ``pending``.
    """
    risks = [
        risk
        for risk in load_emerging_risks(risks_path)
        if isinstance(risk, dict)
    ]
    if review_status is not None:
        wanted = (
            {review_status}
            if isinstance(review_status, str)
            else set(review_status)
        )
        risks = [
            risk
            for risk in risks
            if _text(risk.get("review_status")) in wanted
        ]
    if limit is not None:
        risks = risks[: max(0, int(limit))]
    return risks


def approve_risk(
    risks_path: str | Path,
    issue: str,
    *,
    evidence_db: str | Path | None = None,
    public_source_attested: bool = False,
    rights_attested: bool = False,
    note: str = "",
    reviewer: str = "local-operator",
    expected_revision: str | None = None,
) -> dict[str, Any]:
    """Approve one emerging risk after explicit human attestations."""
    _require_attestations(
        public_source_attested=public_source_attested,
        rights_attested=rights_attested,
    )
    with register_lock(risks_path):
        risks, index, record = _load_risk(risks_path, issue)
        _check_revision(record, expected_revision)
        _ensure_not_synthetic(record, f"Emerging risk {issue}")
        canonical_refs = validate_risk_for_approval(
            record,
            evidence_db=evidence_db,
        )

        reviewed_at = _now()
        message = "Manual review approved public-source access and rights attestations."
        if _text(note):
            message += f" Reviewer note: {_text(note)}"
        updated = dict(record)
        updated.update(
            {
                "rights_review": "attested_for_public_use",
                "review_status": "approved",
                "reviewed_at": reviewed_at,
                "last_reviewed": reviewed_at,
                "evidence_refs": canonical_refs,
                "provenance_note": _append_note(record.get("provenance_note"), message),
            }
        )
        updated["reviewed_by"] = reviewer.strip()
        event = decision_event(kind="risk", record_id=str(record.get("risk_id") or issue),
                               actor=reviewer, action="approve", reason=note or message,
                               before=record, after=updated,
                               attestations={"public_source": public_source_attested,
                                             "rights": rights_attested})
        updated["review_history"] = [*record.get("review_history", []), event]
        risks[index] = updated
        save_emerging_risks(risks_path, risks)
    return updated


def reject_risk(
    risks_path: str | Path,
    issue: str,
    *,
    reason: str,
    reviewer: str = "local-operator",
    expected_revision: str | None = None,
) -> dict[str, Any]:
    """Reject one emerging risk with a required human-readable reason."""
    reason = _text(reason)
    if not reason:
        raise ReviewError("Risk rejection requires a reason")
    with register_lock(risks_path):
        risks, index, record = _load_risk(risks_path, issue)
        _check_revision(record, expected_revision)
        _ensure_not_synthetic(record, f"Emerging risk {issue}")
        reviewed_at = _now()
        updated = dict(record)
        updated.update(
            {
                "review_status": "rejected",
                "reviewed_at": reviewed_at,
                "last_reviewed": reviewed_at,
                "provenance_note": _append_note(
                    record.get("provenance_note"),
                    f"Manual review rejected this risk. Reason: {reason}",
                ),
            }
        )
        updated["reviewed_by"] = reviewer.strip()
        event = decision_event(kind="risk", record_id=str(record.get("risk_id") or issue),
                               actor=reviewer, action="reject", reason=reason,
                               before=record, after=updated)
        updated["review_history"] = [*record.get("review_history", []), event]
        risks[index] = updated
        save_emerging_risks(risks_path, risks)
    return updated


def list_signpost_assessments_for_review(
    db_path: str | Path,
    *,
    review_status: str | None = "pending",
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """List immutable assessment snapshots, optionally filtered by review status."""
    _require_scenario_db(db_path)
    try:
        return list_signpost_assessments(
            str(db_path),
            review_status=review_status,
            limit=limit,
        )
    except sqlite3.Error as exc:
        raise ReviewError(
            f"Scenario database is not initialized or readable: {db_path}: {exc}"
        ) from exc


def _pending_signpost_assessment(
    db_path: str | Path,
    scenario_id: str,
) -> dict[str, Any]:
    _require_scenario_db(db_path)
    try:
        record = get_latest_signpost_assessment(
            str(db_path),
            scenario_id,
        )
    except sqlite3.Error as exc:
        raise ReviewError(
            f"Scenario database is not initialized or readable: {db_path}: {exc}"
        ) from exc
    if record is None:
        raise ReviewError(
            f"Scenario {scenario_id} has no signpost assessment history"
        )
    if _text(record.get("review_status")) != "pending":
        raise ReviewError(
            f"Scenario {scenario_id} has no pending signpost assessment"
        )
    assessment_status = _text(record.get("classification"))
    if assessment_status not in SIGNPOST_ASSESSMENT_STATUSES:
        raise ReviewError(
            f"Scenario {scenario_id} has an invalid signpost assessment status"
        )
    if not _text(record.get("rationale")):
        raise ReviewError(
            f"Scenario {scenario_id} has no signpost assessment rationale"
        )
    return record


def _assessment_ref_list(value: object, field_name: str) -> list[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ReviewError(
                f"Signpost assessment {field_name} must contain a JSON list"
            ) from exc
    if not isinstance(value, list):
        raise ReviewError(
            f"Signpost assessment {field_name} must be a list"
        )
    normalized: list[str] = []
    for item in value:
        ref = _text(item)
        if ref and ref not in normalized:
            normalized.append(ref)
    return normalized


def _validate_signpost_evidence_for_approval(
    assessment: dict[str, Any],
    *,
    evidence_db: str | Path | None,
) -> None:
    classification = _text(assessment.get("classification"))
    exact_refs = _assessment_ref_list(
        assessment.get("evidence_refs"),
        "evidence_refs",
    )
    considered_refs = _assessment_ref_list(
        assessment.get("considered_refs"),
        "considered_refs",
    )
    if classification in SIGNPOST_DIRECTIONAL_STATUSES and not exact_refs:
        raise ReviewError(
            f"{classification} signpost assessment requires exact evidence refs"
        )
    if not considered_refs:
        raise ReviewError(
            "Signpost assessment requires considered evidence refs"
        )
    if not set(exact_refs).issubset(set(considered_refs)):
        raise ReviewError(
            "Signpost evidence_refs must be a subset of considered_refs"
        )
    if evidence_db is None:
        raise ReviewError(
            "Signpost approval requires an initialized evidence database"
        )
    _require_evidence_db(evidence_db)
    try:
        approved = list_evidence_events(str(evidence_db), approved_only=True)
    except sqlite3.Error as exc:
        raise ReviewError(
            f"Evidence database is not initialized or readable: "
            f"{evidence_db}: {exc}"
        ) from exc
    by_id = {
        _text(record.get("event_id")): record
        for record in approved
        if _text(record.get("event_id"))
    }
    by_url = {
        _text(record.get("source_url")): record
        for record in approved
        if _text(record.get("source_url"))
    }
    unresolved: list[str] = []
    for ref in considered_refs:
        record = by_url.get(ref) if is_public_http_url(ref) else by_id.get(ref)
        if record is None or not is_approved_record(record):
            unresolved.append(ref)
    if unresolved:
        raise ReviewError(
            "Signpost assessment references must still resolve to approved "
            "evidence by event ID or exact source URL: "
            + ", ".join(unresolved)
        )


def approve_signpost_assessment(
    db_path: str | Path,
    scenario_id: str,
    *,
    evidence_db: str | Path | None = None,
    note: str = "",
) -> dict[str, Any]:
    """Approve one model-assisted signpost interpretation without lifecycle changes."""
    record = _pending_signpost_assessment(db_path, scenario_id)
    _validate_signpost_evidence_for_approval(
        record,
        evidence_db=evidence_db,
    )
    reviewed_at = _now()
    message = (
        f"Manual signpost review approved the "
        f"{_text(record.get('classification'))!r} assessment at {reviewed_at}."
    )
    if _text(note):
        message += f" Reviewer note: {_text(note)}"
    try:
        return decide_signpost_assessment(
            str(db_path),
            scenario_id,
            assessment_id=int(record["id"]),
            decision="approved",
            reviewed_at=reviewed_at,
            reviewer_note=_text(note),
            audit_message=message,
            evidence_db_path=str(evidence_db),
        )
    except (sqlite3.Error, ValueError) as exc:
        raise ReviewError(
            f"Could not update scenario {scenario_id}: {exc}"
        ) from exc


def reject_signpost_assessment(
    db_path: str | Path,
    scenario_id: str,
    *,
    reason: str,
) -> dict[str, Any]:
    """Reject one signpost interpretation while retaining it for audit."""
    reason = _text(reason)
    if not reason:
        raise ReviewError("Signpost assessment rejection requires a reason")
    record = _pending_signpost_assessment(db_path, scenario_id)
    reviewed_at = _now()
    message = (
        f"Manual signpost review rejected the "
        f"{_text(record.get('classification'))!r} assessment at {reviewed_at}. "
        f"Reason: {reason}"
    )
    try:
        return decide_signpost_assessment(
            str(db_path),
            scenario_id,
            assessment_id=int(record["id"]),
            decision="rejected",
            reviewed_at=reviewed_at,
            reviewer_note=reason,
            audit_message=message,
        )
    except (sqlite3.Error, ValueError) as exc:
        raise ReviewError(
            f"Could not update scenario {scenario_id}: {exc}"
        ) from exc


# ── merge proposals (P8 atom-identity hygiene; ADR-014) ──────────────────
#
# Merge proposals are a proposal-only review item type: machinery ranks
# candidates and stores pending rows (proposals.py), humans dispose. These
# two functions are the ONLY write path to a proposal's decided state —
# same ADR-013 pattern as the evidence/risk/signpost decision functions.
# Approval additionally performs the second sanctioned register mutation
# (ADR-014): dedup.apply_merge under the register lock, provenance-stamped,
# single reviewer with a written reason. The advisory verdict stored on
# the proposal is display-only and is never read here.


def _require_proposal_gate(reviewer: str, reason: str, label: str) -> tuple[str, str]:
    reviewer = _text(reviewer)
    reason = _text(reason)
    if not reviewer:
        raise ReviewError(f"{label} decisions require a reviewer identity")
    if not reason:
        raise ReviewError(f"{label} decisions require a written reason")
    return reviewer, reason


def _require_merge_gate(reviewer: str, reason: str) -> tuple[str, str]:
    return _require_proposal_gate(reviewer, reason, "Merge")


def _pending_merge_proposal(db_path: str | Path, proposal_id: str) -> dict[str, Any]:
    from .proposals import get_proposal

    proposal = get_proposal(db_path, proposal_id)
    if proposal is None:
        raise ReviewError(f"Merge proposal not found: {proposal_id}")
    if _text(proposal.get("status")) != "pending":
        raise ReviewError(
            f"Merge proposal {proposal_id} has no pending decision "
            f"(status: {_text(proposal.get('status')) or 'unknown'})"
        )
    return proposal


def _find_register_record(
    risks: list[dict[str, Any]], title: str
) -> dict[str, Any]:
    matches = [
        r
        for r in risks
        if isinstance(r, dict)
        and _text(r.get("issue") or r.get("title")) == _text(title)
    ]
    if not matches:
        raise ReviewError(f"Emerging risk not found: {title}")
    if len(matches) > 1:
        raise ReviewError(f"Emerging risk title is not unique: {title}")
    return matches[0]


def _choose_merge_survivor(
    risks: list[dict[str, Any]], proposal: dict[str, Any]
) -> tuple[str, str]:
    """Deterministic keep/drop choice for a merge proposal.

    The record with more evidence_refs survives (it carries the reviewed
    citation mass); ties break on earlier first_seen, then on the exact
    title. Fixed and documented (ADR-014) so the direction of a merge is
    reconstructable from the register, not from a proposal author's whim.
    Both records are re-resolved from the CURRENT register — never from
    the possibly stale proposal payload.
    """
    rec_a = _find_register_record(risks, _text(proposal.get("title_a")))
    rec_b = _find_register_record(risks, _text(proposal.get("title_b")))

    def _wins(x: dict[str, Any], y: dict[str, Any]) -> bool:
        """True when record x should survive over record y (total order)."""
        refs_x = x.get("evidence_refs")
        refs_y = y.get("evidence_refs")
        n_x = len(refs_x) if isinstance(refs_x, list) else 0
        n_y = len(refs_y) if isinstance(refs_y, list) else 0
        if n_x != n_y:
            return n_x > n_y  # more reviewed citation mass survives
        first_x = _text(x.get("first_seen") or x.get("first_observed"))
        first_y = _text(y.get("first_seen") or y.get("first_observed"))
        if first_x != first_y:
            if not first_x:
                return False  # unknown sighting date loses
            if not first_y:
                return True
            return first_x < first_y  # earlier sighting wins
        title_x = _text(x.get("issue") or x.get("title"))
        title_y = _text(y.get("issue") or y.get("title"))
        return title_x <= title_y  # final deterministic tie-break

    keep, drop = (rec_a, rec_b) if _wins(rec_a, rec_b) else (rec_b, rec_a)
    return (
        _text(keep.get("issue") or keep.get("title")),
        _text(drop.get("issue") or drop.get("title")),
    )


def approve_merge(
    db_path: str | Path,
    risks_path: str | Path,
    proposal_id: str,
    *,
    reviewer: str,
    reason: str,
) -> dict[str, Any]:
    """Approve one pending merge proposal: apply the register merge and
    record the decision.

    Single-reviewer gate (ADR-014): a named reviewer and a non-empty
    written reason are required — there are no evidence attestations on
    this path (citations keep the two-attestation flow of approve_risk).
    The register mutation happens inside ``dedup.apply_merge`` under
    ``register_lock`` with provenance stamps on both records; the
    before/after diff is stored on the proposal row. Approving a proposal
    whose pair was already merged is a recorded no-op (the stored diff
    says ``already_merged``), never a corrupt register.
    """
    from .dedup import MergeError, apply_merge
    from .proposals import decide_merge_proposal

    reviewer, reason = _require_merge_gate(reviewer, reason)
    proposal = _pending_merge_proposal(db_path, proposal_id)
    risks = load_emerging_risks(risks_path)
    keep_title, drop_title = _choose_merge_survivor(risks, proposal)
    try:
        diff = apply_merge(
            risks_path, keep_title, drop_title,
            reviewer=reviewer, reason=reason,
        )
    except MergeError as exc:
        raise ReviewError(str(exc)) from exc
    decided_at = _now()
    try:
        decide_merge_proposal(
            db_path,
            proposal_id,
            decision="approved",
            reviewer=reviewer,
            reason=reason,
            decided_at=decided_at,
            merge_diff=diff,
        )
    except (sqlite3.Error, ValueError) as exc:
        raise ReviewError(
            f"Could not record merge proposal decision {proposal_id}: {exc}"
        ) from exc
    return {"proposal_id": proposal_id, "decision": "approved",
            "decided_at": decided_at, "merge": diff}


def reject_merge(
    db_path: str | Path,
    proposal_id: str,
    *,
    reviewer: str,
    reason: str,
) -> dict[str, Any]:
    """Reject one pending merge proposal. Records only — the register is
    never touched on this path."""
    from .proposals import decide_merge_proposal

    reviewer, reason = _require_merge_gate(reviewer, reason)
    _pending_merge_proposal(db_path, proposal_id)
    decided_at = _now()
    try:
        decide_merge_proposal(
            db_path,
            proposal_id,
            decision="rejected",
            reviewer=reviewer,
            reason=reason,
            decided_at=decided_at,
        )
    except (sqlite3.Error, ValueError) as exc:
        raise ReviewError(
            f"Could not record merge proposal decision {proposal_id}: {exc}"
        ) from exc
    return {"proposal_id": proposal_id, "decision": "rejected",
            "decided_at": decided_at}


# ── channel proposals (P1) and analogue dossiers (P2); ADR-014 ───────────
#
# Same proposal-only contract as merges: machinery proposes (proposals.py),
# humans dispose, and these functions are the ONLY write path to a decided
# state. Unlike merges, approval mutates NOTHING beyond the row — the P1
# graph/register application and any P2 prior conversion are explicitly
# deferred (ADR-014(c)); approved channel proposals feed only the join's
# shadow pass, approved analogues only the display dossiers.


def _pending_channel_proposal(
    db_path: str | Path, proposal_id: str
) -> dict[str, Any]:
    from .proposals import get_channel_proposal

    proposal = get_channel_proposal(db_path, proposal_id)
    if proposal is None:
        raise ReviewError(f"Channel proposal not found: {proposal_id}")
    if _text(proposal.get("status")) != "pending":
        raise ReviewError(
            f"Channel proposal {proposal_id} has no pending decision "
            f"(status: {_text(proposal.get('status')) or 'unknown'})"
        )
    return proposal


def approve_channel_proposal(
    db_path: str | Path,
    proposal_id: str,
    *,
    reviewer: str,
    reason: str,
) -> dict[str, Any]:
    """Approve one pending channel proposal. Records ONLY the decision.

    Single-reviewer gate (ADR-014): named reviewer + written reason, no
    attestations. Approval changes nothing except the row status —
    ``graph_version_applied`` stays NULL and the production graph, the
    register, and every score are untouched on this branch; the row feeds
    the join's shadow pass only.
    """
    from .proposals import decide_channel_proposal

    reviewer, reason = _require_proposal_gate(reviewer, reason, "Channel proposal")
    _pending_channel_proposal(db_path, proposal_id)
    decided_at = _now()
    try:
        decide_channel_proposal(
            db_path,
            proposal_id,
            decision="approved",
            reviewer=reviewer,
            reason=reason,
            decided_at=decided_at,
        )
    except (sqlite3.Error, ValueError) as exc:
        raise ReviewError(
            f"Could not record channel proposal decision {proposal_id}: {exc}"
        ) from exc
    return {"proposal_id": proposal_id, "decision": "approved",
            "decided_at": decided_at}


def reject_channel_proposal(
    db_path: str | Path,
    proposal_id: str,
    *,
    reviewer: str,
    reason: str,
) -> dict[str, Any]:
    """Reject one pending channel proposal. Records only — nothing else
    is touched on this path (same as every proposal kind)."""
    from .proposals import decide_channel_proposal

    reviewer, reason = _require_proposal_gate(reviewer, reason, "Channel proposal")
    _pending_channel_proposal(db_path, proposal_id)
    decided_at = _now()
    try:
        decide_channel_proposal(
            db_path,
            proposal_id,
            decision="rejected",
            reviewer=reviewer,
            reason=reason,
            decided_at=decided_at,
        )
    except (sqlite3.Error, ValueError) as exc:
        raise ReviewError(
            f"Could not record channel proposal decision {proposal_id}: {exc}"
        ) from exc
    return {"proposal_id": proposal_id, "decision": "rejected",
            "decided_at": decided_at}


def _pending_analogue(db_path: str | Path, analogue_id: str) -> dict[str, Any]:
    from .proposals import get_analogue

    analogue = get_analogue(db_path, analogue_id)
    if analogue is None:
        raise ReviewError(f"Analogue not found: {analogue_id}")
    if _text(analogue.get("status")) != "pending":
        raise ReviewError(
            f"Analogue {analogue_id} has no pending decision "
            f"(status: {_text(analogue.get('status')) or 'unknown'})"
        )
    return analogue


def validate_analogue_for_approval(record: dict[str, Any]) -> None:
    """Validate an analogue dossier's claim fields before approval.

    The register's evidence bar applied to a claim (P2): the analogue must
    be mechanism-mapped, carry at least one citation, and name its
    disanalogies. Non-raising display mirrors are not needed — the review
    queue shows the raw claim fields and the human decides; this is the
    hard gate, in the style of :func:`validate_evidence_for_approval`.
    """
    mechanism = record.get("mechanism_map")
    if not isinstance(mechanism, dict) or not any(
        _text(v) for v in mechanism.values()
    ):
        raise ReviewError("Analogue approval requires a non-empty mechanism map")
    citations = record.get("citations")
    if not isinstance(citations, list) or not any(_text(c) for c in citations):
        raise ReviewError("Analogue approval requires at least one citation")
    disanalogies = record.get("disanalogies")
    if not isinstance(disanalogies, list) or not any(
        _text(d) for d in disanalogies
    ):
        raise ReviewError("Analogue approval requires non-empty disanalogies")


def approve_analogue(
    db_path: str | Path,
    analogue_id: str,
    *,
    reviewer: str,
    reason: str,
) -> dict[str, Any]:
    """Approve one pending analogue dossier. Records ONLY the decision.

    Single-reviewer gate (ADR-014) plus the claim validation of
    :func:`validate_analogue_for_approval`. Approval converts nothing into
    a prior and touches no score — the dossier becomes displayable
    workshop material; P2→prior conversion is out of program. Claim
    fields are append-only: this function never rewrites them.
    """
    from .proposals import decide_analogue

    reviewer, reason = _require_proposal_gate(reviewer, reason, "Analogue")
    analogue = _pending_analogue(db_path, analogue_id)
    validate_analogue_for_approval(analogue)
    decided_at = _now()
    try:
        decide_analogue(
            db_path,
            analogue_id,
            decision="approved",
            reviewer=reviewer,
            reason=reason,
            decided_at=decided_at,
        )
    except (sqlite3.Error, ValueError) as exc:
        raise ReviewError(
            f"Could not record analogue decision {analogue_id}: {exc}"
        ) from exc
    return {"analogue_id": analogue_id, "decision": "approved",
            "decided_at": decided_at}


def reject_analogue(
    db_path: str | Path,
    analogue_id: str,
    *,
    reviewer: str,
    reason: str,
) -> dict[str, Any]:
    """Reject one pending analogue dossier. Records only — the claim
    stays stored (append-only) and nothing else is touched."""
    from .proposals import decide_analogue

    reviewer, reason = _require_proposal_gate(reviewer, reason, "Analogue")
    _pending_analogue(db_path, analogue_id)
    decided_at = _now()
    try:
        decide_analogue(
            db_path,
            analogue_id,
            decision="rejected",
            reviewer=reviewer,
            reason=reason,
            decided_at=decided_at,
        )
    except (sqlite3.Error, ValueError) as exc:
        raise ReviewError(
            f"Could not record analogue decision {analogue_id}: {exc}"
        ) from exc
    return {"analogue_id": analogue_id, "decision": "rejected",
            "decided_at": decided_at}


# ── My MITRE ATLAS applicability-mapping review path ────────────────
#
# I use the same proposal-only boundary as channels and analogues. I record
# each claim against a pinned release and reserve pending-state decisions for
# these review-facing functions. I never change scenarios, catalog data, or
# scores here.


def _pending_mitre_atlas_mapping(
    db_path: str | Path,
    proposal_id: str,
) -> dict[str, Any]:
    from .frameworks.mappings import get_mapping_proposal

    proposal = get_mapping_proposal(db_path, proposal_id)
    if proposal is None:
        raise ReviewError(f"MITRE ATLAS mapping proposal not found: {proposal_id}")
    if _text(proposal.get("status")) != "pending":
        raise ReviewError(
            f"MITRE ATLAS mapping proposal {proposal_id} has no pending "
            f"decision (status: {_text(proposal.get('status')) or 'unknown'})"
        )
    return proposal


def approve_mitre_atlas_mapping(
    db_path: str | Path,
    proposal_id: str,
    *,
    reviewer: str,
    reason: str,
) -> dict[str, Any]:
    """I approve a hypothesized ATLAS mapping without applying a mutation."""
    from .frameworks.mappings import MappingError, decide_mapping_proposal

    reviewer, reason = _require_proposal_gate(
        reviewer,
        reason,
        "MITRE ATLAS mapping",
    )
    _pending_mitre_atlas_mapping(db_path, proposal_id)
    decided_at = _now()
    try:
        decide_mapping_proposal(
            db_path,
            proposal_id,
            decision="approved",
            reviewer=reviewer,
            reason=reason,
            decided_at=decided_at,
        )
    except (sqlite3.Error, MappingError) as exc:
        raise ReviewError(
            f"Could not record MITRE ATLAS mapping decision {proposal_id}: {exc}"
        ) from exc
    return {
        "proposal_id": proposal_id,
        "decision": "approved",
        "decided_at": decided_at,
    }


def reject_mitre_atlas_mapping(
    db_path: str | Path,
    proposal_id: str,
    *,
    reviewer: str,
    reason: str,
) -> dict[str, Any]:
    """I reject one ATLAS mapping while retaining its immutable claim history."""
    from .frameworks.mappings import MappingError, decide_mapping_proposal

    reviewer, reason = _require_proposal_gate(
        reviewer,
        reason,
        "MITRE ATLAS mapping",
    )
    _pending_mitre_atlas_mapping(db_path, proposal_id)
    decided_at = _now()
    try:
        decide_mapping_proposal(
            db_path,
            proposal_id,
            decision="rejected",
            reviewer=reviewer,
            reason=reason,
            decided_at=decided_at,
        )
    except (sqlite3.Error, MappingError) as exc:
        raise ReviewError(
            f"Could not record MITRE ATLAS mapping decision {proposal_id}: {exc}"
        ) from exc
    return {
        "proposal_id": proposal_id,
        "decision": "rejected",
        "decided_at": decided_at,
    }
