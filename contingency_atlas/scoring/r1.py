"""R1 regime: public/demo signal-activation series (evaluation-plan.md
section 1, open question 1: pair co-occurrence from "signal active" on R1).

Builds a quarterly activation series by joining the emerging-risk inventory
to the public evidence store through ``evidence_refs`` (provenance-
disciplined: no fuzzy matching, no inferred links). A risk is "active" in a
quarter when at least one linked public evidence item has a source date in
that quarter.

The demo dataset is sparse by construction; :func:`sufficiency` reports
whether Task B can run meaningfully, and :func:`run_r1_eval` returns a
documented "insufficient_data" result instead of a fake pass when it
cannot (roadmap Phase 1.3 allows "clear documented failure"; Phase 3 keeps
the machinery ready for when the public store accumulates).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..provenance import is_approved_record
from ..schema import migrate_evidence_events
from .evaluate import EvalResult, _render_markdown, evaluation_verdicts, run_evaluation
from .inventory import load_risk_atoms, slugify
from .models import IncidentEvent, RiskAtom
from .scorer import ScorerConfig

MIN_BUCKETS = 6
MIN_HOLDOUT_POSITIVES = 3
MIN_ACTIVATIONS = 20
# Below this many holdout co-occurring pairs the Task B metrics are
# directional-only, never a verdict. Aligned with the pre-registered
# power minimum (n >= 30 resolved claims) used by the hybrid-scoring
# program's G-shadow gate (HYBRID_SCORING_PROPOSAL.md, Part C).
SMALL_SAMPLE_MAX_POSITIVES = 30


def _quarter_index(iso_date: str) -> int:
    year, month = int(iso_date[:4]), int(iso_date[5:7])
    return year * 4 + (month - 1) // 3


def build_signal_series(
    risks_path: str | Path, evidence_db_path: str | Path
) -> tuple[list[RiskAtom], list[IncidentEvent], dict[str, Any]]:
    """Join risks -> evidence_refs -> evidence_events.source_date.

    Approved-only on both sides of the join: evidence-event and risk records
    that have not completed human review are excluded, so the compound
    bridge (compound_join -> compound_graduate) can only ever be driven
    by reviewed evidence — the same invariant phases 3/5/6 enforce via
    :func:`contingency_atlas.provenance.is_approved_record`.
    """
    loaded_atoms = load_risk_atoms(risks_path)
    atoms_by_id = {a.risk_id: a for a in loaded_atoms}
    if len(atoms_by_id) != len(loaded_atoms):
        raise ValueError("risk inventory contains duplicate persisted identities")
    migrate_evidence_events(str(evidence_db_path))
    conn = sqlite3.connect(str(evidence_db_path))
    try:
        rows = conn.execute(
            "SELECT event_id, source_date, loss_amount_usd "
            "FROM evidence_events"
            " WHERE review_status IN ('approved', 'approved_synthetic')"
        ).fetchall()
    finally:
        conn.close()
    evidence_by_id = {eid: (date, loss) for eid, date, loss in rows}

    events: list[IncidentEvent] = []
    quarters: set[int] = set()
    linked_risks: set[str] = set()
    n_real_losses = 0
    seq = 0
    # evidence_refs live in the legacy JSON, not on RiskAtom; re-read the
    # raw file for the linkage (provenance-disciplined: exact refs only)
    raw = json.loads(Path(risks_path).read_text())
    # Resolve legacy IDs in original inventory order, exactly as the loader
    # does. Titles are display labels, never approval or evidence join keys.
    atoms: list[RiskAtom] = []
    seen: dict[str, int] = {}
    for item in raw:
        title = item.get("issue") or item.get("title") or "untitled-risk"
        base = slugify(title)
        n = seen.get(base, 0)
        seen[base] = n + 1
        risk_id = str(item.get("risk_id") or "").strip() or (
            base if n == 0 else f"{base}-{n + 1}"
        )
        atom = atoms_by_id.get(risk_id)
        if not is_approved_record(item):
            continue
        if atom is None or atom.title != title or not is_approved_record(atom.to_dict()):
            raise ValueError("risk inventory changed while building the signal series")
        if item.get("status") in {"faded", "retired"} or item.get("merged_into"):
            continue
        atoms.append(atom)
        for ref in dict.fromkeys(item.get("evidence_refs", [])):
            if ref not in evidence_by_id:
                continue
            date, loss = evidence_by_id[ref]
            if not date or len(date) < 7:
                continue
            q = _quarter_index(date)
            quarters.add(q)
            linked_risks.add(risk_id)
            seq += 1
            if loss is not None:
                n_real_losses += 1
            events.append(
                IncidentEvent(
                    event_id=f"R1-{seq:05d}",
                    risk_id=risk_id,
                    period=q,
                    # Occurrence and severity are separate observations.
                    # Unknown monetary amounts must remain missing.
                    loss_amount=float(loss) if loss is not None else None,
                )
            )

    if quarters:
        base = min(quarters)
        for e in events:
            e.period -= base  # re-index from 0
    diagnostics = {
        "n_risks": len(atoms),
        "n_risks_with_activations": len(linked_risks),
        "n_activations": len(events),
        "n_buckets": len(quarters),
        "bucket": "quarter",
        "min_bucket_date": min(
            (
                evidence_by_id[ref][0]
                for ref in evidence_by_id
                if evidence_by_id.get(ref)
            ),
            default=None,
        ),
        "activations_per_risk": round(len(events) / max(len(linked_risks), 1), 2),
        # share of activations backed by a real reported loss amount; 0.0
        # means a pure activation regime with no loss semantics (A7)
        "real_loss_fraction": round(n_real_losses / max(len(events), 1), 4),
    }
    return atoms, events, diagnostics


def sufficiency(diagnostics: dict[str, Any], holdout_quarters: int) -> tuple[bool, list[str]]:
    """Can Task B run meaningfully on this series?"""
    reasons = []
    if diagnostics["n_buckets"] < MIN_BUCKETS:
        reasons.append(
            f"only {diagnostics['n_buckets']} quarters spanned (< {MIN_BUCKETS})"
        )
    if diagnostics["n_buckets"] <= holdout_quarters:
        reasons.append(
            f"holdout of {holdout_quarters} quarters leaves no train window"
        )
    if diagnostics["n_activations"] < MIN_ACTIVATIONS:
        reasons.append(
            f"only {diagnostics['n_activations']} activations (< {MIN_ACTIVATIONS})"
        )
    if diagnostics["n_risks_with_activations"] < 5:
        reasons.append(
            f"only {diagnostics['n_risks_with_activations']} risks have any activation"
        )
    return (not reasons, reasons)


def run_r1_eval(
    risks_path: str | Path,
    evidence_db_path: str | Path,
    *,
    holdout_quarters: int = 2,
    config: ScorerConfig | None = None,
) -> EvalResult:
    """Task B on R1, or a documented insufficiency result."""
    atoms, events, diagnostics = build_signal_series(risks_path, evidence_db_path)
    ok, reasons = sufficiency(diagnostics, holdout_quarters)
    if not ok:
        report = {
            "eval_version": "1.0",
            "data_regime": "R1",
            "status": "insufficient_data",
            "diagnostics": diagnostics,
            "reasons": reasons,
            "interpretation": (
                "The public/demo evidence store does not yet support a "
                "meaningful quarterly signal-activation Task B. This is a "
                "documented data limitation, not a scoring failure: the "
                "machinery is ready and this check reruns as the store grows."
            ),
            "recommendation": "iterate",
            "recommendation_scope": "ranking_and_structure_only",
            "verdicts": evaluation_verdicts(None, None, (config or ScorerConfig()).dependence_mode),
        }
        md = "\n".join(
            [
                "# R1 Task B — insufficient data (documented)",
                "",
                f"- risks: {diagnostics['n_risks']} "
                f"({diagnostics['n_risks_with_activations']} with activations)",
                f"- activations: {diagnostics['n_activations']} over "
                f"{diagnostics['n_buckets']} quarters "
                f"(~{diagnostics['activations_per_risk']} per active risk)",
                "",
                "## Why Task B cannot run",
                "",
            ]
            + [f"- {r}" for r in reasons]
            + [
                "",
                report["interpretation"],
                "",
            ]
        )
        return EvalResult(report=report, report_md=md)

    result = run_evaluation(
        atoms,
        events,
        truth=None,
        holdout_quarters=holdout_quarters,
        config=config,
        regime="R1",
    )
    result.report["diagnostics"] = diagnostics
    n_pos = result.report["task_b"]["n_holdout_positives"]
    if n_pos < SMALL_SAMPLE_MAX_POSITIVES:
        result.report["small_sample"] = True
        result.report["recommendation"] = "iterate"
        result.report["verdicts"]["ranking"]["status"] = "not_validated"
        result.report["verdicts"]["enterprise_readiness"]["status"] = "not_validated"
        result.report["interpretation"] = (
            f"R1 holdout has only {n_pos} co-occurring pairs: the Task B "
            "numbers below are directional at best and must not be read as a "
            "pass/fail verdict. B3 ≡ B1 when no pair co-occurs in train, "
            "which is expected at this data volume."
        )
    result.report_md = _render_markdown(result.report)
    return result
