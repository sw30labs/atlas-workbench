"""Compound join — monthly step that joins emerging risks into scored
compound scenarios (the bridge between the original pipeline and the
redesign scoring engine).

Cadence: runs at the end of the pipeline when at least ``MIN_INTERVAL_DAYS``
(28) have passed since the last successful join, unless forced. Fully
deterministic — no LLM anywhere in this module.

What it does, in order:

1. Due check against the ``compound_join_runs`` ledger.
2. Load the risk inventory (``emerging_risks.json`` -> RiskAtoms).
3. Build the quarterly signal-activation series from the public evidence
   store via exact ``evidence_refs`` linkage.
4. Sufficiency gate: when the public series is too sparse for meaningful
   scoring, the run is recorded as ``insufficient_data`` with the reason
   and NOTHING is fabricated (R1 reality today).
5. Otherwise: fit the deterministic scorer on all-but the last 2 quarters
   and score channel-blocked pairs (the production generation path).
6. Persist the top pairs into the ``compound_scenarios`` table (separate
   from the legacy scenario register — compound scores never map onto
   legacy urgency) and a ``compound_scenarios.json`` artifact next to the
   risk inventory.

Compound p_hat is a research quantity with calibration tracking, not a
capital parameter. Rows carry uncertainty classes; anything
``judgment_dominant`` is workshop material only.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from itertools import combinations
from pathlib import Path
from typing import Any

from .config import Settings, get_settings
from .guard_hook import (
    GuardRejected,
    compound_record,
    guard_enabled,
    run_record,
    validate,
)
from .scoring import ALGORITHM_VERSION
from .scoring.candidates import channel_blocked_pairs
from .scoring.channels import build_channel_graph
from .scoring.models import ChannelGraph, GraphEdge, pair_id_for
from .scoring.r1 import build_signal_series, sufficiency
from .scoring.scorer import DetScorer, ScorerConfig
from .scoring.telemetry import TelemetryLogger

logger = logging.getLogger(__name__)

MIN_INTERVAL_DAYS = 28
TOP_N = 20
HOLDOUT_QUARTERS = 2
COMPOUND_ARTIFACT_NAME = "compound_scenarios.json"
TELEMETRY_PATH = Path("generated/telemetry/compound-join.jsonl")


@dataclass
class CompoundJoinResult:
    status: str  # ok | insufficient_data | skipped_not_due | error
    run_id: str
    n_risks: int = 0
    n_pairs: int = 0
    n_scored: int = 0
    n_written: int = 0
    note: str = ""
    artifact: str | None = None
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Schema (additive, idempotent — same contract as schema.py)
# ---------------------------------------------------------------------------


def migrate_compound(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS compound_scenarios (
                compound_id TEXT PRIMARY KEY,
                risk_ids TEXT NOT NULL,
                state_hash TEXT NOT NULL,
                p_hat REAL,
                p_lo REAL,
                p_hi REAL,
                loss_q50 REAL,
                loss_q95 REAL,
                loss_mean REAL,
                uncertainty_class TEXT,
                dependence_support INTEGER,
                evidence_support REAL,
                data_regime TEXT,
                status TEXT NOT NULL,
                run_id TEXT,
                scored_at TEXT,
                algorithm_version TEXT,
                provenance_note TEXT
            )
        """)
        # A7: stable cross-version identity + fitted-data hash on the
        # current view (additive migration for pre-A7 databases).
        cols = {r[1] for r in conn.execute("PRAGMA table_info(compound_scenarios)")}
        if "pair_id" not in cols:
            conn.execute("ALTER TABLE compound_scenarios ADD COLUMN pair_id TEXT")
        if "data_hash" not in cols:
            conn.execute("ALTER TABLE compound_scenarios ADD COLUMN data_hash TEXT")
        if "loss_units" not in cols:
            conn.execute("ALTER TABLE compound_scenarios ADD COLUMN loss_units TEXT")
        # backfill pair_id for pre-A7 rows (identity is derivable from the
        # stored risk_ids; data_hash/loss_units stay NULL on historical rows
        # — they were computed without a fitted-series hash)
        for cid, risk_ids_json in conn.execute(
            "SELECT compound_id, risk_ids FROM compound_scenarios WHERE pair_id IS NULL"
        ).fetchall():
            try:
                risk_ids = sorted(json.loads(risk_ids_json))
            except (TypeError, json.JSONDecodeError):
                continue
            if risk_ids:
                conn.execute(
                    "UPDATE compound_scenarios SET pair_id = ? WHERE compound_id = ?",
                    (pair_id_for(risk_ids), cid),
                )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_compound_scenarios_pair "
            "ON compound_scenarios(pair_id)"
        )
        # Repair pre-existing graph-version duplicates before enforcing a
        # single current row per stable pair. Retain every historical row.
        conn.execute(
            "UPDATE compound_scenarios AS older SET status = 'superseded' "
            "WHERE older.status = 'candidate' AND older.pair_id IS NOT NULL "
            "AND older.compound_id != (SELECT newer.compound_id "
            "FROM compound_scenarios newer WHERE newer.pair_id = older.pair_id "
            "AND newer.status = 'candidate' "
            "ORDER BY COALESCE(newer.scored_at, '') DESC, newer.rowid DESC LIMIT 1)"
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_compound_current_pair "
            "ON compound_scenarios(pair_id) WHERE status = 'candidate' AND pair_id IS NOT NULL"
        )
        conn.execute("""
            CREATE TABLE IF NOT EXISTS compound_join_runs (
                run_id TEXT PRIMARY KEY,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL,
                n_risks INTEGER DEFAULT 0,
                n_pairs INTEGER DEFAULT 0,
                n_scored INTEGER DEFAULT 0,
                n_written INTEGER DEFAULT 0,
                note TEXT
            )
        """)
        # A7: append-only realization history. ``compound_scenarios`` is the
        # current view (INSERT OR REPLACE); every scored top-N realization is
        # also appended here in full fidelity, so estimator/config changes
        # are recorded as new rows instead of silently overwriting history
        # (review D8/D12). INSERT-only, enforced by trigger.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS score_realizations (
                realization_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                pair_id TEXT,
                compound_id TEXT,
                state_hash TEXT NOT NULL,
                data_hash TEXT,
                judgment_version TEXT NOT NULL DEFAULT 'det',
                algorithm_version TEXT,
                model_config_hash TEXT,
                rng_seed TEXT,
                p_hat REAL,
                p_lo REAL,
                p_hi REAL,
                scored_at TEXT NOT NULL,
                realization_json TEXT NOT NULL
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_score_realizations_pair "
            "ON score_realizations(pair_id, scored_at)"
        )
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS score_realizations_no_update
            BEFORE UPDATE ON score_realizations
            BEGIN SELECT RAISE(ABORT, 'score realization history cannot be updated'); END
        """)
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS score_realizations_no_delete
            BEFORE DELETE ON score_realizations
            BEGIN SELECT RAISE(ABORT, 'score realization history cannot be deleted'); END
        """)
        conn.commit()
    finally:
        conn.close()


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _last_successful_run(db_path: str) -> str | None:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT finished_at FROM compound_join_runs "
            "WHERE status = 'ok' ORDER BY finished_at DESC LIMIT 1"
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def is_due(db_path: str, *, today: datetime | None = None) -> bool:
    last = _last_successful_run(db_path)
    if not last:
        return True
    today = today or datetime.now(timezone.utc)
    try:
        last_dt = datetime.fromisoformat(last)
    except ValueError:
        return True
    return today - last_dt >= timedelta(days=MIN_INTERVAL_DAYS)


