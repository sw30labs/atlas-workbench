"""Dashboard integration for draft-first research intake.

The preparation graph is read-only.  This module keeps its result in a small,
expiring in-memory store and exposes a separate confirmation seam that writes
selected, QA-passing records as *pending* review items.  Raw intake is never
written to disk and no path here can approve a record.
"""

from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from contingency_atlas.config import Settings
from contingency_atlas.db import add_evidence_event, list_evidence_events
from contingency_atlas.guard_hook import evidence_event_record, validate
from contingency_atlas.intake import (
    IntakeCandidate,
    IntakePreparation,
    make_llm_extractor,
    make_llm_judge,
    make_llm_repairer,
    prepare_intake,
)
from contingency_atlas.singularity_source import (
    collect_relevant_stories,
    compose_material,
    record_source_job,
    record_submitted_stories,
)
from contingency_atlas.phases.phase2_risks import (
    load_emerging_risks,
    normalize_risk_record,
    save_emerging_risks,
)
from contingency_atlas.provenance import url_dedup_variants
from contingency_atlas.review import register_lock

MAX_DRAFTS = 25
DRAFT_TTL_SECONDS = 60 * 60


class IntakeFlowError(ValueError):
    """A safe, operator-facing intake workflow error."""


@dataclass
class StoredIntakeDraft:
    draft_id: str
    preparation: IntakePreparation
    evidence_payloads: dict[str, dict[str, Any]]
    created_at: float
    submitted_ids: set[str] = field(default_factory=set)
    inflight_ids: set[str] = field(default_factory=set)
    singularity_stories: list[dict[str, Any]] = field(default_factory=list)


class IntakeDraftStore:
    """Thread-safe, bounded, process-local store for non-persistent drafts."""

    def __init__(
        self,
        *,
        max_drafts: int = MAX_DRAFTS,
        ttl_seconds: int = DRAFT_TTL_SECONDS,
    ) -> None:
        self.max_drafts = max(1, int(max_drafts))
        self.ttl_seconds = max(1, int(ttl_seconds))
        self._drafts: dict[str, StoredIntakeDraft] = {}
        self._lock = threading.Lock()

    def _purge_locked(self, now: float) -> None:
        expired = [
            draft_id
            for draft_id, draft in self._drafts.items()
            if now - draft.created_at >= self.ttl_seconds
        ]
        for draft_id in expired:
            del self._drafts[draft_id]
        while len(self._drafts) >= self.max_drafts:
            oldest = min(
                self._drafts.values(), key=lambda draft: draft.created_at
            )
            del self._drafts[oldest.draft_id]

    def create(
        self,
        preparation: IntakePreparation,
        evidence_payloads: dict[str, dict[str, Any]],
        singularity_stories: list[dict[str, Any]] | None = None,
    ) -> StoredIntakeDraft:
        now = time.monotonic()
        draft = StoredIntakeDraft(
            draft_id=f"draft-{uuid.uuid4().hex[:12]}",
            preparation=preparation,
            evidence_payloads=dict(evidence_payloads),
            created_at=now,
            singularity_stories=list(singularity_stories or []),
        )
        with self._lock:
            self._purge_locked(now)
            self._drafts[draft.draft_id] = draft
        return draft

    def get(self, draft_id: str) -> StoredIntakeDraft:
        now = time.monotonic()
        with self._lock:
            self._purge_locked(now)
            draft = self._drafts.get(draft_id)
            if draft is None:
                raise IntakeFlowError(
                    "Intake draft was not found or has expired; prepare it again."
                )
            return draft

    def claim(
        self, draft_id: str, selected_ids: Iterable[str]
    ) -> tuple[StoredIntakeDraft, list[str]]:
        selected = list(dict.fromkeys(str(value).strip() for value in selected_ids))
        if not selected or any(not value for value in selected):
            raise IntakeFlowError("Select at least one prepared item to send to Review.")
        with self._lock:
            now = time.monotonic()
            self._purge_locked(now)
            draft = self._drafts.get(draft_id)
            if draft is None:
                raise IntakeFlowError(
                    "Intake draft was not found or has expired; prepare it again."
                )
            by_id = {
                candidate.candidate_id: candidate
                for candidate in draft.preparation.candidates
            }
            unknown = [value for value in selected if value not in by_id]
            if unknown:
                raise IntakeFlowError(
                    "Draft selection contains unknown item(s): " + ", ".join(unknown)
                )
            unavailable = [
                value
                for value in selected
                if value in draft.submitted_ids or value in draft.inflight_ids
            ]
            if unavailable:
                raise IntakeFlowError(
                    "Draft item(s) were already submitted or are being submitted: "
                    + ", ".join(unavailable)
                )
            ineligible = [value for value in selected if not by_id[value].eligible_for_review]
            if ineligible:
                raise IntakeFlowError(
                    "Only QA-passing, non-duplicate items can be submitted: "
                    + ", ".join(ineligible)
                )
            draft.inflight_ids.update(selected)
            return draft, selected

    def finish(
        self,
        draft_id: str,
        claimed_ids: Iterable[str],
        submitted_ids: Iterable[str],
    ) -> None:
        with self._lock:
            draft = self._drafts.get(draft_id)
            if draft is None:
                return
            claimed = set(claimed_ids)
            draft.inflight_ids.difference_update(claimed)
            draft.submitted_ids.update(submitted_ids)

    def clear(self) -> None:
        """Test/support hook; production expiry is automatic."""
        with self._lock:
            self._drafts.clear()


