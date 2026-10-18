"""Web dashboard for the Contingency Atlas risk foresight pipeline.

Stdlib-only HTTP server (no new dependencies). Serves a single-page app
plus a JSON API aggregating scenarios, evidence, and emerging-risk data.

Run:
    python -m contingency_atlas.web            # http://127.0.0.1:8080
    python -m contingency_atlas.web --port 9000
"""

from __future__ import annotations

import argparse
import errno
import ipaddress
import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import yaml

from contingency_atlas.config import get_settings
from contingency_atlas.web import runner
from contingency_atlas.web.enterprise import EnterpriseHandlerMixin, POST_PATHS
from contingency_atlas.web.portfolio import PortfolioHandlerMixin

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"
MAX_REQUEST_BYTES = 256_000
MAX_INGEST_BYTES = 11_000_000  # base64 headroom for 8MB artifacts

DEFAULT_TELEMETRY_JSONL = Path("generated/telemetry/compound-join.jsonl")


def _telemetry_paths() -> list[Path]:
    """Telemetry logs for the Costs page: ATLAS_TELEMETRY env override
    (single file, used by tests) or every log in generated/telemetry/.
    Read per-request."""
    override = os.environ.get("ATLAS_TELEMETRY")
    if override:
        return [Path(override)]
    directory = Path("generated/telemetry")
    return sorted(directory.glob("*.jsonl")) if directory.exists() else []


def build_token_usage(period: str) -> dict[str, Any]:
    """Token usage time series for the situation-room chart.

    ``period``: "7d" (last 7 calendar days, UTC, daily buckets) or "7w"
    (last 7 ISO weeks, weekly buckets). Input/output stay separate —
    they are priced differently.
    """
    from contingency_atlas.scoring.telemetry import TelemetryLogger

    events: list[dict[str, Any]] = []
    for path in _telemetry_paths():
        if path.exists():
            events.extend(TelemetryLogger(path).load_events())

    now = datetime.now(timezone.utc)
    if period == "7w":
        # current ISO week + previous 6, Monday-start
        week_monday = (now - timedelta(days=now.weekday())).date()
        starts = [week_monday - timedelta(weeks=6 - i) for i in range(7)]
        keys = [s.isocalendar()[:2] for s in starts]
        labels = [f"W{wk:02d}" for (_, wk) in keys]

        def bucket_of(ts: str) -> int | None:
            try:
                d = datetime.fromisoformat(ts).date()
            except ValueError:
                return None
            iso = d.isocalendar()[:2]
            return keys.index(iso) if iso in keys else None
    else:
        period = "7d"
        today = now.date()
        starts = [today - timedelta(days=6 - i) for i in range(7)]
        keys = [s.isoformat() for s in starts]
        labels = [s[5:] for s in keys]

        def bucket_of(ts: str) -> int | None:
            key = ts[:10]
            return keys.index(key) if key in keys else None

    buckets = [{"label": labels[i], "input": 0, "output": 0} for i in range(7)]
    for e in events:
        idx = bucket_of(e.get("ts", ""))
        if idx is not None:
            buckets[idx]["input"] += e["input_tokens"]
            buckets[idx]["output"] += e["output_tokens"]
    return {
        "period": period,
        "buckets": buckets,
        "totals": {
            "input": sum(b["input"] for b in buckets),
            "output": sum(b["output"] for b in buckets),
        },
    }


def build_cost_summary() -> dict[str, Any] | None:
    """Cost/telemetry aggregation for the Costs page (telemetry-spec.md
    sections 3-4). Aggregates every telemetry log in generated/telemetry/
    (the production LLM log + per-run logs); attribution = direct scenario
    events + even share of cycle-level cost. Promoted count comes from the
    compound register (Phase 7 output)."""
    from contingency_atlas.scoring.telemetry import TelemetryLogger

    paths = _telemetry_paths()
    events: list[dict[str, Any]] = []
    for path in paths:
        if path.exists():
            events.extend(TelemetryLogger(path).load_events())
    if not events:
        return None

    def _bucket(key: str) -> dict[str, Any]:
        out: dict[str, dict[str, Any]] = {}
        for e in events:
            k = e.get(key) or "—"
            b = out.setdefault(k, {"cost_usd": 0.0, "n_events": 0, "tokens": 0,
                                   "input_tokens": 0, "output_tokens": 0,
                                   "latency_ms": 0})
            b["cost_usd"] += e["cost_usd"]
            b["n_events"] += 1
            b["tokens"] += e["input_tokens"] + e["output_tokens"]
            b["input_tokens"] += e["input_tokens"]
            b["output_tokens"] += e["output_tokens"]
            b["latency_ms"] += e["latency_ms"]
        for b in out.values():
            b["cost_usd"] = round(b["cost_usd"], 6)
            b["latency_avg_ms"] = round(b["latency_ms"] / b["n_events"])
            del b["latency_ms"]
        return dict(sorted(out.items(), key=lambda kv: -kv[1]["cost_usd"]))

    n_promoted = len(_rows(get_settings().scenario_db_path, "compound_scenarios"))

    spend = sum(e["cost_usd"] for e in events)
    shadow = sum(e["cost_shadow_usd"] for e in events)
    tok_in = sum(e["input_tokens"] for e in events)
    tok_out = sum(e["output_tokens"] for e in events)
    cache_read = sum(e["cache_read_tokens"] for e in events)
    cache_write = sum(e["cache_write_tokens"] for e in events)

    # attribution: cost = direct + even share of cycle-level (unattributed)
    # cost; tokens = DIRECT only — unattributed tokens are not scenario
    # property, diluting them produces nonsense numbers. The remainder is
    # shown as an explicit unattributed row. Input/output stay separate:
    # they are priced differently.
    direct: dict[str, float] = {}
    direct_in: dict[str, int] = {}
    direct_out: dict[str, int] = {}
    shared = 0.0
    shared_in = 0
    shared_out = 0
    for e in events:
        if e.get("scenario_id"):
            sid = e["scenario_id"]
            direct[sid] = direct.get(sid, 0.0) + e["cost_usd"]
            direct_in[sid] = direct_in.get(sid, 0) + e["input_tokens"]
            direct_out[sid] = direct_out.get(sid, 0) + e["output_tokens"]
        else:
            shared += e["cost_usd"]
            shared_in += e["input_tokens"]
            shared_out += e["output_tokens"]
    per_shared = shared / max(len(direct), 1)
    attributed = [
        {
            "scenario_id": sid,
            "cost_usd": round(c + per_shared, 6),
            "input_tokens": direct_in.get(sid, 0),
            "output_tokens": direct_out.get(sid, 0),
        }
        for sid, c in direct.items()
    ]
    attributed.sort(key=lambda r: -r["cost_usd"])

    pricing_versions = sorted(
        {e.get("unit_price", {}).get("pricing_version", "unknown") for e in events}
    )
    recent = [
        {
            "ts": e["ts"], "node": e["node"], "model_id": e["model_id"],
            "tokens": e["input_tokens"] + e["output_tokens"],
            "cost_usd": e["cost_usd"], "latency_ms": e["latency_ms"],
            "status": e["status"],
        }
        for e in events[-30:][::-1]
    ]
    return {
        "telemetry_path": ", ".join(str(p) for p in paths),
        "n_events": len(events),
        "spend_usd": round(spend, 6),
        "spend_shadow_usd": round(shadow, 6),
        "n_promoted": n_promoted,
        "cost_per_promoted_usd": round(spend / n_promoted, 6) if n_promoted else None,
        "tokens": {
            "input": tok_in,
            "output": tok_out,
            "cache_read": cache_read,
            "cache_write": cache_write,
        },
        "cache_hit_rate": round(cache_read / (cache_read + tok_in), 4) if (cache_read + tok_in) else None,
        "egress_denies": sum(1 for e in events if e["status"] == "denied_egress"),
        "by_phase": _bucket("phase"),
        "by_model": _bucket("model_id"),
        "by_node": _bucket("node"),
        "attributed": attributed[:20],
        "unattributed": {
            "cost_usd": round(shared, 6),
            "input_tokens": shared_in,
            "output_tokens": shared_out,
        },
        "pricing_versions": pricing_versions,
        "recent": recent,
    }


_SCENARIO_MAP = {
    "scenario_id": "id",
    "risk_category": "category",
    "created_year": "year",
    "financial_impact_band": "fin_impact",
    "event_driver_class": "event_name",
    "primary_cause_class": "cause",
    "impact_band": "impact_rank",
}

_EVIDENCE_EVENT_MAP = {
    "event_id": "ref",
    "loss_amount_usd": "loss_usd",
    "source_date": "date",
    "risk_category": "category",
    "source_url": "url",
}


def _rows(db_path: Path, table: str) -> list[dict[str, Any]]:
    if not db_path.exists():
        return []
    try:
        conn = sqlite3.connect(str(db_path))
    except sqlite3.Error:
        return []
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(f'SELECT * FROM "{table}"')]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def _renamed(rows: list[dict[str, Any]], mapping: dict[str, str]) -> list[dict[str, Any]]:
    out = []
    for row in rows:
        item = {}
        for key, value in row.items():
            item[mapping.get(key, key)] = value
        out.append(item)
    return out


def _pending_review_count(
    settings,
    evidence_events: list[dict],
    risks: list[dict],
) -> int:
    """I count every item waiting in the human review queue for the nav badge.

    Events enter as ``pending``. Risks enter as either ``pending`` or
    ``needs_update`` because a changed, previously decided risk is excluded
    downstream until a human reviews it again. ADR-014 merge, channel, and
    analogue proposals plus MITRE ATLAS mapping proposals are counted from
    the proposal store, tolerant of an unreadable or absent DB.
    """
    from contingency_atlas.proposals import list_analogues, list_proposals

    n = sum(
        1
        for record in evidence_events
        if (record.get("review_status") or "") == "pending"
    )
    n += sum(
        1
        for record in risks
        if (record.get("review_status") or "") in {"pending", "needs_update"}
    )
    if not settings.scenario_db_path.is_file():
        return n
    for kind in ("merge", "channel"):
        try:
            n += len(list_proposals(
                settings.scenario_db_path, kind=kind, status="pending"
            ))
        except sqlite3.Error:
            pass
    try:
        n += len(list_analogues(settings.scenario_db_path, status="pending"))
    except sqlite3.Error:
        pass
    try:
        from contingency_atlas.frameworks.mappings import list_mapping_proposals

        n += len(list_mapping_proposals(
            settings.scenario_db_path, status="pending"
        ))
    except (ImportError, sqlite3.Error):
        pass
    return n