# ---------------------------------------------------------------------------
# The step
# ---------------------------------------------------------------------------


def _shadow_coverage(
    db_path: str,
    scorer: DetScorer,
    graph: ChannelGraph,
    atoms: list,
    production_pairs: list[tuple[str, str]],
) -> dict[str, Any] | None:
    """P1 shadow pass: score the pairs approved channel proposals WOULD add.

    Display-only coverage delta (HYBRID_SCORING_PROPOSAL.md P1 ablation,
    shadow-only per the program plan). The enriched graph exists only in
    memory: base edges plus each approved proposal materialized as
    ``GraphEdge(reason="expert", source=proposal_id)`` — a ``tag_add`` /
    ``new_channel`` links its atoms to every (in-memory) carrier of the
    channel, an ``edge_add`` links its two atoms directly. Delta pairs
    (connected in the enriched graph, not in production) are scored with
    the production-fitted scorer.

    Returns ``None`` when no approved proposals exist — the caller then
    emits no ``shadow`` key and the artifact is byte-identical to a
    shadow-less run. When a block IS returned, its contents go ONLY into
    the artifact's ``"shadow"`` key: never into ``compound_scenarios``,
    ``score_realizations``, or the artifact's ``"compounds"`` list. The
    production candidate tuple stays ("shared_channel", "empirical") on
    this branch; ``graduation_freeze`` records that the freeze holds by
    construction.
    """
    from .proposals import list_proposals  # lazy: the store lives outside scoring/

    approved = list_proposals(db_path, kind="channel", status="approved")
    if not approved:
        return None
    edges = list(graph.edges)
    channels: dict[str, set[str]] = {a.risk_id: set(a.channels) for a in atoms}
    n_applied = 0
    for prop in approved:
        kind = str(prop.get("kind") or "")
        risk_ids = [str(r) for r in (prop.get("risk_ids") or [])]
        channel = str(prop.get("channel") or "")
        proposal_id = str(prop.get("proposal_id") or "")
        new_edges: list[GraphEdge] = []
        if kind == "edge_add" and len(risk_ids) == 2:
            a, b = sorted(risk_ids)
            new_edges.append(
                GraphEdge(src=a, dst=b, reason="expert", weight=1.0,
                          source=proposal_id)
            )
        elif kind in ("tag_add", "new_channel") and channel and risk_ids:
            newly = set(risk_ids)
            for rid in risk_ids:
                channels.setdefault(rid, set()).add(channel)
            members = sorted(rid for rid, chs in channels.items() if channel in chs)
            for x, y in combinations(members, 2):
                if x in newly or y in newly:
                    new_edges.append(
                        GraphEdge(src=x, dst=y, reason="expert", weight=1.0,
                                  source=proposal_id)
                    )
        if new_edges:
            edges.extend(new_edges)
            n_applied += 1
    enriched = ChannelGraph(
        graph_id=graph.graph_id,
        version=graph.version,
        nodes=list(graph.nodes),
        edges=edges,
    )
    enriched_pairs = channel_blocked_pairs(
        enriched, reasons=("shared_channel", "empirical", "expert")
    )
    base = {tuple(sorted(p)) for p in production_pairs}
    coverage: list[dict[str, Any]] = []
    for pair in enriched_pairs:
        if pair in base:
            continue
        try:
            rlz = scorer.score_combo(list(pair))
        except Exception as exc:  # unfitted atom etc. — recorded, skipped
            logger.warning("shadow scoring skipped %s: %s", pair, exc)
            continue
        coverage.append(
            {"pair": list(pair), "pair_id": pair_id_for(list(pair)),
             "p_hat": rlz.p_hat}
        )
    coverage.sort(key=lambda r: (-r["p_hat"], r["pair"]))
    return {
        "n_proposals_applied": n_applied,
        "n_delta_pairs": len(coverage),
        "coverage_delta": coverage,
        "graduation_freeze": True,
    }


