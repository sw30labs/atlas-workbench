"""P7 judgment ledger — prospective claims, frozen forecasts, resolutions
(HYBRID_SCORING_PROPOSAL.md section P7; frozen-forecast rationale in Part C).

A claim is resolvable only if the forecast object it is scored against was
frozen BEFORE the resolving window opened (Part C). Every ledgered claim
therefore carries a ``frozen_forecast``: the deterministic realization
as-published, numbers carried verbatim from ``compound_scenarios`` — never
recomputed. Judgment never edits it; resolutions are appended as new
linked entries and claim rows themselves are immutable.

The ledger is an append-only, hash-chained JSONL log — the same
prev_hash/entry_hash pattern as the egress audit log
(``scoring/egress.py:127-217``), generalized into :func:`append_entry` /
:func:`verify_chain` here; egress itself is untouched.

Row shapes. Claim rows (``kind`` one of :data:`CLAIM_KINDS`)::

    {entry_id, ts, kind, elicitor, claim, claim_hash, frozen_forecast,
     human_decision, resolution: None, resolution_ts: None,
     prev_hash, entry_hash}

Resolution rows (claims are never edited — resolution state lives only in
these linked rows)::

    {entry_id, ts, kind: "resolution", resolves_entry_id, resolution,
     resolution_ts, prev_hash, entry_hash}

Display-only: nothing in this module touches a score. The ledger is the
calibration instrument for the future judgment-vs-deterministic
comparison (G-shadow), not an input to the scorer. Stdlib only.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .models import IncidentEvent, canonical_json, hash_object, sha256_hex

logger = logging.getLogger(__name__)

#: Ledger format version. Bump on any change to the row shapes above.
JUDGMENT_LEDGER_VERSION = 1

#: Frozen-forecast object version. Bump when the frozen object gains or
#: reinterprets fields (v1 fields are listed in :func:`freeze_forecast`).
FORECAST_DEF_VERSION = 1

#: Default ledger location (relative to the project root / cwd).
DEFAULT_LEDGER_PATH = Path("generated/judgment/ledger.jsonl")

#: Claim kinds, string-typed: "analogue" (P2 dossier claims), "elicitation"
#: (quantile elicitations, future), "canary" (contamination-canary rows,
#: future), "llm_join" (dark-matter joins from compound_join_llm — frozen
#: at proposal time with their falsifiable signatures).
CLAIM_KINDS = ("analogue", "elicitation", "canary", "llm_join")

#: Claim kinds the resolution job resolves against the activation series
#: (analogue dossier claims and dark-matter join claims — both pin risk_ids
#: and an all_must_fire outcome).
RESOLVABLE_CLAIM_KINDS = ("analogue", "llm_join")

#: Production horizon of the deterministic scorer (ScorerConfig default).
#: Pinned into every frozen forecast v1: compound_scenarios rows do not
#: store the horizon, and the production join runs the default config.
FORECAST_HORIZON_QUARTERS = 4

#: P7 feasibility kill rule: the prospective program is under-powered if
#: the ledger resolves fewer than ~30 claims per quarter (after 4 quarters).
FEASIBILITY_TARGET_PER_QUARTER = 30


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Hash-chained append-only log (egress pattern, generalized)
# ---------------------------------------------------------------------------


def _read_entries(path: str | Path) -> list[dict[str, Any]]:
    """All ledger rows in order; missing file -> []. Blank lines skipped."""
    path = Path(path)
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _last_entry_hash(path: Path) -> str:
    """Re-seed the chain from the last line — the egress startup pattern.

    Done on every append (not cached in process state), so two appends in
    separate processes on the same path still link up. A corrupt tail line
    raises here (json.loads) rather than silently forking the chain.
    """
    if not path.exists():
        return ""
    for line in reversed(path.read_text(encoding="utf-8").splitlines()):
        if line.strip():
            return str(json.loads(line)["entry_hash"])
    return ""


def append_entry(path: str | Path, entry: dict[str, Any]) -> dict[str, Any]:
    """Append one hash-chained row; returns the row with chain fields added.

    The entry gains ``prev_hash`` (re-seeded from the last line on first
    use, same pattern as ``egress.EgressGateway``) and
    ``entry_hash = sha256_hex(canonical_json(entry_with_prev))``; exactly
    one canonical-key-order JSON line is appended. Parent dirs are created.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {**entry, "prev_hash": _last_entry_hash(path)}
    row["entry_hash"] = sha256_hex(canonical_json(row))
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")
    return row