def _attach_mitre_atlas_mappings(
    db_path: Path,
    source_rows: list[dict[str, Any]],
    display_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """I attach only reviewed mappings and keep the overlay non-scoring.

    I support catalog absence, so I return an empty overlay instead of
    requiring an import before my core dashboard works.
    """
    try:
        from contingency_atlas.frameworks import framework_status
        from contingency_atlas.frameworks.mappings import (
            approved_mappings_for_scenarios,
        )

        status = framework_status(db_path)
        approved = approved_mappings_for_scenarios(db_path, source_rows)
    except (ImportError, sqlite3.Error, ValueError):
        status, approved = {}, {}

    active = status.get("active") or status.get("active_release")
    if isinstance(active, dict):
        active_version = active.get("content_version")
    else:
        active_version = active
    for row in display_rows:
        scenario_id = str(row.get("id") or row.get("scenario_id") or "")
        row["mitre_atlas"] = {
            "active_release": active_version,
            "mappings": approved.get(scenario_id, []),
        }
    return status


def build_payload() -> dict[str, Any]:
    """Aggregate all dashboard data from the configured stores."""
    from contingency_atlas.web.health import data_health
    settings = get_settings()
    health = data_health(settings)
    scenario_available = next(item["status"] for item in health["stores"]
                              if item["store"] == "scenarios") == "ok"

    scenario_rows = _rows(settings.scenario_db_path, "scenarios")
    scenarios = _renamed(scenario_rows, _SCENARIO_MAP)
    mitre_atlas_status = _attach_mitre_atlas_mappings(
        settings.scenario_db_path, scenario_rows, scenarios
    ) if scenario_available else {}
    evidence_events = _renamed(
        _rows(settings.evidence_db_path, "evidence_events"),
        _EVIDENCE_EVENT_MAP,
    )
    runs = _rows(settings.scenario_db_path, "pipeline_runs")
    patterns = _rows(settings.scenario_db_path, "pattern_detections")
    causal = _rows(settings.scenario_db_path, "causal_findings")

    risks: list[dict[str, Any]] = []
    if settings.emerging_risks_path.exists():
        try:
            data = json.loads(settings.emerging_risks_path.read_text())
            if isinstance(data, list):
                risks = [r for r in data if isinstance(r, dict)]
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            risks = []

    active = [s for s in scenarios if (s.get("status") or "active") == "active"]
    scored = [s["urgency_score"] for s in active if isinstance(s.get("urgency_score"), (int, float))]
    total_loss = sum(event.get("loss_usd") or 0 for event in evidence_events)

    return {
        "meta": {
            "data_health": health,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "scenario_count": len(scenarios),
            "active_count": len(active),
            # Rejected records stay in the payload for audit views but are
            # not part of the research corpus, so they are not counted.
            "evidence_event_count": sum(
                1 for e in evidence_events if e.get("review_status") != "rejected"
            ),
            "risk_count": sum(1 for r in risks if r.get("review_status") != "rejected"),
            "pending_review": _pending_review_count(
                settings,
                evidence_events,
                risks,
            ),
            "total_loss_usd": total_loss,
            "avg_urgency": (sum(scored) / len(scored)) if scored else None,
            "max_urgency": max(scored) if scored else None,
            "omlx_model": settings.llm_model,
            "llm_provider": settings.llm_provider,
            "auto_retire": bool(settings.auto_retire),
            "mitre_atlas": mitre_atlas_status,
        },
        "scenarios": scenarios,
        "evidence_events": evidence_events,
        "risks": risks,
        "runs": runs,
        "patterns": patterns,
        "causal": causal,
        "compounds": _compound_rows(settings, risks),
        "compound_join_runs": _compound_join_runs(settings),
        "compound_shadow": _compound_shadow(settings),
        "llm_join": _llm_join_artifact(settings),
    }


def _llm_join_artifact(settings) -> dict[str, Any] | None:
    """The dark-matter join artifact (docs/technical/llm-compound-join.md),
    or None when no run has written it yet. Read per-request, tolerant of
    absence and corruption — the display strip simply does not render.
    Display-only: the artifact never feeds production scores.
    """
    artifact = Path(settings.emerging_risks_path).parent / "compound_join_llm.json"
    try:
        data = json.loads(artifact.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _compound_shadow(settings) -> dict[str, Any] | None:
    """The join artifact's display-only ``"shadow"`` block (P1 what-if
    watch), or None when no artifact/block exists. Read per-request,
    tolerant of absence and corruption — the strip simply does not render.
    """
    artifact = Path(settings.emerging_risks_path).parent / "compound_scenarios.json"
    try:
        data = json.loads(artifact.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    shadow = data.get("shadow") if isinstance(data, dict) else None
    return shadow if isinstance(shadow, dict) else None


def _score_realization_history(db_path: Path, pair_id: str) -> list[dict[str, Any]]:
    """Append-only score history for one pair (A7 paper trail), oldest
    first. Empty before the first join or for an unknown pair."""
    if not pair_id or not db_path.exists():
        return []
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT pair_id, run_id, p_hat, p_lo, p_hi, model_config_hash,"
            " algorithm_version, judgment_version, scored_at"
            " FROM score_realizations WHERE pair_id = ?"
            " ORDER BY scored_at ASC, rowid ASC",
            (str(pair_id),),
        ).fetchall()
    except sqlite3.Error:  # table predates the first join
        return []
    finally:
        conn.close()
    return [dict(r) for r in rows]


def _compound_rows(
    settings, risks: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Phase 7 compound scenarios with member titles, holdout-survival
    flag, and graduation status (degrades to [] before the first join)."""
    rows = [row for row in _rows(settings.scenario_db_path, "compound_scenarios")
            if row.get("status") != "superseded"]
    if not rows:
        return []
    graduations = {
        r["compound_id"]: r
        for r in _rows(settings.scenario_db_path, "compound_graduations")
    }
    graduations_by_pair = {row.get("pair_id"): row for row in graduations.values() if row.get("pair_id")}
    # slug -> title map, same slugification as the inventory loader
    from contingency_atlas.scoring.inventory import slugify

    titles: dict[str, str] = {}
    seen: dict[str, int] = {}
    for item in risks:
        title = item.get("issue") or item.get("title") or ""
        base = slugify(title)
        n = seen.get(base, 0)
        seen[base] = n + 1
        rid = base if n == 0 else f"{base}-{n + 1}"
        titles[rid] = title
    try:
        from contingency_atlas.compound_graduate import _holdout_fired_sets

        holdout_fired, _ = _holdout_fired_sets(settings)
    except Exception:
        holdout_fired = {}
    # P6 red-team memos, display-only: latest stored refutation memo per
    # compound, shown beside the claim (never a gate — ADR-001/013).
    try:
        from contingency_atlas.refutations import latest_memos

        memos = latest_memos(settings.scenario_db_path)
    except Exception:
        memos = {}
    # P2 analogue dossiers, display-only: APPROVED analogues keyed on the
    # stable A7 pair_id (graph-version churn cannot orphan them). Shown
    # beside the compound claim; never a score input (ADR-001/014).
    try:
        from contingency_atlas.proposals import list_analogues

        approved_analogues = list_analogues(
            settings.scenario_db_path, status="approved"
        )
    except Exception:
        approved_analogues = []
    dossiers_by_pair: dict[str, list[dict[str, Any]]] = {}
    for analogue in approved_analogues:
        dossiers_by_pair.setdefault(analogue.get("pair_id"), []).append(
            {
                "analogue_id": analogue.get("analogue_id"),
                "event": analogue.get("event"),
                "period": analogue.get("period"),
                "outcome_class": analogue.get("outcome_class"),
                "co_fired": analogue.get("co_fired"),
                "n_disanalogies": len(analogue.get("disanalogies") or []),
                "n_citations": len(analogue.get("citations") or []),
                "reviewer": analogue.get("reviewer"),
                "decided_at": analogue.get("decided_at"),
            }
        )

    out = []
    for row in rows:
        try:
            risk_ids = json.loads(row.get("risk_ids") or "[]")
        except json.JSONDecodeError:
            risk_ids = []
        grad = graduations.get(row.get("compound_id")) or graduations_by_pair.get(row.get("pair_id"))
        memo = memos.get(row.get("compound_id"))
        refutation = None
        if memo:
            refutation = {
                "memo_id": memo.get("memo_id"),
                "case_against": memo.get("case_against"),
                "key_doubts": memo.get("key_doubts"),
                "falsifier": memo.get("falsifier"),
                "definition_stress": memo.get("definition_stress"),
                "reviewer_rating": memo.get("reviewer_rating"),
                "model_id": memo.get("model_id"),
                "created_at": memo.get("created_at"),
            }
        # k-agnostic holdout check: the full risk set co-fired in one
        # holdout quarter (matches the graduation attestation, A7)
        if len(risk_ids) >= 2 and holdout_fired:
            holdout_survived = any(
                set(risk_ids) <= fired for fired in holdout_fired.values()
            )
        else:
            holdout_survived = None
        out.append(
            {
                "compound_id": row.get("compound_id"),
                "pair_id": row.get("pair_id"),
                "risk_ids": risk_ids,
                "member_titles": [titles.get(r, r) for r in risk_ids],
                "p_hat": row.get("p_hat"),
                "p_lo": row.get("p_lo"),
                "p_hi": row.get("p_hi"),
                "loss_q95": row.get("loss_q95"),
                "loss_mean": row.get("loss_mean"),
                # "activation_units" => no loss semantics; UI must not
                # render dollar figures for this row (A7)
                "loss_units": row.get("loss_units"),
                "uncertainty_class": row.get("uncertainty_class"),
                "dependence_support": row.get("dependence_support"),
                "evidence_support": row.get("evidence_support"),
                "holdout_survived": holdout_survived,
                "graduated_scenario_id": grad.get("scenario_id") if grad else None,
                "judge_score": grad.get("judge_score") if grad else None,
                "refutation": refutation,
                "dossiers": dossiers_by_pair.get(row.get("pair_id"), []),
                "scored_at": row.get("scored_at"),
                "run_id": row.get("run_id"),
                "status": row.get("status"),
            }
        )
    out.sort(key=lambda r: -(r.get("p_hat") or 0))
    return out


def _compound_join_runs(settings) -> list[dict[str, Any]]:
    """Recent join runs for the ledger strip."""
    rows = _rows(settings.scenario_db_path, "compound_join_runs")
    rows.sort(key=lambda r: r.get("started_at") or "", reverse=True)
    return rows[:10]


# Latest EVAL/governance artifacts written by the on-demand pipeline cards
# (runner.py writes these CWD-relative paths). Informational only.
EVAL_ARTIFACTS = {
    "crash_test": Path("generated/redesign/phase1/r0/eval_report.json"),
    "parallel_worlds": Path("generated/redesign/phase1/multiseed/multiseed_report.json"),
    "ablations": Path("generated/redesign/phase1/ablations/ablations.json"),
    "judgment": Path("generated/judgment/judgment_report.json"),
}


def build_eval_reports() -> dict[str, Any]:
    """The latest EVAL + judgment artifacts, parsed for dashboard display.

    Missing or corrupt files come back as None — the panel renders those
    as 'not run yet' instead of failing.
    """
    out: dict[str, Any] = {}
    for key, path in EVAL_ARTIFACTS.items():
        try:
            out[key] = {
                "path": str(path),
                "modified_at": datetime.fromtimestamp(
                    path.stat().st_mtime, timezone.utc
                ).isoformat(),
                "report": json.loads(path.read_text()),
            }
        except (OSError, json.JSONDecodeError):
            out[key] = None
    return out


def build_review_queue() -> dict[str, Any]:
    """Reviewable candidates with deterministic validation reports.

    The queue includes pending events, pending or needs-update risks, and
    pending proposal rows. Cached machine pre-analysis remains advisory and
    this read-only aggregate leaves decisions to ``POST /api/review/decide``.
    """
    from contingency_atlas.review import (
        ReviewError,
        evidence_validation_report,
        list_evidence_for_review,
        list_risks_for_review,
        risk_validation_report,
    )
    from contingency_atlas.db import get_evidence_event
    from contingency_atlas.research import load_research_memos
    from contingency_atlas.review_assist import (
        agreement_stats,
        content_hash,
        load_suggestions,
        record_key,
    )
    from contingency_atlas.proposals import (
        acceptance_stats,
        list_analogues,
        list_proposals,
    )

    settings = get_settings()
    try:
        events = list_evidence_for_review(settings.evidence_db_path)
    except ReviewError:
        events = []
    try:
        # needs_update is a record whose assessment changed after a human
        # decided it, so it is waiting on a human exactly like pending is —
        # and phases 3 and 5 exclude it until one acts. Queue both, or the
        # record is stranded: invisible here, yet excluded downstream.
        risks = list_risks_for_review(
            settings.emerging_risks_path,
            review_status=("pending", "needs_update"),
        )
    except (OSError, json.JSONDecodeError):
        risks = []
    suggestions = load_suggestions(settings.scenario_db_path)
    memos = load_research_memos(settings.scenario_db_path)

    def entry(kind: str, record: dict[str, Any]) -> dict[str, Any]:
        from contingency_atlas.decisions import record_revision
        rid = record_key(kind, record)
        report = (
            evidence_validation_report(record)
            if kind == "event"
            else risk_validation_report(record, evidence_db=settings.evidence_db_path)
        )
        suggestion = suggestions.get((kind, rid))
        stale = bool(
            suggestion and suggestion.get("content_hash") != content_hash(kind, record)
        )
        memo = memos.get((kind, rid))
        if memo and memo.get("candidates"):
            # Resolve each machine-found candidate's CURRENT review status so
            # the card can show which are approved and adoptable as refs.
            resolved = []
            for event_id in memo["candidates"]:
                row = get_evidence_event(str(settings.evidence_db_path), str(event_id))
                resolved.append({
                    "event_id": event_id,
                    "review_status": (row or {}).get("review_status") or "missing",
                    "headline": (row or {}).get("headline"),
                })
            memo = dict(memo, candidate_status=resolved)
        return {
            "kind": kind,
            "record_id": rid,
            "revision": record_revision(record),
            "record": record,
            "report": report,
            "suggestion": suggestion,
            "suggestion_stale": stale,
            "research": memo,
        }

    def merge_entry(proposal: dict[str, Any]) -> dict[str, Any]:
        """Queue entry for a pending merge proposal (P8).

        Same entry shape as events/risks so the review UI renders one
        queue. The "report" is informational only — similarity components,
        not approval gates: merge decisions are pure human judgment with a
        written reason (ADR-014), the advisory verdict is display-only.
        """
        components = proposal.get("components") or {}
        shared = components.get("shared_tokens") or []
        checks = [
            {
                "key": "similarity",
                "label": "Similarity blend",
                "status": "pass",
                "detail": (
                    f"{proposal.get('similarity', 0.0):.3f} "
                    f"(title {components.get('title_jaccard', '—')} · "
                    f"text {components.get('text_jaccard', '—')})"
                ),
            },
            {
                "key": "shared_tokens",
                "label": "Shared tokens",
                "status": "pass",
                "detail": ", ".join(shared) if shared else "—",
            },
        ]
        record = dict(proposal)
        # display conveniences for the shared review-item rendering
        record["issue"] = f"{proposal.get('title_a', '')} ⇄ {proposal.get('title_b', '')}"
        record["description"] = (
            f"A: {proposal.get('title_a', '')}\n"
            f"B: {proposal.get('title_b', '')}"
        )
        return {
            "kind": "merge",
            "record_id": proposal.get("proposal_id"),
            "record": record,
            "report": {
                "kind": "merge",
                "approvable": True,
                "checks": checks,
                "refs": [],
                "blockers": [],
            },
            "suggestion": None,
            "suggestion_stale": False,
        }

    def channel_entry(proposal: dict[str, Any]) -> dict[str, Any]:
        """Queue entry for a pending channel proposal (P1).

        Informational report only — the proposed kind/channel, the named
        transmission mechanism, and the citation-clamp status. Decisions
        are pure human judgment with a written reason (ADR-014); approval
        changes NOTHING in production (graph application is deferred) —
        the row only feeds the join's shadow pass.
        """
        kind_label = {
            "tag_add": "missing tag",
            "edge_add": "expert edge",
            "new_channel": "new channel",
        }.get(proposal.get("kind"), str(proposal.get("kind") or ""))
        risk_ids = proposal.get("risk_ids") or []
        checks = [
            {
                "key": "proposal",
                "label": "Proposal",
                "status": "pass",
                "detail": (
                    f"{kind_label} · channel "
                    f"{proposal.get('channel') or '—'} · "
                    f"{', '.join(risk_ids)}"
                ),
            },
            {
                "key": "mechanism",
                "label": "Transmission mechanism",
                "status": "pass",
                "detail": proposal.get("mechanism") or "—",
            },
            {
                "key": "citation",
                "label": "Citation (URL-clamped at intake)",
                "status": "pass",
                "detail": (
                    f"{proposal.get('citation_status') or 'unverified'} · "
                    f"{proposal.get('citation_url') or '—'}"
                ),
            },
        ]
        record = dict(proposal)
        # display conveniences for the shared review-item rendering
        record["issue"] = (
            f"{kind_label}: {proposal.get('channel') or '—'}"
            f" ⇄ {', '.join(risk_ids)}"
        )
        record["description"] = proposal.get("mechanism") or ""
        # Advisory machine explanation (review_suggestions cache) — display
        # only; a mechanism/citation rewrite makes it stale.
        suggestion = suggestions.get(("channel", proposal.get("proposal_id")))
        stale = bool(
            suggestion
            and suggestion.get("content_hash") != content_hash("channel", proposal)
        )
        return {
            "kind": "channel",
            "record_id": proposal.get("proposal_id"),
            "record": record,
            "report": {
                "kind": "channel",
                "approvable": True,
                "checks": checks,
                "refs": [],
                "blockers": [],
            },
            "suggestion": suggestion,
            "suggestion_stale": stale,
            "research": memos.get(("channel", proposal.get("proposal_id"))),
        }

    def analogue_entry(analogue: dict[str, Any]) -> dict[str, Any]:
        """Queue entry for a pending analogue dossier (P2).

        The checks mirror review.validate_analogue_for_approval — the
        claim must be mechanism-mapped, cited, and name its disanalogies
        — but the decision stays human (single reviewer + written reason,
        ADR-014). Display-only: no prior conversion exists on this branch.
        """
        mechanism_map = analogue.get("mechanism_map") or {}
        mapped = any(str(v).strip() for v in mechanism_map.values())
        citations = analogue.get("citations") or []
        disanalogies = analogue.get("disanalogies") or []
        checks = [
            {
                "key": "mechanism",
                "label": "Mechanism map",
                "status": "pass" if mapped else "fail",
                "detail": json.dumps(mechanism_map, sort_keys=True)
                if mapped else "—",
            },
            {
                "key": "citations",
                "label": "Citations",
                "status": "pass" if citations else "fail",
                "detail": f"{len(citations)} citation(s)",
            },
            {
                "key": "disanalogies",
                "label": "Disanalogies (mandatory)",
                "status": "pass" if disanalogies else "fail",
                "detail": f"{len(disanalogies)} named",
            },
        ]
        blockers = [c["label"] for c in checks if c["status"] != "pass"]
        record = dict(analogue)
        record["issue"] = analogue.get("event") or analogue.get("analogue_id")
        record["description"] = (
            f"period {analogue.get('period') or '—'} · outcome "
            f"{analogue.get('outcome_class') or '—'} · co_fired "
            f"{analogue.get('co_fired')}"
        )
        return {
            "kind": "analogue",
            "record_id": analogue.get("analogue_id"),
            "record": record,
            "report": {
                "kind": "analogue",
                "approvable": not blockers,
                "checks": checks,
                "refs": [],
                "blockers": blockers,
            },
            "suggestion": None,
            "suggestion_stale": False,
        }

    def mitre_atlas_entry(
        proposal: dict[str, Any], scenarios_by_id: dict[str, dict[str, Any]]
    ) -> dict[str, Any]:
        """I render one pinned applicability claim inside the shared queue."""
        from contingency_atlas.frameworks.mappings import mapping_state

        scenario = scenarios_by_id.get(str(proposal.get("scenario_id") or ""))
        current = bool(
            scenario and mapping_state(proposal, scenario) == "current"
        )
        technique_name = proposal.get("name") or proposal.get("technique_name")
        scenario_title = (scenario or {}).get("title")
        checks = [
            {
                "key": "technique",
                "label": "Pinned technique",
                "status": "pass" if technique_name else "fail",
                "detail": (
                    f"{proposal.get('technique_id') or '—'} · "
                    f"{technique_name or 'not found in pinned release'}"
                ),
            },
            {
                "key": "scenario_snapshot",
                "label": "Scenario snapshot",
                "status": "pass" if current else "fail",
                "detail": "current" if current else "scenario missing or changed",
            },
            {
                "key": "rationale",
                "label": "Applicability rationale",
                "status": "pass" if proposal.get("rationale") else "fail",
                "detail": proposal.get("rationale") or "—",
            },
        ]
        blockers = [c["label"] for c in checks if c["status"] != "pass"]
        record = {
            **proposal,
            "issue": (
                f"{proposal.get('technique_id') or 'Technique'} · "
                f"{technique_name or 'unknown'} → {scenario_title or proposal.get('scenario_id')}"
            ),
            "description": proposal.get("rationale") or "",
            "technique_name": technique_name,
            "technique_description": proposal.get("description"),
            "scenario_title": scenario_title,
            "scenario_state": "current" if current else "stale",
        }
        return {
            "kind": "mitre_atlas",
            "record_id": proposal.get("proposal_id"),
            "record": record,
            "report": {
                "kind": "mitre_atlas",
                "approvable": not blockers,
                "checks": checks,
                "refs": [],
                "blockers": blockers,
            },
            "suggestion": None,
            "suggestion_stale": False,
        }

    try:
        merge_pending = list_proposals(settings.scenario_db_path, status="pending")
    except sqlite3.Error:
        merge_pending = []
    try:
        channel_pending = list_proposals(
            settings.scenario_db_path, kind="channel", status="pending"
        )
    except sqlite3.Error:
        channel_pending = []
    try:
        analogue_pending = list_analogues(settings.scenario_db_path, status="pending")
    except sqlite3.Error:
        analogue_pending = []
    try:
        from contingency_atlas.frameworks.mappings import list_mapping_proposals

        mitre_pending = list_mapping_proposals(
            settings.scenario_db_path, status="pending", include_context=True
        )
    except (ImportError, sqlite3.Error):
        mitre_pending = []
    scenarios_by_id = {
        str(row.get("scenario_id") or ""): row
        for row in _rows(settings.scenario_db_path, "scenarios")
    }

    return {
        "events": [entry("event", e) for e in events],
        "risks": [entry("risk", r) for r in risks],
        "merge_proposals": {
            "pending": [merge_entry(p) for p in merge_pending],
            "acceptance": acceptance_stats(settings.scenario_db_path),
        },
        "channel_proposals": {
            "pending": [channel_entry(p) for p in channel_pending],
        },
        "analogue_proposals": {
            "pending": [analogue_entry(a) for a in analogue_pending],
        },
        "mitre_atlas_proposals": {
            "pending": [
                mitre_atlas_entry(proposal, scenarios_by_id)
                for proposal in mitre_pending
            ],
        },
        "agreement": agreement_stats(settings.scenario_db_path),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def load_about() -> dict[str, Any]:
    """About-page content from about.yaml (ATLAS_ABOUT override).

    Content, not config: missing/invalid file degrades to {} rather
    than raising — the page simply renders an empty state.
    """
    default_path = Path(__file__).with_name("about.yaml")
    path = Path(os.environ.get("ATLAS_ABOUT", str(default_path))).expanduser()
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        return {"error": f"invalid YAML in {path.name}: {exc}"}
    return data if isinstance(data, dict) else {}


def _is_loopback_bind_host(host: str) -> bool:
    """Return whether a server bind target is explicitly local-only."""
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class DashboardHandler(EnterpriseHandlerMixin, PortfolioHandlerMixin, BaseHTTPRequestHandler):
    server_version = "ContingencyAtlasDashboard/0.1"

    def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
        path = self.path.split("?", 1)[0]
        if path.startswith("/api/") and not self._local_request_allowed():
            self._send_json(
                {"error": "dashboard APIs are restricted to loopback"},
                status=403,
            )
            return
        if path.startswith("/api/") and not self._authorize(path, "GET"):
            return
        if path == "/api/access":
            self._send_json(self.atlas_principal.public_dict())
            return
        if self._enterprise_get(path):
            return
        if self._portfolio_get(path):
            return
        if path in ("/", "/index.html"):
            self._send_file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
        elif path in ("/experience.css", "/experience.js"):
            self._send_file(STATIC_DIR / path[1:],
                            "text/css; charset=utf-8" if path.endswith(".css")
                            else "text/javascript; charset=utf-8")
        elif path == "/costs":
            # The Costs page is now a Command Deck view — keep old links
            # working, forwarding any shareable what-if rates (?pin=&pout=).
            query = self.path.split("?", 1)[1] if "?" in self.path else ""
            target = f"/?{query}#costs" if query else "/#costs"
            self.send_response(302)
            self.send_header("Location", target)
            self.end_headers()
        elif path == "/api/costs/summary":
            costs = build_cost_summary()
            if costs is None:
                self._send_json({"error": "no telemetry log found"}, status=404)
            else:
                self._send_json(costs)
        elif path == "/api/tokens/usage":
            query = parse_qs(urlparse(self.path).query)
            self._send_json(build_token_usage(str(query.get("period", ["7d"])[0])))
        elif path == "/favicon.svg":
            self._send_file(STATIC_DIR / "favicon.svg", "image/svg+xml")
        elif path == "/api/data":
            self._send_json(build_payload())
        elif path == "/api/eval/reports":
            self._send_json(build_eval_reports())
        elif path == "/api/review/pending":
            self._send_json(build_review_queue())
        elif path == "/api/review/history":
            from contingency_atlas.decisions import list_decisions
            query = parse_qs(urlparse(self.path).query)
            kind = str(query.get("kind", ["event"])[0])
            record_id = str(query.get("record_id", [""])[0])
            settings = get_settings()
            try:
                if kind == "risk":
                    from contingency_atlas.phases.phase2_risks import load_emerging_risks
                    history = [entry for record in load_emerging_risks(settings.emerging_risks_path)
                               if not record_id or record_id in {record.get("risk_id"), record.get("issue")}
                               for entry in record.get("review_history", [])]
                elif kind == "event":
                    history = list_decisions(str(settings.evidence_db_path), kind=kind, record_id=record_id or None)
                else:
                    self._send_json({"error": "kind must be event or risk"}, status=400)
                    return
                self._send_json({"history": history})
            except (OSError, sqlite3.Error, ValueError):
                self._send_json({"error": "Decision history is unavailable"}, status=503)
        elif path == "/api/scenarios/recommendation":
            self._handle_scenario_recommendation()
        elif path == "/api/frameworks/mitre-atlas/status":
            from contingency_atlas.frameworks import framework_status

            try:
                status = framework_status(get_settings().scenario_db_path)
            except (OSError, sqlite3.Error):
                logger.exception("MITRE ATLAS status lookup failed")
                self._send_json(
                    {"error": "MITRE ATLAS catalog status is unavailable"},
                    status=500,
                )
            else:
                self._send_json(status)
        elif path == "/api/frameworks/mitre-atlas/techniques":
            self._handle_mitre_atlas_technique_search()
        elif path == "/api/compounds/history":
            query = parse_qs(urlparse(self.path).query)
            pair_id = str(query.get("pair_id", [""])[0])
            self._send_json({
                "pair_id": pair_id,
                "history": _score_realization_history(
                    get_settings().scenario_db_path, pair_id
                ),
            })
        elif path == "/api/about":
            self._send_json(load_about())
        elif path == "/api/health":
            self._send_json({"ok": True})
        elif path == "/api/settings":
            from contingency_atlas.web.settings import build_settings_payload

            self._send_json(build_settings_payload(get_settings()))
        elif path == "/api/omlx":
            from contingency_atlas.web.display import safe_endpoint_url
            settings = get_settings()
            self._send_json({
                "reachable": runner.omlx_reachable(settings),
                "provider": settings.llm_provider,
                "base_url": safe_endpoint_url(settings.llm_base_url),
                "model": settings.llm_model,
                "auto_retire": bool(settings.auto_retire),
            })
        elif path == "/api/intake/sources":
            self._handle_intake_sources()
        elif path in ("/api/bulk-ingest/folders", "/api/bulk-ingest/batch"):
            self._handle_bulk_ingest_get(path)
        elif path.startswith("/api/jobs/"):
            job = runner.get_job(path.rsplit("/", 1)[-1])
            if job:
                self._send_json(job)
            else:
                self.send_error(404, "Unknown job")
        else:
            self.send_error(404, "Not found")

    def do_POST(self) -> None:  # noqa: N802 (stdlib naming)
        path = self.path.split("?", 1)[0]
        if path not in ("/api/run", "/api/ingest/convert",
                        "/api/intake/prepare", "/api/intake/submit",
                        "/api/review/decide",
                        "/api/review/decide-batch",
                        "/api/review/adopt-refs", "/api/review/detach-refs",
                        "/api/review/rate-memo",
                        "/api/review/scan-duplicates",
                        "/api/frameworks/mitre-atlas/proposals",
                        "/api/settings/frameworks/mitre-atlas/import",
                        "/api/settings/frameworks/mitre-atlas/activate",
                        "/api/settings/models",
                        "/api/bulk-ingest/plan", "/api/bulk-ingest/submit") and path not in POST_PATHS:
            self.send_error(404, "Not found")
            return

        if not self._mutation_request_allowed():
            self._send_json(
                {"error": "run requests are restricted to the local dashboard"},
                status=403,
            )
            return

        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].lower()
        if content_type != "application/json":
            self._send_json(
                {"error": "Content-Type must be application/json"},
                status=415,
            )
            return

        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            self._send_json({"error": "Content-Length is required"}, status=411)
            return
        try:
            length = int(raw_length)
        except ValueError:
            self._send_json({"error": "invalid Content-Length"}, status=400)
            return
        if length < 0:
            self._send_json({"error": "invalid Content-Length"}, status=400)
            return
        max_bytes = (
            MAX_INGEST_BYTES
            if path in (
                "/api/ingest/convert",
                "/api/settings/frameworks/mitre-atlas/import",
                "/api/imports/preview",
            )
            else MAX_REQUEST_BYTES
        )
        if length > max_bytes:
            self._send_json({"error": "request body is too large"}, status=413)
            return

        raw_body = self.rfile.read(length)
        if not self._authorize(path, "POST", raw_body):
            return
        try:
            body = json.loads(raw_body or b"{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json({"error": "invalid JSON body"}, status=400)
            return
        if not isinstance(body, dict):
            self._send_json({"error": "JSON body must be an object"}, status=400)
            return
        from contingency_atlas.web.access import bind_actor
        body = bind_actor(body, self.atlas_principal)

        if self._enterprise_post(path, body):
            return
        if path == "/api/ingest/convert":
            self._handle_ingest_convert(body)
            return

        if path == "/api/intake/prepare":
            self._handle_intake_prepare(body)
            return

        if path == "/api/intake/submit":
            self._handle_intake_submit(body)
            return

        if path == "/api/frameworks/mitre-atlas/proposals":
            self._handle_mitre_atlas_proposal(body)
            return

        if path == "/api/settings/frameworks/mitre-atlas/import":
            self._handle_settings_atlas_import(body)
            return

        if path == "/api/settings/frameworks/mitre-atlas/activate":
            self._handle_settings_atlas_activate(body)
            return

        if path == "/api/settings/models":
            self._handle_settings_models(body)
            return

        if path == "/api/review/decide":
            self._handle_review_decide(body)
            return

        if path == "/api/review/decide-batch":
            self._handle_review_decide_batch(body)
            return

        if path == "/api/review/adopt-refs":
            self._handle_review_adopt_refs(body)
            return

        if path == "/api/review/detach-refs":
            self._handle_review_detach_refs(body)
            return

        if path == "/api/review/rate-memo":
            self._handle_rate_memo(body)
            return

        if path == "/api/review/scan-duplicates":
            self._handle_scan_duplicates(body)
            return

        if path in ("/api/bulk-ingest/plan", "/api/bulk-ingest/submit"):
            self._handle_bulk_ingest_post(path, body)
            return

        if body.get("phase") == "bulk_ingest":
            # Validate the folder against the allow-list before queueing, so a
            # refused path never reaches the durable journal.
            from contingency_atlas.web.bulk_ingest_web import FolderError, resolve_ingest_folder
            try:
                body = {**body, "folder": str(resolve_ingest_folder(get_settings(), body.get("folder")))}
            except FolderError as exc:
                self._send_json({"error": str(exc)}, status=400)
                return
            if body.get("classification") not in (None, "organization", "synthetic"):
                self._send_json({"error": "classification must be 'organization' or 'synthetic'"}, status=400)
                return
            if body.get("use_llm") not in (None, True, False):
                self._send_json({"error": "use_llm must be true or false"}, status=400)
                return

        job_id, error = runner.start_job(str(body.get("phase") or ""), body)
        if error:
            self._send_json({"error": error}, status=409 if "running" in error else 400)
        else:
            self._send_json({"job_id": job_id}, status=202)

    def _handle_bulk_ingest_get(self, path: str) -> None:
        """Allowed folders (with recent batches), or one staged batch for review."""
        from contingency_atlas.web import bulk_ingest_web as bulk

        settings = get_settings()
        try:
            if path == "/api/bulk-ingest/folders":
                self._send_json(bulk.list_ingest_folders(settings))
                return
            query = parse_qs(urlparse(self.path).query)
            batch_id = str(query.get("id", [""])[0])
            self._send_json(bulk.load_batch_for_review(settings, batch_id))
        except bulk.BatchError as exc:
            self._send_json({"error": str(exc)}, status=404)
        except (OSError, ValueError):
            logger.exception("bulk ingest read failed")
            self._send_json({"error": "Bulk ingest data is unavailable"}, status=503)

    def _handle_bulk_ingest_post(self, path: str, body: dict[str, Any]) -> None:
        """Plan a selection, or submit the reviewed plan through the import gate."""
        from contingency_atlas.web import bulk_ingest_web as bulk

        settings = get_settings()
        try:
            if path == "/api/bulk-ingest/plan":
                payload = bulk.plan(settings, body.get("batch_id"), body.get("selection"))
            else:
                payload = bulk.submit(settings, body.get("batch_id"), body.get("selection"),
                                      actor=body.get("actor"), plan_digest=body.get("plan_digest"))
        except bulk.BatchError as exc:
            # Typed codes from the engine and the adapter; never inferred from message text.
            self._send_json({"error": str(exc), "code": exc.code}, status=bulk.http_status(exc))
            return
        except (OSError, ValueError, sqlite3.Error) as exc:
            # Import-gate refusals (disabled workspace, revision conflicts) are ValueErrors.
            self._send_json({"error": str(exc)}, status=400)
            return
        self._send_json(payload)

    def _handle_mitre_atlas_technique_search(self) -> None:
        """I expose a bounded search over the active, locally pinned catalog."""
        from contingency_atlas.frameworks import framework_status, search_techniques

        query = parse_qs(urlparse(self.path).query)
        text_query = str(query.get("q", [""])[0]).strip()
        platform = str(query.get("platform", [""])[0]).strip() or None
        maturity = str(query.get("maturity", [""])[0]).strip() or None
        if len(text_query) > 200:
            self._send_json({"error": "q may contain at most 200 characters"}, status=400)
            return
        try:
            limit = int(query.get("limit", [20])[0])
        except (TypeError, ValueError):
            self._send_json({"error": "limit must be an integer"}, status=400)
            return
        if not 1 <= limit <= 50:
            self._send_json({"error": "limit must be between 1 and 50"}, status=400)
            return
        settings = get_settings()
        try:
            status = framework_status(settings.scenario_db_path)
            techniques = search_techniques(
                settings.scenario_db_path,
                query=text_query,
                platform=platform,
                maturity=maturity,
                limit=limit,
            )
        except ValueError as exc:
            self._send_json({"error": str(exc)}, status=400)
            return
        except (OSError, sqlite3.Error):
            logger.exception("MITRE ATLAS technique search failed")
            self._send_json(
                {"error": "MITRE ATLAS technique search is unavailable"},
                status=500,
            )
            return
        active = status.get("active") or status.get("active_release")
        content_version = (
            active.get("content_version") if isinstance(active, dict) else active
        )
        self._send_json({
            "content_version": content_version,
            "techniques": techniques,
            "count": len(techniques),
        })

    def _handle_mitre_atlas_proposal(self, body: dict[str, Any]) -> None:
        """I create a pending mapping only; this endpoint cannot approve it."""
        from contingency_atlas.frameworks.mappings import create_mapping_proposal

        scenario_id = str(body.get("scenario_id") or "").strip()
        technique_id = str(body.get("technique_id") or "").strip()
        rationale = str(body.get("rationale") or "").strip()
        content_version = str(body.get("content_version") or "").strip() or None
        if not scenario_id or not technique_id or not rationale:
            self._send_json(
                {"error": "scenario_id, technique_id, and rationale are required"},
                status=400,
            )
            return
        if len(scenario_id) > 160 or len(technique_id) > 32 or len(rationale) > 4000:
            self._send_json({"error": "mapping proposal fields exceed their limits"}, status=400)
            return
        try:
            proposal = create_mapping_proposal(
                get_settings().scenario_db_path,
                scenario_id,
                technique_id,
                rationale,
                content_version=content_version,
            )
        except ValueError as exc:
            message = str(exc)
            status = 409 if "active" in message.lower() or "pending" in message.lower() else 400
            self._send_json({"error": message}, status=status)
            return
        except (OSError, sqlite3.Error):
            logger.exception("MITRE ATLAS mapping proposal intake failed")
            self._send_json(
                {"error": "MITRE ATLAS mapping proposal intake is unavailable"},
                status=500,
            )
            return
        self._send_json({
            "ok": True,
            "proposal_id": proposal.get("proposal_id"),
            "proposal": proposal,
        }, status=201)

    def _handle_settings_atlas_import(self, body: dict[str, Any]) -> None:
        """Import a local MITRE ATLAS file. I never fetch the asserted URL."""
        from contingency_atlas.web.settings import (
            SettingsActionError,
            import_atlas_from_request,
        )

        try:
            payload = import_atlas_from_request(get_settings(), body)
        except SettingsActionError as exc:
            message = str(exc)
            status = 409 if "already pinned" in message.lower() else 400
            self._send_json({"error": message}, status=status)
            return
        except (OSError, sqlite3.Error):
            logger.exception("MITRE ATLAS settings import failed")
            self._send_json(
                {"error": "MITRE ATLAS catalog import is unavailable"},
                status=500,
            )
            return
        self._send_json(payload, status=201)

    def _handle_settings_atlas_activate(self, body: dict[str, Any]) -> None:
        from contingency_atlas.web.settings import (
            SettingsActionError,
            activate_atlas_from_request,
        )

        try:
            payload = activate_atlas_from_request(get_settings(), body)
        except SettingsActionError as exc:
            self._send_json({"error": str(exc)}, status=400)
            return
        except (OSError, sqlite3.Error):
            logger.exception("MITRE ATLAS settings activate failed")
            self._send_json(
                {"error": "MITRE ATLAS catalog activation is unavailable"},
                status=500,
            )
            return
        self._send_json(payload)

    def _handle_settings_models(self, body: dict[str, Any]) -> None:
        from contingency_atlas.web.settings import (
            SettingsActionError,
            apply_model_settings,
        )

        try:
            payload = apply_model_settings(get_settings(), body)
        except SettingsActionError as exc:
            message = str(exc)
            status = 409 if "environment" in message.lower() else 400
            self._send_json({"error": message}, status=status)
            return
        except ValueError as exc:
            self._send_json({"error": str(exc)}, status=400)
            return
        except OSError:
            logger.exception("Settings model write failed")
            self._send_json({"error": "Could not write the YAML config"}, status=500)
            return
        self._send_json(payload)

    def _handle_ingest_convert(self, body: dict[str, Any]) -> None:
        """Convert an uploaded artifact and extract an evidence DRAFT.

        Writes nothing: the response pre-fills the Phase 1 modal, the user
        reviews/edits, and only the existing /api/run phase1 path mutates
        stores (the review boundary stays in the already-audited flow).
        """
        from contingency_atlas.ingest_artifacts import (
            convert_to_markdown,
            decode_artifact,
            extract_evidence,
            rubric_check,
        )

        warnings: list[str] = []
        try:
            if isinstance(body.get("text"), str) and body["text"].strip():
                markdown = body["text"]
                converter = "passthrough"
                filename = "(pasted text)"
            else:
                filename = str(body.get("filename") or "artifact")
                content_b64 = body.get("content_b64")
                if not isinstance(content_b64, str) or not content_b64:
                    self._send_json({"error": "content_b64 or text is required"}, status=400)
                    return
                try:
                    raw = decode_artifact(filename, content_b64)
                except (ValueError, Exception) as exc:
                    self._send_json({"error": f"could not decode artifact: {exc}"}, status=400)
                    return
                conv = convert_to_markdown(filename, raw)
                markdown = conv.markdown
                converter = conv.converter
                warnings.extend(conv.warnings)

            result: dict[str, Any] = {
                "filename": filename,
                "converter": converter,
                "warnings": warnings,
                "markdown_preview": markdown[:2000],
                "source_text": "",
                "sources": [],
                "rubric": None,
            }
            if not markdown.strip():
                self._send_json(result)
                return

            if runner.omlx_reachable():
                from contingency_atlas.llm import OMLXClient
                from contingency_atlas.scoring.telemetry import production_telemetry

                mode = str(body.get("mode") or "evidence")
                if mode not in ("evidence", "intelligence"):
                    self._send_json(
                        {"error": "mode must be 'evidence' or 'intelligence'"}, status=400
                    )
                    return
                client = OMLXClient(
                    get_settings(),
                    telemetry=production_telemetry(),
                    node="ingest_convert",
                )
                try:
                    extraction = extract_evidence(markdown, client, mode=mode)
                    failed = int(extraction.get("chunks_failed") or 0)
                    result.update(
                        {
                            "source_text": extraction["source_text"],
                            "sources": extraction["sources"],
                            # The paired lane is the only one that may fill the
                            # form: Phase 1 rejects any run where the block
                            # count and the URL count disagree.
                            "source_urls": extraction["source_urls"],
                            "unattributed": [
                                r["text"] for r in extraction.get("unattributed", [])
                            ],
                            "orphan_urls": extraction.get("orphan_urls", []),
                            "chunks_processed": extraction["chunks_processed"],
                            "chunks_failed": failed,
                            "truncated": extraction["truncated"],
                            "mode": mode,
                        }
                    )
                    # Both _texts and _urls truncate to MAX_EVIDENCE_TEXTS, so
                    # the counts still agree and the run succeeds — having
                    # quietly discarded the overflow. Say so, rather than let a
                    # partial ingest look like a complete one.
                    if len(extraction["source_urls"]) > runner.MAX_EVIDENCE_TEXTS:
                        warnings.append(
                            f"{len(extraction['source_urls'])} records extracted but "
                            f"Phase 1 accepts {runner.MAX_EVIDENCE_TEXTS} per run — "
                            f"only the first {runner.MAX_EVIDENCE_TEXTS} will be ingested"
                        )
                    if failed:
                        # chunks_processed counts chunks ATTEMPTED, so without
                        # this the modal reports "7 chunks" for a run where all
                        # seven failed — indistinguishable from a document that
                        # genuinely held no evidence. The count was already
                        # computed by merge_extractions and dropped here.
                        warnings.append(
                            f"{failed} of {extraction['chunks_processed']} sections "
                            f"failed to extract — the evidence below is incomplete"
                        )
                    result["rubric"] = rubric_check(markdown, extraction, client)
                finally:
                    client.close()
            else:
                from contingency_atlas.ingest_artifacts import harvest_literal_urls

                literal = harvest_literal_urls(markdown)
                result["sources"] = [
                    {"publisher": "", "reference": "", "url": u, "origin": "literal",
                     "confidence": "high", "note": "link in artifact"}
                    for u in literal
                ]
                result["source_text"] = markdown[:4000]
                # Without extraction nothing pairs text to a link, and the URL
                # field must stay empty rather than be filled with every link
                # found: the body is one block, so N URLs would guarantee the
                # 1:1 rejection. They are offered as orphans to pair by hand.
                result["source_urls"] = []
                result["orphan_urls"] = literal
                result["unattributed"] = []
                warnings.append(
                    "OMLX offline: semantic extraction skipped — the body is the raw "
                    f"text preview and the {len(literal)} link(s) found are listed "
                    "below unpaired, since nothing can attribute them to a claim"
                )
            self._send_json(result)
        except Exception as exc:  # defensive: conversion must not 500 the dashboard
            logger.exception("ingest convert failed")
            self._send_json({"error": f"conversion failed: {exc}"}, status=500)

    def _handle_intake_sources(self) -> None:
        """Report optional adjacent sources without connecting credentials to the UI."""
        try:
            from contingency_atlas.singularity_source import probe_singularity_atlas

            probe = probe_singularity_atlas()
            payload = {"singularity_atlas": probe.to_dict()}
        except Exception:
            logger.exception("optional Singularity Atlas probe failed")
            payload = {
                "singularity_atlas": {
                    "available": False,
                    "reason": "probe failed",
                    "uri": "",
                    "driver_installed": False,
                    "loopback": True,
                }
            }
        self._send_json(payload)

    def _handle_intake_prepare(self, body: dict[str, Any]) -> None:
        """Prepare one mixed research draft without mutating governed stores."""
        from contingency_atlas.llm import OMLXClient
        from contingency_atlas.schema import migrate, migrate_evidence_events
        from contingency_atlas.scoring.telemetry import production_telemetry
        from contingency_atlas.web.intake_flow import prepare_research_material

        material = body.get("research_material")
        include_singularity = bool(body.get("include_singularity_atlas"))
        if material is None:
            material = ""
        if not isinstance(material, str):
            self._send_json(
                {"error": "research_material must be a string"},
                status=400,
            )
            return
        if not material.strip() and not include_singularity:
            self._send_json(
                {"error": "research_material must be a non-empty string"},
                status=400,
            )
            return
        settings = get_settings()
        if not runner.omlx_reachable(settings):
            self._send_json(
                {
                    "error": (
                        f"LLM backend ({settings.llm_provider}) is unavailable; "
                        "start it before preparing research."
                    )
                },
                status=503,
            )
            return
        try:
            migrate(str(settings.scenario_db_path))
            migrate_evidence_events(str(settings.evidence_db_path))
            client = OMLXClient(
                settings,
                telemetry=production_telemetry(),
                node="intake_prepare",
            )
            try:
                payload = prepare_research_material(
                    material,
                    settings=settings,
                    client=client,
                    include_singularity_atlas=include_singularity,
                )
            finally:
                client.close()
        except (TypeError, ValueError) as exc:
            self._send_json({"error": str(exc)}, status=400)
            return
        except Exception:
            logger.exception("research intake preparation failed")
            self._send_json(
                {"error": "Research preparation failed; no records were saved."},
                status=500,
            )
            return
        self._send_json(payload, status=201)

    def _handle_intake_submit(self, body: dict[str, Any]) -> None:
        """Queue selected prepared items as pending evidence and risks."""
        from contingency_atlas.schema import migrate, migrate_evidence_events
        from contingency_atlas.web.intake_flow import (
            IntakeFlowError,
            submit_draft_to_review,
        )

        draft_id = str(body.get("draft_id") or "").strip()
        selected_ids = body.get("selected_ids")
        if not draft_id:
            self._send_json({"error": "draft_id is required"}, status=400)
            return
        if not isinstance(selected_ids, list):
            self._send_json({"error": "selected_ids must be a list"}, status=400)
            return
        settings = get_settings()
        try:
            migrate(str(settings.scenario_db_path))
            migrate_evidence_events(str(settings.evidence_db_path))
            payload = submit_draft_to_review(
                draft_id,
                selected_ids,
                settings=settings,
            )
        except IntakeFlowError as exc:
            self._send_json({"error": str(exc)}, status=409)
            return
        except (OSError, sqlite3.Error, ValueError) as exc:
            logger.warning("research intake submit rejected: %s", exc)
            self._send_json(
                {"error": f"Could not queue the selected draft: {exc}"},
                status=400,
            )
            return
        except Exception:
            logger.exception("research intake submit failed")
            self._send_json(
                {"error": "Research submission failed; inspect Review for partial results."},
                status=500,
            )
            return
        self._send_json(payload, status=201)

    def _handle_scenario_recommendation(self) -> None:
        """Recommend scenario coverage from approved records only."""
        from contingency_atlas.coverage import recommend_scenario_coverage
        from contingency_atlas.db import list_evidence_events, list_scenarios
        from contingency_atlas.phases.phase2_risks import load_emerging_risks

        settings = get_settings()
        try:
            recommendation = recommend_scenario_coverage(
                list_evidence_events(str(settings.evidence_db_path)),
                load_emerging_risks(settings.emerging_risks_path),
                list_scenarios(str(settings.scenario_db_path), status=None),
            ).to_dict()
        except (OSError, sqlite3.Error, ValueError):
            logger.exception("scenario coverage recommendation failed")
            self._send_json(
                {"error": "Approved-corpus coverage recommendation is unavailable."},
                status=500,
            )
            return
        recommendation.update(
            {
                "recommended_count": recommendation["recommended"],
                "based_on": "approved_corpus",
                "uncovered_clusters": recommendation["basis"]["uncovered_risks"],
            }
        )
        self._send_json(recommendation)

    def _apply_review_decision(self, body: dict[str, Any]) -> dict[str, Any]:
        """Apply one already shape-checked decision through the audited flow.

        Both the single-record and batch handlers call this method so their
        attestation, validation, persistence, locking, and agreement-audit
        semantics cannot drift apart. ``ReviewError`` and unexpected errors
        deliberately escape for the HTTP handlers to report.
        """
        from contingency_atlas.review import (
            approve_analogue,
            approve_channel_proposal,
            approve_evidence,
            approve_merge,
            approve_mitre_atlas_mapping,
            approve_risk,
            reject_analogue,
            reject_channel_proposal,
            reject_evidence,
            reject_merge,
            reject_mitre_atlas_mapping,
            reject_risk,
        )
        from contingency_atlas.review_assist import record_decision

        kind = str(body.get("kind") or "")
        action = str(body.get("action") or "")
        record_id = str(body.get("record_id") or "").strip()
        settings = get_settings()
        note = str(body.get("note") or "")
        reason = str(body.get("reason") or "")
        reviewer = str(body.get("reviewer") or "")
        record_reviewer = reviewer.strip() or "local-operator"
        expected_revision = body.get("expected_revision")
        # Attestations are an explicit human gate. JSON truthiness (for
        # example, the string "false" or the number 1) must not satisfy it.
        public_ok = body.get("confirm_public_source") is True
        rights_ok = body.get("confirm_rights") is True
        if kind == "event" and action == "approve":
            record = approve_evidence(
                settings.evidence_db_path, record_id,
                public_source_attested=public_ok,
                rights_attested=rights_ok, note=note,
                reviewer=record_reviewer, expected_revision=expected_revision,
            )
        elif kind == "event":
            record = reject_evidence(
                settings.evidence_db_path, record_id, reason=reason,
                reviewer=record_reviewer, expected_revision=expected_revision,
            )
        elif kind == "merge" and action == "approve":
            # ADR-014 path: single reviewer + written reason; the register
            # merge runs under the register lock inside dedup.apply_merge.
            record = approve_merge(
                settings.scenario_db_path,
                settings.emerging_risks_path,
                record_id,
                reviewer=reviewer,
                reason=reason or note,
            )
        elif kind == "merge":
            record = reject_merge(
                settings.scenario_db_path, record_id,
                reviewer=reviewer,
                reason=reason or note,
            )
        elif kind == "channel" and action == "approve":
            # Approval flips the proposal row only; production is untouched.
            record = approve_channel_proposal(
                settings.scenario_db_path, record_id,
                reviewer=reviewer,
                reason=reason or note,
            )
        elif kind == "channel":
            record = reject_channel_proposal(
                settings.scenario_db_path, record_id,
                reviewer=reviewer,
                reason=reason or note,
            )
        elif kind == "analogue" and action == "approve":
            record = approve_analogue(
                settings.scenario_db_path, record_id,
                reviewer=reviewer,
                reason=reason or note,
            )
        elif kind == "analogue":
            record = reject_analogue(
                settings.scenario_db_path, record_id,
                reviewer=reviewer,
                reason=reason or note,
            )
        elif kind == "mitre_atlas" and action == "approve":
            record = approve_mitre_atlas_mapping(
                settings.scenario_db_path, record_id,
                reviewer=reviewer,
                reason=reason or note,
            )
        elif kind == "mitre_atlas":
            record = reject_mitre_atlas_mapping(
                settings.scenario_db_path, record_id,
                reviewer=reviewer,
                reason=reason or note,
            )
        elif action == "approve":
            record = approve_risk(
                settings.emerging_risks_path, record_id,
                evidence_db=settings.evidence_db_path,
                public_source_attested=public_ok,
                rights_attested=rights_ok, note=note,
                reviewer=record_reviewer, expected_revision=expected_revision,
            )
        else:
            record = reject_risk(
                settings.emerging_risks_path, record_id, reason=reason,
                reviewer=record_reviewer, expected_revision=expected_revision,
            )

        try:  # best-effort agreement audit; the stores above are authoritative
            record_decision(settings.scenario_db_path, kind, record_id, action)
        except Exception:
            logger.exception("could not record review decision in suggestion log")
        return record

    @staticmethod
    def _review_error_status(message: str) -> int:
        return 409 if any(word in message.lower() for word in ("locked", "changed after you opened")) else 400

    def _handle_review_decide(self, body: dict[str, Any]) -> None:
        """Apply one human review decision through the audited review flow."""
        from contingency_atlas.review import ReviewError

        kind = str(body.get("kind") or "")
        action = str(body.get("action") or "")
        record_id = str(body.get("record_id") or "").strip()
        if kind not in ("event", "risk", "merge", "channel", "analogue", "mitre_atlas"):
            self._send_json(
                {"error": "kind must be 'event', 'risk', 'merge', 'channel' "
                          "'analogue' or 'mitre_atlas'"}, status=400
            )
            return
        if action not in ("approve", "reject"):
            self._send_json(
                {"error": "action must be 'approve' or 'reject'"}, status=400
            )
            return
        if not record_id:
            self._send_json({"error": "record_id is required"}, status=400)
            return

        try:
            record = self._apply_review_decision({**body, "record_id": record_id})
        except ReviewError as exc:
            message = str(exc)
            self._send_json(
                {"error": message}, status=self._review_error_status(message)
            )
            return
        except Exception as exc:  # defensive: a decision must never 500 silently
            logger.exception("review decision failed")
            self._send_json({"error": f"decision failed: {exc}"}, status=500)
            return

        self._send_json({"ok": True, "kind": kind, "action": action, "record": record})

    def _handle_review_decide_batch(self, body: dict[str, Any]) -> None:
        """Apply up to 500 decisions sequentially, with per-item outcomes.

        The batch is deliberately non-atomic: records can span two SQLite
        databases and the atomically replaced emerging-risk JSON register.
        Shape and shared-gate validation finish before the first mutation;
        once execution starts, successful earlier decisions remain durable if
        a later item fails.
        """
        from contingency_atlas.review import ReviewError

        kinds = ("event", "risk", "merge", "channel", "analogue", "mitre_atlas")
        action = str(body.get("action") or "")
        if action not in ("approve", "reject"):
            self._send_json(
                {"error": "action must be 'approve' or 'reject'"}, status=400
            )
            return

        raw_items = body.get("items")
        if not isinstance(raw_items, list) or not raw_items:
            self._send_json({"error": "items must be a non-empty array"}, status=400)
            return
        if len(raw_items) > 500:
            self._send_json(
                {"error": "items may contain at most 500 records"}, status=400
            )
            return

        items: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for index, item in enumerate(raw_items):
            if not isinstance(item, dict):
                self._send_json(
                    {"error": f"items[{index}] must be an object"}, status=400
                )
                return
            kind_value = item.get("kind")
            record_id_value = item.get("record_id")
            if not isinstance(kind_value, str) or kind_value not in kinds:
                self._send_json(
                    {"error": f"items[{index}].kind must be 'event', 'risk', "
                              "'merge', 'channel', 'analogue' or 'mitre_atlas'"},
                    status=400,
                )
                return
            if not isinstance(record_id_value, str) or not record_id_value.strip():
                self._send_json(
                    {"error": f"items[{index}].record_id is required"}, status=400
                )
                return
            record_id = record_id_value.strip()
            key = (kind_value, record_id)
            if key in seen:
                self._send_json(
                    {"error": f"duplicate review item: {kind_value}:{record_id}"},
                    status=400,
                )
                return
            seen.add(key)
            items.append({"kind": kind_value, "record_id": record_id,
                          "expected_revision": item.get("expected_revision")})

        note = str(body.get("note") or "").strip()
        reason = str(body.get("reason") or "").strip()
        # ``note`` is the batch API's canonical shared field. ``reason`` is
        # accepted as a compatibility fallback for callers modelled on the
        # single-record endpoint, but mixed record kinds must never receive
        # different text from one request.
        decision_note = note or reason
        reviewer = str(body.get("reviewer") or "").strip()
        proposal_selected = any(
            item["kind"] in ("merge", "channel", "analogue", "mitre_atlas") for item in items
        )
        evidence_selected = any(item["kind"] in ("event", "risk") for item in items)

        if action == "reject" and not decision_note:
            self._send_json({"error": "Batch rejection requires a note"}, status=400)
            return
        if proposal_selected and (not reviewer or not decision_note):
            self._send_json(
                {"error": "Batch decisions for merge, channel, analogue or MITRE ATLAS records "
                          "require a reviewer and note"},
                status=400,
            )
            return
        if action == "approve" and evidence_selected and not (
            body.get("confirm_public_source") is True
            and body.get("confirm_rights") is True
        ):
            self._send_json(
                {"error": "Batch approval of evidence or risks requires explicit "
                          "public-source and rights attestations"},
                status=400,
            )
            return

        shared = {
            "action": action,
            "note": decision_note,
            # A common reviewer note is also the rejection/written reason.
            "reason": decision_note,
            "reviewer": reviewer,
            "confirm_public_source": body.get("confirm_public_source") is True,
            "confirm_rights": body.get("confirm_rights") is True,
        }
        results: list[dict[str, Any]] = []
        for item in items:
            try:
                record = self._apply_review_decision({**shared, **item})
            except ReviewError as exc:
                message = str(exc)
                results.append({
                    "ok": False,
                    **item,
                    "error": message,
                    "status": self._review_error_status(message),
                })
            except Exception as exc:  # one bad item must not hide later outcomes
                logger.exception(
                    "batch review decision failed for %s:%s",
                    item["kind"], item["record_id"],
                )
                results.append({
                    "ok": False,
                    **item,
                    "error": f"decision failed: {exc}",
                    "status": 500,
                })
            else:
                results.append({"ok": True, **item, "record": record, "status": 200})

        succeeded = sum(1 for result in results if result["ok"])
        failed = len(results) - succeeded
        self._send_json(
            {
                "ok": failed == 0,
                "action": action,
                "total": len(results),
                "succeeded": succeeded,
                "failed": failed,
                "results": results,
            },
            status=200 if failed == 0 else 207,
        )

    def _handle_review_adopt_refs(self, body: dict[str, Any]) -> None:
        """Attach APPROVED evidence event ids to a pending risk's refs.

        Deliberately narrow: only event ids that exist and are fully
        approved may be adopted, only onto a still-pending risk, under the
        register lock. This is how machine-found (or manually ingested)
        evidence becomes citable without hand-editing the register — the
        risk itself still goes through the normal attested approval after.
        """
        from contingency_atlas.db import get_evidence_event
        from contingency_atlas.phases.phase2_risks import (
            load_emerging_risks,
            save_emerging_risks,
        )
        from contingency_atlas.review import ReviewError, register_lock

        record_id = str(body.get("record_id") or "").strip()
        event_ids = body.get("event_ids")
        if not record_id:
            self._send_json({"error": "record_id is required"}, status=400)
            return
        if not isinstance(event_ids, list) or not event_ids:
            self._send_json({"error": "event_ids must be a non-empty list"}, status=400)
            return
        event_ids = [str(e).strip() for e in event_ids if str(e).strip()][:20]

        settings = get_settings()
        rejected = []
        for event_id in event_ids:
            row = get_evidence_event(str(settings.evidence_db_path), event_id)
            if row is None:
                rejected.append(f"{event_id}: no such evidence event")
            elif row.get("review_status") != "approved":
                rejected.append(
                    f"{event_id}: review_status is "
                    f"{row.get('review_status')!r} — only approved evidence "
                    "can be adopted as a ref"
                )
        if rejected:
            self._send_json({"error": "; ".join(rejected)}, status=400)
            return

        try:
            with register_lock(settings.emerging_risks_path):
                risks = load_emerging_risks(settings.emerging_risks_path)
                index = next(
                    (i for i, r in enumerate(risks)
                     if isinstance(r, dict)
                     and str(r.get("issue") or r.get("title") or "") == record_id),
                    None,
                )
                if index is None:
                    self._send_json(
                        {"error": f"no emerging risk with issue {record_id!r}"},
                        status=404,
                    )
                    return
                record = dict(risks[index])
                if str(record.get("review_status") or "") != "pending":
                    self._send_json(
                        {"error": "refs can only be adopted onto a pending risk"},
                        status=400,
                    )
                    return
                refs = [str(r) for r in (record.get("evidence_refs") or [])]
                added = [e for e in event_ids if e not in refs]
                record["evidence_refs"] = refs + added
                if added:
                    note = (record.get("provenance_note") or "").strip()
                    stamp = (
                        f"Evidence refs adopted after human approval of the "
                        f"cited events: {', '.join(added)}."
                    )
                    record["provenance_note"] = f"{note} | {stamp}" if note else stamp
                risks[index] = record
                save_emerging_risks(settings.emerging_risks_path, risks)
        except ReviewError as exc:
            message = str(exc)
            status = 409 if "locked" in message.lower() else 400
            self._send_json({"error": message}, status=status)
            return

        self._send_json({"ok": True, "added": added, "record": record})

    def _handle_review_detach_refs(self, body: dict[str, Any]) -> None:
        """Detach refs from a pending risk's evidence_refs.

        The mirror of adopt-refs, and the only sanctioned way to REMOVE
        refs (e.g. URLs a monitoring cycle unioned in from the broader
        source packet without bearing on this risk). Any ref value may be
        detached — junk refs are precisely the ones that resolve to
        nothing. Still-pending risks only, under the register lock, with
        a provenance stamp; idempotent — absent refs are skipped.
        """
        from contingency_atlas.categories import derive_evidence_strength
        from contingency_atlas.phases.phase2_risks import (
            load_emerging_risks,
            save_emerging_risks,
        )
        from contingency_atlas.review import ReviewError, register_lock

        record_id = str(body.get("record_id") or "").strip()
        refs = body.get("refs")
        if not record_id:
            self._send_json({"error": "record_id is required"}, status=400)
            return
        if not isinstance(refs, list) or not refs:
            self._send_json({"error": "refs must be a non-empty list"}, status=400)
            return
        detach = {str(r).strip() for r in refs if str(r).strip()}
        if not detach:
            self._send_json({"error": "refs must be a non-empty list"}, status=400)
            return

        settings = get_settings()
        try:
            with register_lock(settings.emerging_risks_path):
                risks = load_emerging_risks(settings.emerging_risks_path)
                index = next(
                    (i for i, r in enumerate(risks)
                     if isinstance(r, dict)
                     and str(r.get("issue") or r.get("title") or "") == record_id),
                    None,
                )
                if index is None:
                    self._send_json(
                        {"error": f"no emerging risk with issue {record_id!r}"},
                        status=404,
                    )
                    return
                record = dict(risks[index])
                if str(record.get("review_status") or "") != "pending":
                    self._send_json(
                        {"error": "refs can only be detached from a pending risk"},
                        status=400,
                    )
                    return
                current = [str(r) for r in (record.get("evidence_refs") or [])]
                removed = [r for r in current if r in detach]
                record["evidence_refs"] = [r for r in current if r not in detach]
                if removed:
                    # evidence_strength is derived from the refs — recompute
                    # rather than leave a stale strength on a pruned list
                    record["evidence_strength"] = derive_evidence_strength(
                        record["evidence_refs"]
                    )
                    note = (record.get("provenance_note") or "").strip()
                    stamp = (
                        f"Evidence refs detached by human review: "
                        f"{', '.join(removed)}."
                    )
                    record["provenance_note"] = f"{note} | {stamp}" if note else stamp
                risks[index] = record
                save_emerging_risks(settings.emerging_risks_path, risks)
        except ReviewError as exc:
            message = str(exc)
            status = 409 if "locked" in message.lower() else 400
            self._send_json({"error": message}, status=status)
            return

        self._send_json({"ok": True, "removed": removed, "record": record})

    def _handle_rate_memo(self, body: dict[str, Any]) -> None:
        """Record the reviewer's boilerplate rating for a refutation memo.

        P6 spot audit: display-artifact rating only — this mutates no
        review state and no score, so it needs no attestation; the
        loopback gate above still applies.
        """
        from contingency_atlas.refutations import rate_memo

        memo_id = str(body.get("memo_id") or "").strip()
        if not memo_id:
            self._send_json({"error": "memo_id is required"}, status=400)
            return
        try:
            result = rate_memo(
                get_settings().scenario_db_path, memo_id, body.get("rating")
            )
        except ValueError as exc:
            self._send_json({"error": str(exc)}, status=400)
            return
        except Exception as exc:  # defensive: never a silent 500
            logger.exception("memo rating failed")
            self._send_json({"error": f"rating failed: {exc}"}, status=500)
            return
        self._send_json({"ok": True, **result})

    def _handle_scan_duplicates(self, body: dict[str, Any]) -> None:
        """P8 duplicate scan of the governed register -> pending merge proposals.

        Synchronous and deterministic (token-Jaccard, no LLM). When OMLX
        is reachable, an advisory verdict is attached to each NEW proposal
        — best-effort and never blocking: ``advise_merge`` records model
        failures instead of raising, and an unexpected per-proposal error
        is logged and skipped. Every merge stays a human decision in the
        review queue (ADR-014); this endpoint only proposes.
        """
        from contingency_atlas import proposals
        from contingency_atlas.dedup import candidate_duplicates

        settings = get_settings()
        risks: list[dict[str, Any]] = []
        if settings.emerging_risks_path.exists():
            try:
                data = json.loads(settings.emerging_risks_path.read_text())
                if isinstance(data, list):
                    risks = [r for r in data if isinstance(r, dict)]
            except (json.JSONDecodeError, OSError):
                risks = []

        candidates = candidate_duplicates(risks)
        stored = proposals.create_merge_proposals(
            settings.scenario_db_path, candidates
        )

        advisories = 0
        if stored["proposal_ids"] and runner.omlx_reachable(settings):
            from contingency_atlas.llm import OMLXClient
            from contingency_atlas.scoring.telemetry import production_telemetry

            # advise_merge quotes the two descriptions; proposal rows store
            # titles only — enrich from the register (first title wins).
            desc_by_title: dict[str, str] = {}
            for record in risks:
                title = str(record.get("issue") or record.get("title") or "")
                if title and title not in desc_by_title:
                    desc_by_title[title] = str(record.get("description") or "")
            client = OMLXClient(
                settings, telemetry=production_telemetry(), node="merge_advisory"
            )
            try:
                for proposal_id in stored["proposal_ids"]:
                    try:
                        proposal = proposals.get_proposal(
                            settings.scenario_db_path, proposal_id
                        )
                        if not proposal:
                            continue
                        advisory = proposals.advise_merge(
                            client,
                            dict(
                                proposal,
                                description_a=desc_by_title.get(
                                    str(proposal.get("title_a") or ""), ""
                                ),
                                description_b=desc_by_title.get(
                                    str(proposal.get("title_b") or ""), ""
                                ),
                            ),
                        )
                        proposals.record_advisory(
                            settings.scenario_db_path, proposal_id, advisory
                        )
                        advisories += 1
                    except Exception:  # advisory only — never block the scan
                        logger.exception("merge advisory failed for %s", proposal_id)
            finally:
                client.close()

        self._send_json({
            "n_scanned": len(risks),
            "n_candidates": len(candidates),
            "n_created": stored["created"],
            "n_already_pending": stored["skipped_existing_pending"],
            "advisories_attached": advisories,
        })

    def _mutation_request_allowed(self) -> bool:
        """Allow state-changing calls only from the loopback dashboard origin."""
        from contingency_atlas.web.access import AccessError, allowed_origin, load_access_config
        try:
            access = load_access_config()
            if access.mode == "proxy":
                return allowed_origin(self, config=access)
        except AccessError:
            return False
        if not self._local_request_allowed():
            return False

        host = self.headers.get("Host", "").strip().lower()
        origin = self.headers.get("Origin")
        if not origin:
            return True
        try:
            parsed = urlparse(origin)
        except ValueError:
            return False
        return (
            parsed.scheme.lower() in {"http", "https"}
            and parsed.username is None
            and parsed.password is None
            and parsed.netloc.lower() == host
            and self._loopback_netloc(parsed.netloc)
        )

    def _local_request_allowed(self) -> bool:
        """Require both a loopback peer and a loopback Host header."""
        from contingency_atlas.web.access import AccessError, load_access_config, peer_request_allowed
        try:
            access = load_access_config()
            if access.mode == "proxy":
                return peer_request_allowed(self, config=access)
        except AccessError:
            return False
        try:
            peer = ipaddress.ip_address(self.client_address[0])
        except (ValueError, IndexError):
            return False
        if not peer.is_loopback:
            return False

        host = self.headers.get("Host", "").strip().lower()
        if not host or not self._loopback_netloc(host):
            return False
        return True

    def _authorize(self, path: str, method: str, body: bytes | None = None) -> bool:
        from contingency_atlas.web.access import AccessError, authorize_request
        try:
            self.atlas_principal = authorize_request(self, path, method, body)
            self.atlas_actor = self.atlas_principal.actor
            return True
        except AccessError as exc:
            self._send_json({"error": str(exc)}, status=exc.status)
            return False

    @staticmethod
    def _loopback_netloc(netloc: str) -> bool:
        try:
            parsed = urlparse(f"//{netloc}")
            hostname = parsed.hostname
            _ = parsed.port
        except ValueError:
            return False
        if not hostname:
            return False
        if hostname.lower() == "localhost":
            return True
        try:
            return ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            return False

    def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path, content_type: str) -> None:
        try:
            body = path.read_bytes()
        except OSError:
            self.send_error(404, "Not found")
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        super().end_headers()

    def log_message(self, fmt: str, *args: Any) -> None:
        # Quiet by default; uncomment for debugging.
        return


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Contingency Atlas dashboard server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args(argv)
    from contingency_atlas.web.access import AccessError, load_access_config
    try:
        load_access_config()
    except AccessError as exc:
        parser.error(str(exc))
    if not _is_loopback_bind_host(args.host):
        parser.error(
            "--host must be a loopback address or localhost; "
            "the dashboard is intentionally local-only"
        )

    try:
        server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            print(f"Port {args.port} is already in use — a dashboard instance is most likely")
            print(f"already running at http://{args.host}:{args.port}")
            print(f"(or pick another port: python -m contingency_atlas.web --port {args.port + 1})")
            raise SystemExit(1) from None
        raise
    url = f"http://{args.host}:{args.port}"
    print(f"Contingency Atlas dashboard → {url}  (Ctrl+C to stop)")
    runner.recover_jobs()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