def _record_run(db_path: str, run_id: str, started: str, result: CompoundJoinResult) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO compound_join_runs VALUES (?,?,?,?,?,?,?,?,?)",
            (
                run_id, started, _utcnow(), result.status, result.n_risks,
                result.n_pairs, result.n_scored, result.n_written, result.note,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def run_compound_join(
    settings: Settings | None = None,
    *,
    force: bool = False,
    today: datetime | None = None,
    scorer_config: ScorerConfig | None = None,
    shadow: bool = True,
) -> CompoundJoinResult:
    """Run the monthly compound join. Safe to call on every pipeline run —
    it self-gates on cadence and data sufficiency.

    ``shadow`` (default on, inert when no approved channel proposals
    exist): after the production write path — which is unchanged — the
    artifact gains a display-only ``"shadow"`` coverage block for the
    pairs approved P1 proposals would have connected. Shadow scores are
    never written to ``compound_scenarios``, ``score_realizations``, or
    the artifact's ``"compounds"`` list (graduation freeze by
    construction)."""
    settings = settings or get_settings()
    db_path = str(settings.scenario_db_path)
    migrate_compound(db_path)
    run_id = f"compound-{uuid.uuid4().hex[:8]}"
    started = _utcnow()
    result = CompoundJoinResult(status="error", run_id=run_id)

    if not force and not is_due(db_path, today=today):
        result.status = "skipped_not_due"
        result.note = f"last successful join within {MIN_INTERVAL_DAYS} days"
        return result  # not a failure: no ledger write, no noise

    try:
        atoms, events, diagnostics = build_signal_series(
            settings.emerging_risks_path, settings.evidence_db_path
        )
        result.n_risks = len(atoms)
        ok, reasons = sufficiency(diagnostics, HOLDOUT_QUARTERS)
        if not ok:
            result.status = "insufficient_data"
            result.note = "; ".join(reasons)
            _record_run(db_path, run_id, started, result)
            return result

        n_periods = max(e.period for e in events) + 1
        train_quarters = n_periods - HOLDOUT_QUARTERS
        graph = build_channel_graph(atoms, graph_id="g_ch", version=1)
        cfg = scorer_config or ScorerConfig()
        scorer = DetScorer(cfg).fit(
            events, atoms, train_quarters, graph_hash=graph.graph_hash
        )
        pairs = channel_blocked_pairs(graph)
        result.n_pairs = len(pairs)
        telemetry = TelemetryLogger(TELEMETRY_PATH, run_id=run_id)
        realizations = []
        for pair in pairs:
            rlz = scorer.score_combo(list(pair))
            realizations.append(rlz)
            telemetry.emit(
                node="score_det",
                phase="evaluation",
                provider="none",
                model_id="det-scorer",
                plane="aggregate",
                scenario_id=rlz.scenario_id,
                realization_id=rlz.realization_id,
                config_hash=cfg.config_hash(),
                tags={"pair": list(pair)},
            )
        realizations.sort(key=lambda r: -r.p_hat)
        realizations = realizations[:TOP_N]
        result.n_scored = len(pairs)

        scored_at = _utcnow()
        artifact_rows = []
        staged_rows = []
        for rlz in realizations:
            compound_id = f"scn-{rlz.state_hash[:12]}"
            risk_ids = sorted(
                a["risk_id"] for a in rlz.components["atoms"]
            )
            pair_id = pair_id_for(risk_ids)
            # Monetary sufficiency belongs to this pair's training losses.
            # A loss elsewhere (or in holdout) cannot qualify this output.
            loss_semantics = rlz.components["mc"]["loss_semantics"]
            if loss_semantics == "activation_units":
                loss_q50 = loss_q95 = loss_mean = None
                loss_quantiles = None
            else:
                loss_q50 = rlz.loss_quantiles.get("0.5")
                loss_q95 = rlz.loss_quantiles.get("0.95")
                loss_mean = rlz.loss_mean
                loss_quantiles = rlz.loss_quantiles
            # C-08: the current view carries the export spelling
            # ("activation"/"dollars"), not the internal regime name — the
            # two vocabularies drifted (guard witness).
            loss_units = (
                "activation" if loss_semantics == "activation_units" else "dollars"
            )
            staged_rows.append(
                (rlz, compound_id, risk_ids, pair_id,
                 loss_q50, loss_q95, loss_mean, loss_units)
            )
            artifact_rows.append(
                {
                    "compound_id": compound_id,
                    "pair_id": pair_id,
                    "risk_ids": risk_ids,
                    "p_hat": rlz.p_hat,
                    "p_interval": list(rlz.p_interval),
                    "loss_quantiles": loss_quantiles,
                    "loss_units": loss_units,
                    "uncertainty_class": rlz.uncertainty_class,
                    "dependence_support": rlz.components["dependence"]["data_support"],
                    "evidence_support": rlz.evidence_support,
                    "data_hash": rlz.data_hash,
                    "judgment_version": rlz.judgment_version,
                    "severity_observations": {
                        a["risk_id"]: a["n_severity"] for a in rlz.components["atoms"]
                    },
                    "severity_note": rlz.components["mc"]["severity_note"],
                }
            )

        # Ontology gate (C-02/C-07/C-08): compounds and the run record pass
        # both gates before anything is persisted; on REJECT the run is
        # recorded as an error and the previous current view stands.
        if guard_enabled():
            gate = validate(
                [compound_record(r, run_id=run_id) for r in artifact_rows]
                + [run_record(run_id, "ok")],
                seam="run_compound_join",
            )
            try:
                gate.raise_if_rejected("run_compound_join")
            except GuardRejected as exc:
                result.status = "error"
                result.errors.append(str(exc))
                result.note = str(exc)
                _record_run(db_path, run_id, started, result)
                return result

        conn = sqlite3.connect(db_path)
        try:
            # A successful run replaces the current portfolio, including
            # pairs that fell out of the top-N. Historical rows and their
            # graduation links remain available under superseded status.
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE compound_scenarios SET status = 'superseded' "
                "WHERE status = 'candidate'"
            )
            for (rlz, compound_id, risk_ids, pair_id,
                 loss_q50, loss_q95, loss_mean, loss_units) in staged_rows:
                payload = json.dumps(rlz.to_dict(), sort_keys=True)
                prior = conn.execute(
                    "SELECT realization_json FROM score_realizations WHERE realization_id = ?",
                    (rlz.realization_id,),
                ).fetchone()
                if prior is not None and prior[0] != payload:
                    raise ValueError("score realization identity collision with a different payload")
                conn.execute(
                    "INSERT OR REPLACE INTO compound_scenarios VALUES "
                    "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        compound_id,
                        json.dumps(risk_ids),
                        rlz.state_hash,
                        rlz.p_hat,
                        rlz.p_interval[0],
                        rlz.p_interval[1],
                        loss_q50,
                        loss_q95,
                        loss_mean,
                        rlz.uncertainty_class,
                        rlz.components["dependence"]["data_support"],
                        rlz.evidence_support,
                        "R1",
                        "candidate",
                        run_id,
                        scored_at,
                        ALGORITHM_VERSION,
                        "Compound join of emerging risks from public signal "
                        "activations; research quantity, not a capital input.",
                        pair_id,
                        rlz.data_hash,
                        loss_units,
                    ),
                )
                # A7: full-fidelity append-only history. Idempotent on
                # realization identity — re-issuing byte-identical
                # realizations adds no row; any estimator/config/data change
                # produces a *new* realization row instead of overwriting.
                conn.execute(
                    "INSERT OR IGNORE INTO score_realizations VALUES "
                    "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        rlz.realization_id,
                        run_id,
                        pair_id,
                        compound_id,
                        rlz.state_hash,
                        rlz.data_hash,
                        rlz.judgment_version,
                        rlz.algorithm_version,
                        rlz.model_config_hash,
                        rlz.rng_seed,
                        rlz.p_hat,
                        rlz.p_interval[0],
                        rlz.p_interval[1],
                        scored_at,
                        payload,
                    ),
                )
            conn.commit()
        finally:
            conn.close()

        artifact = Path(settings.emerging_risks_path).parent / COMPOUND_ARTIFACT_NAME
        units = {row["loss_units"] for row in artifact_rows}
        artifact_doc: dict[str, Any] = {
            "run_id": run_id,
            "scored_at": scored_at,
            "algorithm_version": ALGORITHM_VERSION,
            "data_regime": "R1",
            "loss_units": next(iter(units)) if len(units) == 1 else "mixed" if units else "activation",
            "probability_scope": "public_signal_co_activation",
            "calibration_status": "not_validated_for_enterprise_use",
            "training_quarters": train_quarters,
            "holdout_quarters": HOLDOUT_QUARTERS,
            "compounds": artifact_rows,
        }
        # P1 shadow pass (wave 5): AFTER the production write path, which
        # is untouched. Display-only — see _shadow_coverage for the freeze
        # contract. Failures are recorded, never raised: production stands.
        if shadow:
            try:
                shadow_block = _shadow_coverage(
                    db_path, scorer, graph, atoms, pairs
                )
            except Exception as exc:
                logger.exception("compound join shadow pass failed")
                result.errors.append(f"shadow pass failed: {exc}")
                shadow_block = None
            if shadow_block is not None:
                artifact_doc["shadow"] = shadow_block
                telemetry.emit(
                    node="compound_join_shadow",
                    phase="evaluation",
                    provider="none",
                    model_id="det-scorer",
                    plane="public",
                    tags={
                        "n_proposals_applied": shadow_block["n_proposals_applied"],
                        "n_delta_pairs": shadow_block["n_delta_pairs"],
                        "graduation_freeze": True,
                    },
                )
        # tmp + rename: same atomic-write discipline as phase 2's
        # save_emerging_risks — a crash mid-write must not leave a
        # truncated artifact behind.
        tmp = artifact.with_suffix(".tmp")
        tmp.write_text(json.dumps(artifact_doc, indent=2, sort_keys=True))
        tmp.replace(artifact)
        result.status = "ok"
        result.n_written = len(artifact_rows)
        result.artifact = str(artifact)
        result.note = f"scored {result.n_scored} channel-blocked pairs, wrote top {result.n_written}"
        telemetry.emit(
            node="compound_join",
            phase="control",
            provider="none",
            model_id="",
            tags={
                "n_pairs": result.n_pairs,
                "n_written": result.n_written,
                "status": "ok",
            },
        )
        telemetry.write_run_report(
            TELEMETRY_PATH.parent / "run_report.json", n_promoted=result.n_written
        )
        _record_run(db_path, run_id, started, result)
        return result
    except Exception as exc:  # recorded, not raised: the pipeline must go on
        logger.exception("compound join failed")
        result.status = "error"
        result.errors.append(str(exc))
        result.note = str(exc)
        _record_run(db_path, run_id, started, result)
        return result


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Monthly compound join step")
    parser.add_argument("--force", action="store_true", help="ignore the due check")
    args = parser.parse_args(argv)
    result = run_compound_join(force=args.force)
    print(f"status: {result.status}")
    print(f"note:   {result.note}")
    if result.artifact:
        print(f"artifact: {result.artifact}")
    return 0 if result.status in ("ok", "insufficient_data", "skipped_not_due") else 1


if __name__ == "__main__":
    raise SystemExit(main())