INTAKE_DRAFTS = IntakeDraftStore()


def _candidate_issues(candidate: IntakeCandidate) -> list[str]:
    issues = list(candidate.validation_issues)
    for round_ in candidate.qa_history:
        issues.extend(round_.issues)
    return list(dict.fromkeys(issue for issue in issues if issue))


def _exact_duplicate(candidate: IntakeCandidate) -> bool:
    return any(match.match_type == "exact" for match in candidate.duplicate_matches)


def _candidate_disposition(candidate: IntakeCandidate) -> str:
    if _exact_duplicate(candidate):
        return "already_known"
    if candidate.eligible_for_review:
        return "ready"
    return "needs_attention"


def _serialize_candidate(
    candidate: IntakeCandidate,
    *,
    evidence_payload: dict[str, Any] | None,
    submitted: bool,
) -> dict[str, Any]:
    duplicate = candidate.duplicate_matches[0] if candidate.duplicate_matches else None
    summary = (
        str(evidence_payload.get("independently_authored_summary") or "")
        if evidence_payload
        else candidate.text
    )
    title = (
        str(evidence_payload.get("headline") or "")
        if evidence_payload
        else candidate.title
    )
    sources = [
        {
            "url": url,
            # A literal, public-shaped URL is admitted provenance, not a
            # reachability/credibility/factual-support determination.
            "status": "public_shape",
            "detail": "Literal public URL; reachability and support require human review.",
        }
        for url in candidate.source_urls
    ]
    return {
        "id": candidate.candidate_id,
        "disposition": _candidate_disposition(candidate),
        "kind": candidate.kind,
        "title": title or "Untitled prepared item",
        "summary": summary,
        "selected": candidate.eligible_for_review and not submitted,
        "submitted": submitted,
        "sources": sources,
        "qa": {
            "status": candidate.qa_status,
            "issues": _candidate_issues(candidate),
            "iterations": len(candidate.qa_history),
        },
        "duplicate": (
            {
                "status": "exact" if duplicate.match_type == "exact" else "possible",
                "similarity": duplicate.similarity,
                "record_id": duplicate.record_id,
                "title": duplicate.title,
                "reasons": list(duplicate.reasons),
            }
            if duplicate
            else None
        ),
        "why": candidate.classification_reason,
        "warnings": list(candidate.warnings),
    }


def serialize_draft(draft: StoredIntakeDraft) -> dict[str, Any]:
    items = [
        _serialize_candidate(
            candidate,
            evidence_payload=draft.evidence_payloads.get(candidate.candidate_id),
            submitted=candidate.candidate_id in draft.submitted_ids,
        )
        for candidate in draft.preparation.candidates
    ]
    counts = {
        name: sum(item["disposition"] == name for item in items)
        for name in ("ready", "needs_attention", "already_known")
    }
    return {
        "draft_id": draft.draft_id,
        "status": draft.preparation.status,
        "summary": counts,
        "items": items,
        "holds": [
            {"unit_id": hold.unit_id, "reason": hold.reason}
            for hold in draft.preparation.holds
        ],
        "accounting": [
            {
                "unit_id": item.unit_id,
                "disposition": item.disposition,
                "candidate_ids": list(item.candidate_ids),
                "reason": item.reason,
            }
            for item in draft.preparation.accounting
        ],
        "errors": list(draft.preparation.errors),
        "source_check_note": (
            "URLs were required to appear literally in the intake and pass public-URL "
            "shape checks. Reachability, rights, and claim support remain Review gates."
        ),
        "expires_in_seconds": DRAFT_TTL_SECONDS,
    }