def verify_chain(path: str | Path) -> tuple[bool, str]:
    """Recompute the full chain. Returns ``(ok, detail)``.

    ``detail`` is human-readable: ``"ok (N entries)"`` or where and how the
    chain broke. A missing ledger is vacuously valid (0 entries), matching
    ``egress.verify_audit_chain``. Tampering with any byte of any earlier
    line fails verification — either the entry's own hash no longer matches
    its contents, or the next entry's ``prev_hash`` no longer links.
    """
    path = Path(path)
    if not path.exists():
        return True, "ledger does not exist (0 entries)"
    prev = ""
    n = 0
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        n += 1
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as exc:
            return False, f"line {lineno}: invalid JSON ({exc})"
        if entry.get("prev_hash") != prev:
            return False, f"line {lineno}: broken link (prev_hash mismatch)"
        claimed = entry.get("entry_hash")
        body = {k: v for k, v in entry.items() if k != "entry_hash"}
        if sha256_hex(canonical_json(body)) != claimed:
            return False, f"line {lineno}: entry_hash mismatch (contents altered)"
        prev = str(claimed)
    return True, f"ok ({n} entries)"


# ---------------------------------------------------------------------------
# Frozen deterministic forecasts (Part C: frozen BEFORE the resolving window)
# ---------------------------------------------------------------------------


def freeze_forecast(compound_row: dict[str, Any]) -> dict[str, Any]:
    """The frozen deterministic forecast object for one compound row.

    Carries the published realization's identity and numbers EXACTLY as
    stored in ``compound_scenarios`` — p_hat/p_lo/p_hi are copied, never
    recomputed. Judgment never edits this object; a future judgment-informed
    realization would be a separate frozen object, scored separately.

    ``horizon_quarters``/``compound_def`` are the production forecast
    definition (ScorerConfig default horizon, all_must_fire): the row does
    not store them, and v1 pins them so the resolution rule is unambiguous.
    """
    return {
        "forecast_def_version": FORECAST_DEF_VERSION,
        "pair_id": compound_row.get("pair_id"),
        "compound_id": compound_row.get("compound_id"),
        "state_hash": compound_row.get("state_hash"),
        "data_hash": compound_row.get("data_hash"),
        # config source fields present in the row:
        "algorithm_version": compound_row.get("algorithm_version"),
        "horizon_quarters": FORECAST_HORIZON_QUARTERS,
        "compound_def": "all_must_fire",
        "p_hat": compound_row.get("p_hat"),
        "p_interval": [compound_row.get("p_lo"), compound_row.get("p_hi")],
        "scored_at": compound_row.get("scored_at"),
    }


# ---------------------------------------------------------------------------
# Claims
# ---------------------------------------------------------------------------


