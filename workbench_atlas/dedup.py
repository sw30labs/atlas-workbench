"""P8 semantic atom-identity hygiene: duplicate ranking and register merge.

Identity error becomes dependence signal: duplicate atoms under different
names convert shared evidence into fake co-occurrence — the exact quantity
the compound scorer sells (HYBRID_SCORING_PROPOSAL.md, P8). Register
identity today is exact/slugified string matching; this module adds a
deterministic candidate ranker plus the single sanctioned merge
application path.

Discipline:

* :func:`candidate_duplicates` is stdlib-only and deterministic —
  normalized token-Jaccard over title and description, no embeddings, no
  LLM, and no hardcoded demo phrases (the legacy scenario dedup with
  baked-in phrases is deliberately not reused).
* :func:`apply_merge` is the ONLY function that mutates the register for
  a merge. The whole load -> modify -> save span runs under
  ``review.register_lock``, writes with the same tmp+rename atomic
  discipline as ``phases.phase2_risks.save_emerging_risks``, and stamps
  provenance on both records. It is invoked by the human review decision
  ``review.approve_merge`` (ADR-013 pattern, ratified by ADR-014) —
  never by machine proposal paths.
* Merging is idempotent: re-applying an already-recorded merge is a
  recorded no-op, not an error and not a second mutation.
"""

from __future__ import annotations

import copy
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .phases.phase2_risks import load_emerging_risks, save_emerging_risks
from .scoring.inventory import slugify


class MergeError(ValueError):
    """Raised when a requested register merge is invalid or unsafe.

    ``review.approve_merge`` converts these into ``ReviewError`` so the
    dashboard's decision endpoint maps them like any other refused
    review action.
    """


#: Generic English stopwords only — similarity must react to domain
#: tokens, never to baked-in demo vocabulary (P8).
_STOPWORDS = frozenset(
    "a an the and or of in on for to with by from at as is are was were "
    "be been over under across amid due via per within without into "
    "risk risks threat threats issue issues".split()
)

_TOKEN_RE = re.compile(r"[a-z0-9]+")

#: Blend weights for the combined similarity: titles carry identity,
#: descriptions carry mechanism/scope context. Fixed constants so the
#: ranking is reproducible and reviewable.
_TITLE_WEIGHT = 0.6
_TEXT_WEIGHT = 0.4

_MAX_SHARED_TOKENS = 12


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _text(value: object) -> str:
    return str(value).strip() if value is not None else ""


def _tokens(text: str) -> set[str]:
    """Normalized token set: lowercase alphanumerics, stopwords dropped."""
    return {
        tok
        for tok in _TOKEN_RE.findall(text.lower())
        if len(tok) >= 2 and tok not in _STOPWORDS
    }


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _title_of(record: dict[str, Any]) -> str:
    return _text(record.get("issue") or record.get("title"))