def _corpus_source_urls(path: str | Path) -> list[str]:
    try:
        events = list_evidence_events(str(path))
    except (OSError, sqlite3.Error):
        return []
    return [
        str(event.get("source_url") or "")
        for event in events
        if event.get("source_url")
    ]


def _events_by_url(path: str | Path) -> dict[str, dict[str, Any]]:
    by_url: dict[str, dict[str, Any]] = {}
    for event in list_evidence_events(str(path)):
        source_url = str(event.get("source_url") or "")
        if not source_url:
            continue
        for variant in url_dedup_variants(source_url):
            by_url.setdefault(variant, event)
    return by_url


def _recompute_preparation_status(preparation: IntakePreparation) -> None:
    has_holds = bool(preparation.holds) or any(
        candidate.qa_status == "hold" for candidate in preparation.candidates
    )
    preparation.status = "ready_with_holds" if has_holds else "ready"


def prepare_research_material(
    research_material: str,
    *,
    settings: Settings,
    client: Any,
    store: IntakeDraftStore = INTAKE_DRAFTS,
    include_singularity_atlas: bool = False,
    singularity_fetch: Any = None,
) -> dict[str, Any]:
    """Run the bounded graph and retain its ready-to-queue draft records."""

    pull = collect_relevant_stories(
        include=include_singularity_atlas,
        workspace=settings.workspace,
        corpus_urls=_corpus_source_urls(settings.evidence_db_path),
        fetch_stories=singularity_fetch,
    )
    combined = compose_material(research_material, pull)
    if not combined.strip():
        if include_singularity_atlas and pull.status == "unavailable":
            raise ValueError(
                "No pasted material, and Singularity Atlas is unavailable: "
                + pull.probe.reason
            )
        if include_singularity_atlas:
            raise ValueError(
                "No pasted material, and Singularity Atlas returned no eligible "
                "unseen public-feed stories."
            )
        raise ValueError("research_material must be a non-empty string")

    preparation = prepare_intake(
        combined,
        extractor=make_llm_extractor(client),
        judge=make_llm_judge(client),
        repairer=make_llm_repairer(client),
        evidence_db_path=settings.evidence_db_path,
        emerging_risks_path=settings.emerging_risks_path,
        max_iterations=min(5, max(1, settings.max_judge_iterations)),
    )
    evidence_payloads = {
        candidate.candidate_id: dict(candidate.record)
        for candidate in preparation.candidates
        if candidate.kind == "evidence" and candidate.eligible_for_review
    }

    # A risk must cite governed evidence.  It may point to an existing event
    # or to a prepared evidence candidate that will be selected in the same
    # confirmation.  Merely possessing a URL is not enough.
    existing_by_url = _events_by_url(settings.evidence_db_path)
    prepared_urls = {
        str(payload.get("source_url") or "")
        for payload in evidence_payloads.values()
        if payload.get("source_url")
    }
    for candidate in preparation.candidates:
        if candidate.kind != "risk" or not candidate.eligible_for_review:
            continue
        if not candidate.title:
            candidate.qa_status = "hold"
            candidate.validation_issues.append("A risk candidate requires an issue title.")
            continue
        unresolved = [
            url
            for url in candidate.source_urls
            if url not in existing_by_url and url not in prepared_urls
        ]
        if unresolved:
            candidate.qa_status = "hold"
            candidate.validation_issues.append(
                "Risk source has no existing or prepared evidence record: "
                + ", ".join(unresolved)
            )

    _recompute_preparation_status(preparation)
    draft = store.create(
        preparation,
        evidence_payloads,
        singularity_stories=[item.to_dict() for item in pull.selected],
    )
    payload = serialize_draft(draft)
    if include_singularity_atlas:
        record_source_job(
            pull, workspace=settings.workspace, draft_id=draft.draft_id
        )
        payload["singularity_atlas"] = pull.to_dict()
    return payload


def _risk_record(candidate: IntakeCandidate, evidence_refs: list[str]) -> dict[str, Any]:
    record = dict(candidate.record)
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    record.update(
        {
            "issue": candidate.title[:240],
            "description": candidate.text[:4000],
            "evidence_refs": evidence_refs,
            "review_status": "pending",
        }
    )
    record.setdefault("first_seen", now)
    record.setdefault("last_reviewed", now)
    record.setdefault("first_observed", now)
    record.setdefault("last_confirmed", now)
    record.setdefault("source_type", "public_research")
    record.setdefault(
        "provenance_note",
        "Model-assisted hypothesis prepared from the linked public evidence; "
        "pending independent human review.",
    )
    return normalize_risk_record(record)


