"""Compound graduation — expand co-reported signal hypotheses into full
scenarios in the scenario library (Phase 8).

The user-facing rule: a compound with held-out signal support graduates
automatically — no human promotion step. But the eligibility decision is
**deterministic**, and the LLM only writes the scenario document; it
never decides what graduates and never touches the numbers.

Eligibility (all mechanical, recorded per compound):

1. **Promoted** — present in the ``compound_scenarios`` table (the monthly
   join's top-N output).
2. **Holdout survival** — the risk pair co-occurred at least once in the
   holdout quarters *after* the scoring window (the signal series the join
   itself used). This is reporting co-occurrence, not proof of operational
   co-materialization, causality, or calibrated event probability.
3. **Not previously graduated** (idempotent).

Graduation per eligible compound:

* the LLM drafts a full scenario document (title, transmission, options,
  signposts — the legacy field set) from the compound's *structural*
  context: atom titles, channels, and the stored score vector;
* the QA judge (same judge as Phase 3) must pass it at threshold;
* the score fields (p̂ band, q95 band, uncertainty class) are inserted
  from the stored realization, never from the LLM;
* the scenario row carries compound provenance (compound_id, state_hash,
  origin) so the library always shows where it came from.

Ledger: ``compound_graduations`` (one row per compound, ever).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Settings, get_settings
from .db import upsert_scenario
from .judge import judge_scenario
from .scoring import ALGORITHM_VERSION
from .scoring.r1 import build_signal_series
from .scoring.models import pair_id_for

logger = logging.getLogger(__name__)

HOLDOUT_QUARTERS = 2
JUDGE_THRESHOLD = 0.75
ORIGIN_TAG = "compound_graduation"


@dataclass
class GraduationResult:
    status: str  # ok | none_eligible | error
    run_id: str
    n_candidates: int = 0
    n_eligible: int = 0
    n_graduated: int = 0
    n_judge_rejected: int = 0
    graduated: list[str] = field(default_factory=list)
    note: str = ""
    errors: list[str] = field(default_factory=list)


def migrate_graduations(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS compound_graduations (
                compound_id TEXT PRIMARY KEY,
                scenario_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                eligibility TEXT NOT NULL,
                judge_score REAL
            )
        """)
        columns = {r[1] for r in conn.execute("PRAGMA table_info(compound_graduations)")}
        if "pair_id" not in columns:
            conn.execute("ALTER TABLE compound_graduations ADD COLUMN pair_id TEXT")
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'compound_scenarios'"
        ).fetchone():
            for compound_id, risk_ids in conn.execute(
                "SELECT g.compound_id, c.risk_ids FROM compound_graduations g "
                "JOIN compound_scenarios c ON c.compound_id = g.compound_id "
                "WHERE g.pair_id IS NULL"
            ).fetchall():
                members = json.loads(risk_ids)
                conn.execute(
                    "UPDATE compound_graduations SET pair_id = ? WHERE compound_id = ?",
                    (pair_id_for(members), compound_id),
                )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_compound_graduations_pair "
            "ON compound_graduations(pair_id)"
        )
        conn.commit()
    finally:
        conn.close()


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _holdout_fired_sets(
    settings: Settings, holdout_quarters: int = HOLDOUT_QUARTERS
) -> tuple[dict[int, set[str]], int]:
    """Per-holdout-quarter sets of atoms that fired in the signal series.

    k-agnostic: a k-way compound survives holdout iff its full risk_id set
    is a subset of some holdout quarter's fired set (all_must_fire).
    """
    atoms, events, _ = build_signal_series(
        settings.emerging_risks_path, settings.evidence_db_path
    )
    if not events:
        return {}, 0
    n_periods = max(e.period for e in events) + 1
    holdout_start = n_periods - holdout_quarters
    by_period: dict[int, set[str]] = {}
    for e in events:
        if e.period >= holdout_start:
            by_period.setdefault(e.period, set()).add(e.risk_id)
    return by_period, n_periods


def _holdout_cooccurring_pairs(
    settings: Settings, holdout_quarters: int = HOLDOUT_QUARTERS
) -> tuple[set[tuple[str, str]], int]:
    """Pairs that co-occurred in the holdout window of the signal series."""
    by_period, n_periods = _holdout_fired_sets(settings, holdout_quarters)
    pairs: set[tuple[str, str]] = set()
    for fired in by_period.values():
        for a in fired:
            for b in fired:
                if a < b:
                    pairs.add((a, b))
    return pairs, n_periods