def _identify(risks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Assign risk_ids exactly like ``scoring.inventory.load_risk_atoms``:
    slugified title, with a ``-2``/``-3`` suffix on repeated slugs."""
    entries: list[dict[str, Any]] = []
    seen: dict[str, int] = {}
    for index, record in enumerate(risks):
        title = _title_of(record)
        base = slugify(title or "untitled-risk")
        n = seen.get(base, 0)
        seen[base] = n + 1
        entries.append(
            {
                "index": index,
                "record": record,
                "risk_id": base if n == 0 else f"{base}-{n + 1}",
                "title": title,
                "title_tokens": _tokens(title),
                "text_tokens": _tokens(
                    f"{title} {_text(record.get('description'))}"
                ),
            }
        )
    return entries


def candidate_duplicates(
    risks: list[dict], *, threshold: float = 0.6
) -> list[dict]:
    """Rank candidate duplicate pairs in the risk register.

    Deterministic, stdlib-only: token-Jaccard on normalized titles plus
    token-Jaccard on title+description text, blended with fixed weights.
    Records already retired by a merge (``merged_into`` set) are skipped —
    they are audit pointers, not live atoms. Returns pairs sorted by
    descending similarity (ties broken by risk_id for reproducibility),
    each with the similarity components so a reviewer can see *why* the
    pair was proposed. No self-pairs; ``threshold`` is inclusive.
    """
    entries = [
        e
        for e in _identify(risks)
        if isinstance(e["record"], dict) and not _text(e["record"].get("merged_into"))
    ]
    pairs: list[dict[str, Any]] = []
    for i in range(len(entries)):
        for j in range(i + 1, len(entries)):
            a, b = entries[i], entries[j]
            title_j = _jaccard(a["title_tokens"], b["title_tokens"])
            text_j = _jaccard(a["text_tokens"], b["text_tokens"])
            similarity = _TITLE_WEIGHT * title_j + _TEXT_WEIGHT * text_j
            if similarity < threshold or similarity <= 0.0:
                continue
            shared = sorted(a["text_tokens"] & b["text_tokens"])
            pairs.append(
                {
                    "risk_id_a": a["risk_id"],
                    "risk_id_b": b["risk_id"],
                    "title_a": a["title"],
                    "title_b": b["title"],
                    "similarity": round(similarity, 6),
                    "components": {
                        "title_jaccard": round(title_j, 6),
                        "text_jaccard": round(text_j, 6),
                        "title_weight": _TITLE_WEIGHT,
                        "text_weight": _TEXT_WEIGHT,
                        "shared_tokens": shared[:_MAX_SHARED_TOKENS],
                    },
                }
            )
    pairs.sort(
        key=lambda p: (-p["similarity"], p["risk_id_a"], p["risk_id_b"])
    )
    return pairs


def _resolve(
    entries: list[dict[str, Any]], id_or_title: str, role: str
) -> dict[str, Any]:
    """Resolve one register record by exact title or by assigned risk_id."""
    wanted = _text(id_or_title)
    matches = [
        e
        for e in entries
        if wanted and wanted in (e["title"], e["risk_id"])
    ]
    if not matches:
        raise MergeError(f"Emerging risk not found for {role} record: {id_or_title}")
    if len(matches) > 1:
        raise MergeError(
            f"{role} record reference is not unique: {id_or_title}"
        )
    return matches[0]


def _earliest(*values: object) -> str:
    """Earliest non-empty ISO date/timestamp (lexicographic = chronological
    for the register's ISO strings; empty means unknown and never wins)."""
    present = [_text(v) for v in values if _text(v)]
    return min(present) if present else ""


def _latest(*values: object) -> str:
    present = [_text(v) for v in values if _text(v)]
    return max(present) if present else ""


def _union_list(first: object, second: object) -> list[str]:
    """Order-preserving union of two string lists (first record's order wins)."""
    out: list[str] = []
    for value in (first, second):
        items = value if isinstance(value, list) else [value]
        for item in items:
            text = _text(item)
            if text and text not in out:
                out.append(text)
    return out


def apply_merge(
    risks_path: str | Path,
    keep_id_or_title: str,
    drop_id_or_title: str,
    *,
    reviewer: str,
    reason: str,
) -> dict:
    """Merge one register record into another, under the register lock.

    The surviving record keeps the union of ``evidence_refs`` (and of
    explicit ``channels`` when present), the earliest ``first_seen`` and
    the latest ``last_reviewed`` of the pair. The retired record stays in
    the register (append-only ethos — nothing is deleted) with
    ``review_status = "superseded"`` and a provenance pointer
    (``merged_into``/``merged_by``/``merged_at`` + reason), which removes
    it from the approved-only loaders on the next join. Both records get
    a provenance stamp naming the reviewer, the reason, and the time.

    Returns a diff dict (before/after of both records). Re-applying the
    same merge is a recorded no-op with ``status = "already_merged"``.
    """
    reviewer = _text(reviewer)
    reason = _text(reason)
    if not reviewer:
        raise MergeError("Register merge requires a reviewer identity")
    if not reason:
        raise MergeError("Register merge requires a written reason")

    # Lazy import: review.py's decision functions call this module, so a
    # top-level import of the lock would be a module cycle.
    from .review import register_lock

    with register_lock(risks_path):
        risks = load_emerging_risks(risks_path)
        entries = _identify(risks)
        keep = _resolve(entries, keep_id_or_title, "keep")
        drop = _resolve(entries, drop_id_or_title, "drop")
        if keep["index"] == drop["index"]:
            raise MergeError("Cannot merge a register record into itself")

        keep_rec: dict[str, Any] = keep["record"]
        drop_rec: dict[str, Any] = drop["record"]
        before_keep = copy.deepcopy(keep_rec)
        before_drop = copy.deepcopy(drop_rec)

        already_into = _text(drop_rec.get("merged_into"))
        if already_into:
            if already_into in (keep["title"], keep["risk_id"]):
                # Recorded no-op: the pair is already merged; the audit
                # trail from the first application stands.
                return {
                    "status": "already_merged",
                    "keep_id": keep["risk_id"],
                    "drop_id": drop["risk_id"],
                    "keep_title": keep["title"],
                    "drop_title": drop["title"],
                    "merged_by": _text(drop_rec.get("merged_by")) or reviewer,
                    "merged_at": _text(drop_rec.get("merged_at")),
                    "reason": _text(drop_rec.get("merge_reason")) or reason,
                    "before": {"keep": before_keep, "drop": before_drop},
                    "after": {
                        "keep": copy.deepcopy(keep_rec),
                        "drop": copy.deepcopy(drop_rec),
                    },
                }
            raise MergeError(
                f"Drop record {drop['title']!r} is already merged into "
                f"{already_into!r}; refusing to re-merge"
            )
        if _text(keep_rec.get("merged_into")):
            raise MergeError(
                f"Keep record {keep['title']!r} is itself a retired "
                "duplicate; pick its surviving record instead"
            )
        for label, record in (("keep", keep_rec), ("drop", drop_rec)):
            if _text(record.get("review_status")) == "approved_synthetic":
                raise MergeError(
                    f"The {label} record is approved synthetic data and is "
                    "immutable through review workflows"
                )

        merged_at = _now()

        # Surviving record: union the evidence (the merge's whole point is
        # that shared evidence was double-counted as co-occurrence), keep
        # the earliest sighting, carry the latest review timestamp.
        keep_rec["evidence_refs"] = _union_list(
            keep_rec.get("evidence_refs"), drop_rec.get("evidence_refs")
        )
        if "channels" in keep_rec or "channels" in drop_rec:
            keep_rec["channels"] = _union_list(
                keep_rec.get("channels"), drop_rec.get("channels")
            )
        keep_rec["first_seen"] = _earliest(
            keep_rec.get("first_seen"), drop_rec.get("first_seen")
        )
        keep_rec["first_observed"] = _earliest(
            keep_rec.get("first_observed"), drop_rec.get("first_observed")
        )
        keep_rec["last_reviewed"] = _latest(
            keep_rec.get("last_reviewed"), drop_rec.get("last_reviewed"),
            merged_at,
        )
        keep_rec["last_confirmed"] = _latest(
            keep_rec.get("last_confirmed"), drop_rec.get("last_confirmed")
        )
        keep_rec["reviewed_at"] = merged_at
        keep_stamp = (
            f"Register merge approved by {reviewer} at {merged_at}: "
            f"absorbed duplicate {drop['title']!r} "
            f"(risk_id {drop['risk_id']!r}). Reason: {reason}"
        )
        keep_rec["provenance_note"] = (
            f"{_text(keep_rec.get('provenance_note'))}\n{keep_stamp}".strip()
        )

        # Retired record: pointer, never deletion.
        drop_stamp = (
            f"Register merge approved by {reviewer} at {merged_at}: retired "
            f"as a duplicate of {keep['title']!r} "
            f"(risk_id {keep['risk_id']!r}). Reason: {reason}"
        )
        drop_rec.update(
            {
                "review_status": "superseded",
                "reviewed_at": merged_at,
                "merged_into": keep["title"],
                "merged_into_risk_id": keep["risk_id"],
                "merged_by": reviewer,
                "merged_at": merged_at,
                "merge_reason": reason,
                "provenance_note": (
                    f"{_text(drop_rec.get('provenance_note'))}\n{drop_stamp}".strip()
                ),
            }
        )

        save_emerging_risks(risks_path, risks)

    return {
        "status": "merged",
        "keep_id": keep["risk_id"],
        "drop_id": drop["risk_id"],
        "keep_title": keep["title"],
        "drop_title": drop["title"],
        "merged_by": reviewer,
        "merged_at": merged_at,
        "reason": reason,
        "before": {"keep": before_keep, "drop": before_drop},
        "after": {
            "keep": copy.deepcopy(keep_rec),
            "drop": copy.deepcopy(drop_rec),
        },
    }