def submit_draft_to_review(
    draft_id: str,
    selected_ids: Iterable[str],
    *,
    settings: Settings,
    store: IntakeDraftStore = INTAKE_DRAFTS,
) -> dict[str, Any]:
    """Persist selected draft items as pending records, with partial success."""

    draft, claimed = store.claim(draft_id, selected_ids)
    by_id = {
        candidate.candidate_id: candidate
        for candidate in draft.preparation.candidates
    }
    selected = [by_id[candidate_id] for candidate_id in claimed]
    queued: list[dict[str, str]] = []
    skipped_duplicates: list[dict[str, str]] = []
    held: list[dict[str, str]] = []
    submitted: set[str] = set()

    try:
        for candidate in selected:
            if candidate.kind != "evidence":
                continue
            payload = draft.evidence_payloads.get(candidate.candidate_id)
            if payload is None:
                held.append({
                    "id": candidate.candidate_id,
                    "reason": "Prepared evidence payload is unavailable.",
                })
                continue
            report = validate(
                [evidence_event_record(payload)], seam="web_intake_submit"
            )
            if report.rejected_ids:
                held.append({
                    "id": candidate.candidate_id,
                    "reason": "Ontology guard rejected the prepared evidence record.",
                })
                continue
            added = add_evidence_event(str(settings.evidence_db_path), payload)
            submitted.add(candidate.candidate_id)
            if added:
                queued.append({
                    "id": candidate.candidate_id,
                    "kind": "evidence",
                    "record_id": str(payload["event_id"]),
                })
            else:
                skipped_duplicates.append({
                    "id": candidate.candidate_id,
                    "kind": "evidence",
                    "reason": "Evidence source already exists in the corpus.",
                })

        events_by_url = _events_by_url(settings.evidence_db_path)
        risk_candidates = [candidate for candidate in selected if candidate.kind == "risk"]
        if risk_candidates:
            with register_lock(settings.emerging_risks_path, timeout=30.0):
                risks = load_emerging_risks(settings.emerging_risks_path)
                pending_risk_items: list[dict[str, str]] = []
                known = {
                    str(risk.get("issue") or "").casefold()
                    for risk in risks
                    if isinstance(risk, dict)
                }
                for candidate in risk_candidates:
                    evidence_refs: list[str] = []
                    unresolved: list[str] = []
                    for source_url in candidate.source_urls:
                        event = events_by_url.get(source_url)
                        event_id = str((event or {}).get("event_id") or "")
                        if event_id:
                            if event_id not in evidence_refs:
                                evidence_refs.append(event_id)
                        else:
                            unresolved.append(source_url)
                    if unresolved:
                        held.append({
                            "id": candidate.candidate_id,
                            "reason": (
                                "Select its supporting evidence or review the existing "
                                "source first: " + ", ".join(unresolved)
                            ),
                        })
                        continue
                    if candidate.title.casefold() in known:
                        submitted.add(candidate.candidate_id)
                        skipped_duplicates.append({
                            "id": candidate.candidate_id,
                            "kind": "risk",
                            "reason": "A risk with the same issue already exists.",
                        })
                        continue
                    record = _risk_record(candidate, evidence_refs)
                    risks.append(record)
                    known.add(candidate.title.casefold())
                    pending_risk_items.append({
                        "id": candidate.candidate_id,
                        "kind": "risk",
                        "record_id": candidate.title,
                    })
                if pending_risk_items:
                    save_emerging_risks(settings.emerging_risks_path, risks)
                    queued.extend(pending_risk_items)
                    submitted.update(item["id"] for item in pending_risk_items)
    finally:
        store.finish(draft_id, claimed, submitted)

    if queued and draft.singularity_stories:
        record_submitted_stories(
            draft.singularity_stories,
            workspace=settings.workspace,
            draft_id=draft_id,
        )

    return {
        "draft_id": draft_id,
        "queued": len(queued),
        "submitted": {
            "evidence": sum(item["kind"] == "evidence" for item in queued),
            "risks": sum(item["kind"] == "risk" for item in queued),
        },
        "queued_items": queued,
        "skipped_duplicates": skipped_duplicates,
        "held": held,
        "review_url": "#review",
    }


__all__ = [
    "DRAFT_TTL_SECONDS",
    "INTAKE_DRAFTS",
    "IntakeDraftStore",
    "IntakeFlowError",
    "prepare_research_material",
    "serialize_draft",
    "submit_draft_to_review",
]