def eligible_compounds(settings: Settings) -> tuple[list[dict[str, Any]], int]:
    """Compound rows that are promoted AND survived holdout AND are new.

    Holdout survival is *computed* for any k >= 2: the combo survives iff
    all k atoms co-fired in a single holdout quarter. Rows with k < 2 are
    degenerate and skipped. Each eligible row carries an
    ``eligibility_evidence`` blob so the graduation attestation records the
    computed survival, never a hardcoded ``True`` (A7/D12).
    """
    db_path = str(settings.scenario_db_path)
    migrate_graduations(db_path)
    fired_by_period, _ = _holdout_fired_sets(settings)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM compound_scenarios WHERE status = 'candidate' "
            "ORDER BY scored_at DESC, compound_id"
        ).fetchall()
        graduated = {
            r[0] for r in conn.execute("SELECT compound_id FROM compound_graduations")
        }
        graduated_pairs = {
            r[0] for r in conn.execute(
                "SELECT pair_id FROM compound_graduations WHERE pair_id IS NOT NULL"
            )
        }
    finally:
        conn.close()
    eligible = []
    selected_pairs: set[str] = set()
    for row in rows:
        risk_ids = sorted(json.loads(row["risk_ids"]))
        pair_id = pair_id_for(risk_ids)
        if (row["compound_id"] in graduated or pair_id in graduated_pairs
                or pair_id in selected_pairs):
            continue
        if len(risk_ids) < 2:
            logger.warning(
                "skipping degenerate compound %s: %d risk id(s)",
                row["compound_id"], len(risk_ids),
            )
            continue
        matching = sorted(
            p for p, fired in fired_by_period.items() if set(risk_ids) <= fired
        )
        if not matching:
            continue
        record = dict(row)
        record["pair_id"] = pair_id
        record["eligibility_evidence"] = {
            "promoted": True,
            "holdout_survival": True,
            "holdout_matching_quarters": matching,
            "holdout_window_quarters": HOLDOUT_QUARTERS,
            "k": len(risk_ids),
            "pair_id": pair_id,
            "observation_scope": "public_signal_co_activation",
            "causal_validation": "not_established",
        }
        eligible.append(record)
        selected_pairs.add(pair_id)
    return eligible, len(rows)


PROMPT_VERSION = "compound-graduation-v2"

_GRADUATE_SYSTEM = """You are a scenario writer for a banking operational-risk foresight
library. You receive a co-reported signal hypothesis (risks with linked
public reports in the same held-out quarter, with an auditable research score) and expand it
into a full scenario document. Stay strictly within the supplied facts:
no invented institutions, dates, CVEs, or loss figures. The score fields
are provided separately — do not restate or alter them. Reporting co-occurrence
does not establish operational co-materialization or a causal mechanism.
Describe possible mechanisms as hypotheses and state what evidence would
validate or refute them. Return JSON only."""

_GRADUATE_FIELDS = [
    "title", "description", "risk_category", "transmission_mechanism",
    "second_order_effects", "affected_capabilities", "defensive_options",
    "resilience_opportunities", "strategic_options", "preparation_strategy",
    "strengthening_signposts", "weakening_signposts", "discussion_prompts",
    "detailed_rationale", "element_of_surprise",
]


def _build_graduation_prompt(compound: dict[str, Any], atom_titles: dict[str, str]) -> list[dict[str, str]]:
    risk_ids = json.loads(compound["risk_ids"])
    titles = [atom_titles.get(r, r) for r in risk_ids]
    user = f"""Compound signal hypothesis (co-reporting support only):
- member risks: {" + ".join(titles)}
- stored score (do not restate or alter): p_hat band and loss quantiles are
  recorded separately
- uncertainty class: {compound.get("uncertainty_class")}
- dependence support: {compound.get("dependence_support")} co-occurring quarters

Write the scenario document as JSON with exactly these keys:
{json.dumps(_GRADUATE_FIELDS)}

Requirements:
- title: compound, specific, no hype
- description: 3-5 sentences, mechanism-first
- transmission_mechanism: a possible coupling to investigate, explicitly unverified
- all other fields: 1-3 sentences each, concrete and falsifiable
- detailed_rationale: cite the dependence support and uncertainty class as given
- JSON only, no markdown fences"""
    return [
        {"role": "system", "content": _GRADUATE_SYSTEM},
        {"role": "user", "content": user},
    ]