def record_claim(
    path: str | Path,
    *,
    kind: str,
    elicitor: dict[str, Any],
    claim: dict[str, Any],
    frozen_forecast: dict[str, Any],
    human_decision: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append one claim row; returns the stored row (chain fields included).

    ``kind`` is one of :data:`CLAIM_KINDS`. ``claim_hash`` pins the claim
    bytes so later tooling can detect any drift between the review store
    and the ledger. ``resolution``/``resolution_ts`` start null and STAY
    null on the claim row — resolutions are separate linked entries.
    """
    if kind not in CLAIM_KINDS:
        raise ValueError(f"unknown claim kind {kind!r} (expected one of {CLAIM_KINDS})")
    row = {
        "entry_id": uuid.uuid4().hex[:12],
        "ts": _now(),
        "kind": kind,
        "elicitor": elicitor,
        "claim": claim,
        "claim_hash": hash_object(claim),
        "frozen_forecast": frozen_forecast,
        "human_decision": human_decision,
        "resolution": None,
        "resolution_ts": None,
    }
    return append_entry(path, row)


def _latest_compound_row(db_path: str | Path, pair_id: str) -> dict[str, Any] | None:
    """Latest-scored compound_scenarios row for a pair, or None.

    A missing db or table is recorded (None) rather than raised — the
    caller logs the skip reason. Direct sqlite read: the scoring core stays
    stdlib-only and does not import the join.
    """
    if not Path(db_path).is_file():
        return None
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if "compound_scenarios" not in tables:
            return None
        row = conn.execute(
            "SELECT * FROM compound_scenarios WHERE pair_id = ? "
            "ORDER BY scored_at DESC, rowid DESC LIMIT 1",
            (str(pair_id),),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def record_analogue_claims(
    db_path: str | Path,
    ledger_path: str | Path,
    *,
    elicitor_default: dict[str, Any] | None = None,
) -> int:
    """Ledger every approved analogue not yet recorded. Returns rows added.

    Idempotent: an analogue whose id already appears in a ledger row's
    ``claim.analogue_id`` is skipped. An approved analogue whose pair has
    NO deterministic realization in ``compound_scenarios`` is skipped with
    a logged reason — a claim without a frozen forecast is NOT ledgered,
    by design (Part C: unfrozen claims are unresolvable).

    The row's human decision (approver + decided_at) is carried into the
    ledger per P7 ("structured claim, human decision, resolution").
    """
    from ..proposals import list_analogues  # lazy: the store lives outside scoring/

    approved = list_analogues(db_path, status="approved")
    if not approved:
        return 0
    ledgered: set[str] = set()
    for entry in _read_entries(ledger_path):
        claim = entry.get("claim")
        if isinstance(claim, dict) and claim.get("analogue_id"):
            ledgered.add(str(claim["analogue_id"]))
    n_added = 0
    # deterministic append order: oldest review decision first
    for ana in sorted(approved, key=lambda a: (str(a.get("created_at")), str(a["analogue_id"]))):
        analogue_id = str(ana["analogue_id"])
        if analogue_id in ledgered:
            continue
        pair_id = str(ana.get("pair_id") or "")
        compound = _latest_compound_row(db_path, pair_id)
        if compound is None:
            logger.info(
                "analogue %s not ledgered: no deterministic realization "
                "(compound_scenarios row) for pair %s",
                analogue_id,
                pair_id,
            )
            continue
        claim = {
            "analogue_id": analogue_id,
            "pair_id": pair_id,
            "risk_ids": [str(r) for r in (ana.get("risk_ids") or [])],
            "event": ana.get("event"),
            "period": ana.get("period"),
            "co_fired": ana.get("co_fired"),
            "outcome_class": ana.get("outcome_class"),
        }
        elicitor = dict(elicitor_default or {})
        elicitor.update(ana.get("elicitor") or {})
        elicitor.setdefault("model_id", "unknown")
        elicitor.setdefault("prompt_version", None)
        elicitor.setdefault("prompt_hash", None)
        record_claim(
            ledger_path,
            kind="analogue",
            elicitor=elicitor,
            claim=claim,
            frozen_forecast=freeze_forecast(compound),
            human_decision={
                "decision": "approved",
                "reviewer": ana.get("reviewer"),
                "decided_at": ana.get("decided_at"),
            },
        )
        ledgered.add(analogue_id)
        n_added += 1
    return n_added


def record_join_claims(
    joins: list[dict[str, Any]],
    ledger_path: str | Path,
    *,
    elicitor: dict[str, Any] | None = None,
) -> int:
    """Pre-register dark-matter joins (compound_join_llm) as llm_join claims.

    Unlike analogue claims (ledgered after human approval), join claims are
    frozen AT PROPOSAL TIME: the falsifiable signature plus the engine's
    frozen forecast IS the bet, and freezing every scored join — not just
    the ones a reviewer liked — is the selection-bias defense (Part D: no
    just-so stories). ``human_decision`` stays null; the human decision
    lives on the channel_proposals row the join also files.

    Idempotent per (pair_id, signature): re-running the join re-freezes
    nothing. A join without an engine realization (``engine.p_hat`` None —
    e.g. an unfitted atom) is skipped with a logged reason: a claim without
    a frozen forecast is NOT ledgered (Part C: unfrozen claims are
    unresolvable). Returns rows added.
    """
    existing: set[tuple[str, str]] = set()
    for entry in _read_entries(ledger_path):
        if entry.get("kind") != "llm_join":
            continue
        claim = entry.get("claim") or {}
        existing.add(
            (str(claim.get("pair_id") or ""), str(claim.get("signature") or ""))
        )
    n_added = 0
    for join in sorted(
        joins, key=lambda j: (str(j.get("pair_id")), str(j.get("join_id")))
    ):
        engine = join.get("engine") or {}
        pair_id = str(join.get("pair_id") or "")
        signature = str(join.get("signature") or "")
        if engine.get("p_hat") is None:
            logger.info(
                "join %s not ledgered: no engine realization (frozen forecast) "
                "for pair %s",
                join.get("join_id"),
                pair_id,
            )
            continue
        key = (pair_id, signature)
        if key in existing:
            continue
        claim = {
            "join_id": join.get("join_id"),
            "pair_id": pair_id,
            "risk_ids": [str(r) for r in (join.get("risk_ids") or [])],
            "mechanism": join.get("mechanism"),
            "signature": signature,
            "disanalogy": join.get("disanalogy"),
        }
        frozen_row = {
            "pair_id": pair_id,
            "compound_id": join.get("join_id"),
            "state_hash": engine.get("state_hash"),
            "data_hash": engine.get("data_hash"),
            "algorithm_version": engine.get("algorithm_version"),
            "p_hat": engine.get("p_hat"),
            "p_lo": engine.get("p_lo"),
            "p_hi": engine.get("p_hi"),
            "scored_at": engine.get("scored_at"),
        }
        el = dict(elicitor or {})
        el.setdefault("model_id", "unknown")
        el.setdefault("prompt_version", None)
        el.setdefault("prompt_hash", None)
        record_claim(
            ledger_path,
            kind="llm_join",
            elicitor=el,
            claim=claim,
            frozen_forecast=freeze_forecast(frozen_row),
            human_decision=None,
        )
        existing.add(key)
        n_added += 1
    return n_added


# ---------------------------------------------------------------------------
# Resolution job (Part B job; quarterly)
# ---------------------------------------------------------------------------


def resolve_claims(
    ledger_path: str | Path,
    series_events: Iterable[IncidentEvent],
    *,
    current_period: int,
    train_end_period: int,
) -> dict[str, Any]:
    """Resolve open analogue/llm_join claims against the quarterly series.

    Window rule (documented v1 fallback): the frozen forecast object does
    not store train bounds, so ``train_end_period`` — the max quarterly
    index the join trained on — is passed in, never guessed. A claim is
    resolvable once at least one quarter with index > ``train_end_period``
    (and <= ``current_period``) exists in the series, and resolves against
    ALL such quarters up to the frozen horizon
    (``train_end_period + horizon_quarters``): the holdout window.
    ``co_fired`` is true iff every risk_id in the claim was active in the
    SAME quarter at least once in that window (all_must_fire).

    Claims are immutable: resolution appends a new linked row
    ``{kind: "resolution", resolves_entry_id, resolution, resolution_ts}``.
    Already-resolved claims (a resolution row references them) are skipped.
    Returns an honest summary dict.
    """
    entries = _read_entries(ledger_path)
    resolved_ids = {
        str(e["resolves_entry_id"])
        for e in entries
        if e.get("kind") == "resolution" and e.get("resolves_entry_id")
    }
    fired: dict[int, set[str]] = {}
    for ev in series_events:
        fired.setdefault(int(ev.period), set()).add(str(ev.risk_id))

    summary: dict[str, Any] = {
        "resolved": 0,
        "already_resolved": 0,
        "open": 0,
        "skipped": [],
        "resolutions": [],
    }
    for entry in entries:
        if entry.get("kind") not in RESOLVABLE_CLAIM_KINDS:
            continue
        entry_id = str(entry.get("entry_id"))
        if entry_id in resolved_ids or entry.get("resolution") is not None:
            summary["already_resolved"] += 1
            continue
        claim = entry.get("claim") or {}
        risk_ids = {str(r) for r in (claim.get("risk_ids") or [])}
        if not risk_ids:
            summary["skipped"].append(
                {"entry_id": entry_id, "reason": "claim carries no risk_ids"}
            )
            continue
        frozen = entry.get("frozen_forecast") or {}
        horizon = int(frozen.get("horizon_quarters") or FORECAST_HORIZON_QUARTERS)
        window_end = train_end_period + horizon
        periods = sorted(
            p for p in fired if train_end_period < p <= min(window_end, current_period)
        )
        if not periods:
            # window not closed: no observed quarter past the train end yet
            summary["open"] += 1
            continue
        co_fired = any(risk_ids <= fired[p] for p in periods)
        resolution = {"co_fired": co_fired, "resolved_periods": periods}
        ts = _now()
        append_entry(
            ledger_path,
            {
                "entry_id": uuid.uuid4().hex[:12],
                "ts": ts,
                "kind": "resolution",
                "resolves_entry_id": entry_id,
                "resolution": resolution,
                "resolution_ts": ts,
            },
        )
        resolved_ids.add(entry_id)
        summary["resolved"] += 1
        summary["resolutions"].append(
            {"resolves_entry_id": entry_id, **resolution}
        )
    return summary


# ---------------------------------------------------------------------------
# Calibration report (resolved claims only; n=0 reported, never hidden)
# ---------------------------------------------------------------------------


def _quarter_label(iso_ts: Any) -> str:
    """Calendar-quarter bucket for a resolution timestamp (e.g. "2026-Q3")."""
    try:
        dt = datetime.fromisoformat(str(iso_ts))
    except ValueError:
        return "unknown"
    return f"{dt.year}-Q{(dt.month - 1) // 3 + 1}"


def calibration_report(ledger_path: str | Path) -> dict[str, Any]:
    """Calibration over RESOLVED analogue/llm_join claims only.

    The Brier score compares each claim's frozen deterministic p_hat to the
    resolved 0/1 outcome — it scores the DETERMINISTIC forecast the claim
    is pinned to (the published number), which is the baseline instrument
    for the future judgment-vs-deterministic comparison (G-shadow), not a
    judgment score. ``resolved_per_quarter`` is the P7 feasibility
    instrument (kill rule: < ~30 resolved claims/quarter). n_resolved == 0
    is reported honestly: brier is None, status "insufficient_data".
    """
    entries = _read_entries(ledger_path)
    resolutions: dict[str, dict[str, Any]] = {}
    for e in entries:
        if e.get("kind") == "resolution" and e.get("resolves_entry_id"):
            # first resolution wins (re-resolution is already impossible)
            resolutions.setdefault(str(e["resolves_entry_id"]), e)

    rows: list[dict[str, Any]] = []
    n_open = 0
    for e in entries:
        if e.get("kind") not in RESOLVABLE_CLAIM_KINDS:
            continue
        res = resolutions.get(str(e.get("entry_id")))
        if res is None:
            n_open += 1
            continue
        frozen = e.get("frozen_forecast") or {}
        p_hat = float(frozen.get("p_hat") or 0.0)
        outcome = 1.0 if (res.get("resolution") or {}).get("co_fired") else 0.0
        rows.append(
            {
                "entry_id": e.get("entry_id"),
                "pair_id": frozen.get("pair_id"),
                "p_hat": p_hat,
                "outcome": outcome,
                "brier": (p_hat - outcome) ** 2,
                "resolved_periods": (res.get("resolution") or {}).get("resolved_periods"),
                "resolution_ts": res.get("resolution_ts"),
            }
        )

    n_resolved = len(rows)
    brier = sum(r["brier"] for r in rows) / n_resolved if n_resolved else None
    per_quarter: dict[str, int] = {}
    for r in rows:
        label = _quarter_label(r["resolution_ts"])
        per_quarter[label] = per_quarter.get(label, 0) + 1

    brier_note = (
        "Brier scores the frozen DETERMINISTIC forecast each claim is pinned "
        "to (the published p_hat), not judgment — the baseline instrument "
        "for the future judgment-vs-deterministic comparison (G-shadow)."
    )
    if n_resolved:
        status = "ok"
        note = (
            f"{n_resolved} resolved claim(s), {n_open} open. "
            "Proper scores over resolved rows only."
        )
    else:
        status = "insufficient_data"
        note = (
            "insufficient data: n_resolved=0 — no claim has resolved yet, so "
            "no proper score exists. Reported, not hidden (P7)."
        )
    return {
        "judgment_ledger_version": JUDGMENT_LEDGER_VERSION,
        "ledger_path": str(ledger_path),
        "generated_at": _now(),
        "status": status,
        "n_resolved": n_resolved,
        "n_open": n_open,
        "brier": brier,
        "brier_note": brier_note,
        "resolved_per_quarter": dict(sorted(per_quarter.items())),
        "feasibility": {
            "target_resolved_per_quarter": FEASIBILITY_TARGET_PER_QUARTER,
            "note": (
                "P7 kill rule: fewer than ~30 resolved claims per quarter "
                "after 4 quarters means the prospective program is "
                "under-powered by construction — stay at L1."
            ),
        },
        "note": note,
        "claims": rows,
    }


def render_report_md(report: dict[str, Any]) -> str:
    """Markdown rendering of :func:`calibration_report` output."""
    lines = [
        "# Judgment ledger — calibration report",
        "",
        f"- ledger: `{report['ledger_path']}`",
        f"- generated: {report['generated_at']}",
        f"- status: {report['status']}",
        f"- resolved claims: {report['n_resolved']} · open claims: {report['n_open']}",
    ]
    if report["brier"] is None:
        lines.append("- Brier: n/a — insufficient data (0 resolved claims)")
    else:
        lines.append(
            "- Brier (frozen deterministic p_hat vs resolved 0/1): "
            f"{report['brier']:.4f}"
        )
    lines += [
        "",
        report["brier_note"],
        "",
        "## Resolved per quarter (P7 feasibility instrument)",
        "",
        f"Target: ≥ {report['feasibility']['target_resolved_per_quarter']} "
        "resolved claims/quarter.",
        "",
    ]
    if report["resolved_per_quarter"]:
        lines += ["| quarter | resolved |", "|---|---|"]
        for quarter, n in report["resolved_per_quarter"].items():
            lines.append(f"| {quarter} | {n} |")
    else:
        lines.append("No resolutions yet.")
    lines += ["", "## Resolved claims", ""]
    if report["claims"]:
        lines += ["| entry | pair_id | p_hat | outcome | brier |", "|---|---|---|---|---|"]
        for r in report["claims"]:
            lines.append(
                f"| {r['entry_id']} | {r['pair_id']} | {r['p_hat']:.4f} "
                f"| {int(r['outcome'])} | {r['brier']:.4f} |"
            )
    else:
        lines.append("None.")
    lines += ["", report["note"], ""]
    return "\n".join(lines)