def _load_risk_records(settings: Settings) -> dict[str, dict[str, Any]]:
    """risk_id -> raw register record (same slug/dedup logic as the scorer)."""
    try:
        raw = json.loads(Path(settings.emerging_risks_path).read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    from .scoring.inventory import slugify

    records: dict[str, dict[str, Any]] = {}
    seen: dict[str, int] = {}
    for item in raw:
        title = item.get("issue") or item.get("title") or ""
        base = slugify(title)
        n = seen.get(base, 0)
        seen[base] = n + 1
        # Persisted risk_id wins, as in scoring.inventory.load_risk_atoms.
        rid = str(item.get("risk_id") or "").strip() or (
            base if n == 0 else f"{base}-{n + 1}"
        )
        records[rid] = item
    return records


def _load_atom_titles(settings: Settings) -> dict[str, str]:
    records = _load_risk_records(settings)
    return {
        rid: (rec.get("issue") or rec.get("title") or "")
        for rid, rec in records.items()
    }


def _derive_compound_metadata(records: list[dict[str, Any]]) -> dict[str, str]:
    """Graduation display metadata, *derived or omitted* (A7/D8) — never
    hardcoded. Rules, applied per field:

    * ``evidence_strength``: derived from the union of the constituents'
      evidence references (same deterministic citation-count rule the
      register uses).
    * ``momentum`` / ``signal_maturity``: carried over only when every
      constituent that states a value agrees; disagreement or absence
      omits the key rather than inventing a claim.
    """
    from .categories import (
        derive_evidence_strength,
        normalize_momentum,
        normalize_signal_maturity,
    )

    meta: dict[str, str] = {}
    refs = sorted({r for rec in records for r in rec.get("evidence_refs", [])})
    meta["evidence_strength"] = derive_evidence_strength(refs)

    momenta = {
        normalize_momentum(rec.get("momentum"), legacy_trend=rec.get("trend"))
        for rec in records
        if rec.get("momentum") or rec.get("trend")
    }
    if len(momenta) == 1:
        meta["momentum"] = next(iter(momenta))

    maturities = {
        normalize_signal_maturity(rec["signal_maturity"])
        for rec in records
        if rec.get("signal_maturity")
    }
    if len(maturities) == 1:
        meta["signal_maturity"] = next(iter(maturities))

    return meta


def _likelihood_band(p_hat: float) -> str:
    """Map a compound p̂ onto the closed scenario likelihood scale.

    C-01: the ``likelihood`` column carries the band; the raw estimate lives
    in ``additional_information``. Serializing stats into the enum field (the
    pre-guard behavior) broke every downstream parser.
    """
    if p_hat >= 0.8:
        return "Very High"
    if p_hat >= 0.6:
        return "High"
    if p_hat >= 0.4:
        return "Moderate"
    if p_hat >= 0.2:
        return "Low"
    return "Very Low"


def _financial_band_for_loss(loss_usd: float) -> str:
    """Band a stored q95 loss per the docs/reference/TAXONOMY.md parser thresholds
    (Extreme ≥ $10B, Major ≥ $1B, Significant ≥ $100M, Moderate ≥ $10M,
    else Limited). C-01: the band field carries the band, not a stats string."""
    if loss_usd >= 1e10:
        return "Extreme"
    if loss_usd >= 1e9:
        return "Major"
    if loss_usd >= 1e8:
        return "Significant"
    if loss_usd >= 1e7:
        return "Moderate"
    return "Limited"


def run_compound_graduation(
    settings: Settings | None = None,
    client=None,
    *,
    judge_threshold: float = JUDGE_THRESHOLD,
) -> GraduationResult:
    """Expand supported signal hypotheses into the scenario library (LLM-drafted,
    judge-gated, numbers from the stored realization only)."""
    settings = settings or get_settings()
    db_path = str(settings.scenario_db_path)
    migrate_graduations(db_path)
    run_id = f"grad-{uuid.uuid4().hex[:8]}"
    result = GraduationResult(status="error", run_id=run_id)
    if client is None:
        result.note = "no LLM client (phase requires OMLX)"
        result.errors.append(result.note)
        return result

    try:
        eligible, n_candidates = eligible_compounds(settings)
        result.n_candidates = n_candidates
        result.n_eligible = len(eligible)
        if not eligible:
            result.status = "none_eligible"
            result.note = "no promoted compounds survived holdout (or none new)"
            return result

        atom_titles = _load_atom_titles(settings)
        risk_records = _load_risk_records(settings)
        conn = sqlite3.connect(db_path)
        try:
            for compound in eligible:
                risk_ids = sorted(json.loads(compound["risk_ids"]))
                # attestation written by the checker, not the writer: the
                # survival verdict and its evidence come from
                # eligible_compounds, never hardcoded (A7/D12)
                eligibility = {
                    **compound.get("eligibility_evidence", {}),
                    "p_hat": compound["p_hat"],
                    "p_interval": [compound["p_lo"], compound["p_hi"]],
                    "uncertainty_class": compound["uncertainty_class"],
                    "dependence_support": compound["dependence_support"],
                }
                # 1) LLM drafts the document
                messages = _build_graduation_prompt(compound, atom_titles)
                try:
                    doc = client.chat_json(messages, temperature=0.2)
                except Exception as exc:
                    result.errors.append(f"{compound['compound_id']}: draft failed: {exc}")
                    continue
                if not isinstance(doc, dict):
                    result.errors.append(f"{compound['compound_id']}: draft not a JSON object")
                    continue

                # 2) numbers come from the stored realization, never the LLM
                scenario_id = f"ATLAS-CMP-{compound['compound_id'].replace('scn-', '')[:10].upper()}"
                titles = [atom_titles.get(r, r) for r in risk_ids]
                scenario: dict[str, Any] = {
                    "scenario_id": scenario_id,
                    "origin": ORIGIN_TAG,
                    "status": "active",
                    "created_year": datetime.now(timezone.utc).year,
                    # display metadata derived from the constituent risk
                    # records (or omitted on disagreement) — never hardcoded
                    **_derive_compound_metadata(
                        [risk_records.get(r, {}) for r in risk_ids]
                    ),
                    "core_risks": "; ".join(titles),
                    "theme_tags": json.dumps(["compound", "co-occurrence", ORIGIN_TAG]),
                    # Public-report co-activation is not operational event
                    # likelihood. Preserve its estimate in scoped metadata.
                    "likelihood": None,
                    "additional_information": json.dumps(
                        {
                            "origin": ORIGIN_TAG,
                            "compound_id": compound["compound_id"],
                            "state_hash": compound["state_hash"],
                            "algorithm_version": compound.get("algorithm_version") or ALGORITHM_VERSION,
                            "run_id": compound.get("run_id"),
                            "p_hat": compound["p_hat"],
                            "p_interval": [compound["p_lo"], compound["p_hi"]],
                            "pair_id": compound["pair_id"],
                            "observation_scope": "public_signal_co_activation",
                            "signal_co_activation_band": _likelihood_band(compound["p_hat"]),
                            "calibration_status": "not_validated_for_enterprise_use",
                        }
                    ),
                }
                # regime-aware loss reporting (A7): activation regimes carry
                # no loss semantics — the band is omitted, never rendered as
                # a dollar figure built from placeholder 1.0 amounts
                if compound.get("loss_q95") is not None:
                    scenario["financial_impact_band"] = _financial_band_for_loss(
                        compound["loss_q95"]
                    )
                for field_name in _GRADUATE_FIELDS:
                    if field_name in doc and doc[field_name]:
                        scenario[field_name] = str(doc[field_name])
                scenario.setdefault("title", f"Compound: {' + '.join(titles)}")

                # 3) QA judge (same gate as phase 3)
                verdict = judge_scenario(scenario, client, threshold=judge_threshold)
                if verdict.error:
                    result.errors.append(
                        f"{compound['compound_id']}: judge error: {verdict.feedback}"
                    )
                    continue
                if not verdict.passed:
                    result.n_judge_rejected += 1
                    continue

                # 4) insert + ledger
                scenario["judge_score"] = verdict.score
                scenario["judge_feedback"] = verdict.feedback
                upsert_scenario(db_path, scenario)
                conn.execute(
                    "INSERT OR REPLACE INTO compound_graduations "
                    "(compound_id, scenario_id, run_id, created_at, eligibility, judge_score, pair_id) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (
                        compound["compound_id"], scenario_id, run_id,
                        _utcnow(), json.dumps(eligibility, sort_keys=True),
                        verdict.score, compound["pair_id"],
                    ),
                )
                conn.commit()
                result.n_graduated += 1
                result.graduated.append(scenario_id)
        finally:
            conn.close()

        result.status = "ok" if result.n_graduated else "none_eligible"
        result.note = (
            f"{result.n_graduated} graduated, {result.n_judge_rejected} judge-rejected, "
            f"{len(result.errors)} errors of {result.n_eligible} eligible"
        )
        return result
    except Exception as exc:
        logger.exception("compound graduation failed")
        result.status = "error"
        result.errors.append(str(exc))
        result.note = str(exc)
        return result
