"""External feature registry + association testing (algorithm-spec.md
section 10, evaluation-plan.md section 4).

This is the only sanctioned path for geopolitical/macro context into
scenario scoring. The discipline, in order:

1. **Pre-registration** — feature families must exist in the registry
   (YAML) *before* any association is tested. Testing an unregistered
   family raises.
2. **Fixed calendar** — features are binary/count series on quarters.
3. **Temporal structure** — discovery on train quarters only; accepted
   features must reproduce the same rate-ratio direction on the holdout
   window. Block bootstrap (block = 2 quarters) gives the CI.
4. **Multiple comparisons** — Benjamini–Hochberg FDR at q = 0.10 across
   all tested (feature x atom) pairs, on train p-values only.
5. **Minimum support** — a feature must be "on" in >= s_min train
   periods (default 8).
6. **Adversarial null pass** — every FDR-passing association gets a
   time-shift null check (deterministic) and optionally an LLM null
   pass (a model writes the best non-causal explanation; a second model
   votes). Both are recorded; disagreement keeps the feature out.

What passes is labeled ``associational_feature`` — an optional modifier
candidate, never proof of causation, and never auto-applied to scores
(spec section 10.2 item 6: modifiers need human/policy accept).
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .channels import occurrence_series
from .models import IncidentEvent
from .telemetry import TelemetryLogger

FDR_Q = 0.10
DEFAULT_MIN_SUPPORT = 8
BOOTSTRAP_DRAWS = 500
BOOTSTRAP_BLOCK = 2


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


@dataclass
class FeatureEntry:
    feature_id: str
    name: str
    family: str  # geo | macro | cyber | reg
    registered_at: str
    status: str = "candidate"  # candidate | accepted | rejected | rejected_null
    series: dict[int, float] = field(default_factory=dict)  # quarter -> value
    fdr_q_value: float | None = None
    holdout_pass: bool | None = None
    null_pass: bool | None = None
    notes: str = ""


class FeatureRegistry:
    """Pre-registered external feature families (YAML-backed)."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.entries: dict[str, FeatureEntry] = {}
        if self.path.exists():
            self._load()

    def _load(self) -> None:
        data = yaml.safe_load(self.path.read_text()) or {}
        for fid, raw in (data.get("features") or {}).items():
            self.entries[fid] = FeatureEntry(
                feature_id=fid,
                name=raw["name"],
                family=raw["family"],
                registered_at=str(raw["registered_at"]),
                status=raw.get("status", "candidate"),
                series={int(k): float(v) for k, v in (raw.get("series") or {}).items()},
                fdr_q_value=raw.get("fdr_q_value"),
                holdout_pass=raw.get("holdout_pass"),
                null_pass=raw.get("null_pass"),
                notes=raw.get("notes", ""),
            )

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "features": {
                fid: {
                    "name": e.name,
                    "family": e.family,
                    "registered_at": e.registered_at,
                    "status": e.status,
                    "series": e.series,
                    "fdr_q_value": e.fdr_q_value,
                    "holdout_pass": e.holdout_pass,
                    "null_pass": e.null_pass,
                    "notes": e.notes,
                }
                for fid, e in sorted(self.entries.items())
            }
        }
        self.path.write_text(yaml.safe_dump(data, sort_keys=True))

    def register(self, entry: FeatureEntry) -> None:
        if entry.feature_id in self.entries:
            raise ValueError(f"feature {entry.feature_id} already registered")
        self.entries[entry.feature_id] = entry

    def require_registered(self, feature_id: str) -> FeatureEntry:
        """Pre-registration gate: unregistered features may not be tested."""
        if feature_id not in self.entries:
            raise KeyError(
                f"feature {feature_id} is not pre-registered; "
                "register the family before mining (spec 10.2 item 1)"
            )
        return self.entries[feature_id]


# ---------------------------------------------------------------------------
# Statistics (pure stdlib)
# ---------------------------------------------------------------------------


def fisher_exact_enrichment(a: int, b: int, c: int, d: int) -> float:
    """One-sided Fisher exact p-value for enrichment in a 2x2 table
    [[a, b], [c, d]]: P(X >= a) under the hypergeometric null."""
    n = a + b + c + d
    row1 = a + b
    col1 = a + c

    def hyp(x: int) -> float:
        return (
            math.comb(col1, x) * math.comb(n - col1, row1 - x) / math.comb(n, row1)
        )

    lo = max(0, row1 - (n - col1))
    hi = min(row1, col1)
    total = sum(hyp(x) for x in range(lo, hi + 1))
    p = sum(hyp(x) for x in range(a, hi + 1))
    return p / total if total > 0 else 1.0


def rate_ratio(a: int, b: int, c: int, d: int) -> float | None:
    """(fire rate | feature on) / (fire rate | feature off), +1 smoothing."""
    if a + b == 0 or c + d == 0:
        return None
    return ((a + 1) / (a + b + 1)) / ((c + 1) / (c + d + 1))


def block_bootstrap_log_rr(
    series_f: list[int], series_y: list[int], *, draws: int = BOOTSTRAP_DRAWS,
    block: int = BOOTSTRAP_BLOCK, seed: int = 0,
) -> tuple[float, float] | None:
    """Block bootstrap 90% CI for log rate-ratio on aligned binary series."""
    rng = random.Random(seed)
    n = len(series_f)
    log_rrs: list[float] = []
    for _ in range(draws):
        idx: list[int] = []
        while len(idx) < n:
            start = rng.randrange(0, max(n - block + 1, 1))
            idx.extend(range(start, min(start + block, n)))
        idx = idx[:n]
        a = sum(1 for i in idx if series_f[i] and series_y[i])
        b = sum(1 for i in idx if series_f[i] and not series_y[i])
        c = sum(1 for i in idx if not series_f[i] and series_y[i])
        d = sum(1 for i in idx if not series_f[i] and not series_y[i])
        rr = rate_ratio(a, b, c, d)
        if rr and rr > 0:
            log_rrs.append(math.log(rr))
    if len(log_rrs) < 10:
        return None
    log_rrs.sort()
    return (
        log_rrs[int(0.05 * (len(log_rrs) - 1))],
        log_rrs[int(0.95 * (len(log_rrs) - 1))],
    )


def benjamini_hochberg(p_values: dict[str, float], q: float = FDR_Q) -> dict[str, float]:
    """BH adjusted q-values. Returns {key: q_value}."""
    m = len(p_values)
    if m == 0:
        return {}
    ordered = sorted(p_values.items(), key=lambda kv: kv[1])
    q_values: dict[str, float] = {}
    prev = 1.0
    for rank, (key, p) in reversed(list(enumerate(ordered, start=1))):
        adj = min(prev, p * m / rank)
        q_values[key] = adj
        prev = adj
    return q_values


# ---------------------------------------------------------------------------
# Association testing
# ---------------------------------------------------------------------------


@dataclass
class AssociationResult:
    feature_id: str
    risk_id: str
    table_train: tuple[int, int, int, int]
    table_holdout: tuple[int, int, int, int]
    p_value: float
    q_value: float | None
    rate_ratio_train: float | None
    log_rr_ci90: tuple[float, float] | None
    holdout_direction_ok: bool
    support_train: int


def _table(series_f: list[int], series_y: list[int]) -> tuple[int, int, int, int]:
    a = sum(1 for f, y in zip(series_f, series_y) if f and y)
    b = sum(1 for f, y in zip(series_f, series_y) if f and not y)
    c = sum(1 for f, y in zip(series_f, series_y) if not f and y)
    d = sum(1 for f, y in zip(series_f, series_y) if not f and not y)
    return (a, b, c, d)


def test_associations(
    registry: FeatureRegistry,
    events: list[IncidentEvent],
    risk_ids: list[str],
    *,
    train_quarters: int,
    n_periods: int,
    min_support: int = DEFAULT_MIN_SUPPORT,
    seed: int = 0,
) -> dict[str, Any]:
    """Test every pre-registered candidate feature against every atom.

    Discovery (Fisher + FDR + bootstrap) uses train quarters only; the
    holdout window is used once, for direction reproduction.
    """
    series_map = occurrence_series(events, n_periods)
    results: list[AssociationResult] = []
    p_values: dict[str, float] = {}
    for fid, entry in registry.entries.items():
        if entry.status != "candidate":
            continue
        f_train = [1 if entry.series.get(t, 0) else 0 for t in range(train_quarters)]
        f_hold = [1 if entry.series.get(t, 0) else 0 for t in range(train_quarters, n_periods)]
        support = sum(f_train)
        if support < min_support:
            entry.notes = f"insufficient support ({support} < {min_support})"
            continue
        for rid in risk_ids:
            y_train = [1 if t in series_map.get(rid, set()) else 0 for t in range(train_quarters)]
            y_hold = [
                1 if t in series_map.get(rid, set()) else 0
                for t in range(train_quarters, n_periods)
            ]
            tab_train = _table(f_train, y_train)
            tab_hold = _table(f_hold, y_hold)
            p = fisher_exact_enrichment(*tab_train)
            key = f"{fid}|{rid}"
            p_values[key] = p
            rr_train = rate_ratio(*tab_train)
            rr_hold = rate_ratio(*tab_hold)
            results.append(
                AssociationResult(
                    feature_id=fid,
                    risk_id=rid,
                    table_train=tab_train,
                    table_holdout=tab_hold,
                    p_value=p,
                    q_value=None,
                    rate_ratio_train=rr_train,
                    log_rr_ci90=block_bootstrap_log_rr(f_train, y_train, seed=seed),
                    holdout_direction_ok=bool(rr_hold is None or rr_train is None or (rr_hold > 1) == (rr_train > 1)),
                    support_train=support,
                )
            )

    q_values = benjamini_hochberg(p_values)
    for res in results:
        res.q_value = q_values[f"{res.feature_id}|{res.risk_id}"]
    return {
        "results": results,
        "q_values": q_values,
        "n_tested": len(p_values),
        "n_features_tested": sum(
            1 for e in registry.entries.values() if e.status == "candidate"
        ),
    }


# ---------------------------------------------------------------------------
# Adversarial null pass
# ---------------------------------------------------------------------------


def time_shift_null(
    entry: FeatureEntry,
    events: list[IncidentEvent],
    risk_id: str,
    *,
    train_quarters: int,
    n_periods: int,
    shifts: tuple[int, ...] = (1, 2, 3, 5, 7),
) -> dict[str, Any]:
    """Deterministic null: shift the feature series by k quarters. A real
    alignment should weaken under arbitrary shifts; an association that
    gets STRONGER under shift is flagged (seasonality/artifact suspect).
    """
    series_map = occurrence_series(events, n_periods)
    y_train = [1 if t in series_map.get(risk_id, set()) else 0 for t in range(train_quarters)]
    base_f = [entry.series.get(t, 0) for t in range(train_quarters)]
    base_p = fisher_exact_enrichment(*_table([1 if v else 0 for v in base_f], y_train))
    shifted: dict[int, float] = {}
    for k in shifts:
        f_shift = [base_f[(t - k) % train_quarters] for t in range(train_quarters)]
        shifted[k] = fisher_exact_enrichment(*_table([1 if v else 0 for v in f_shift], y_train))
    stronger_under_shift = sum(1 for p in shifted.values() if p < base_p)
    return {
        "base_p": base_p,
        "shift_p": shifted,
        "n_shifts_stronger": stronger_under_shift,
        "null_ok": stronger_under_shift <= 1,
    }


_LLM_NULL_PROMPT = """An association was found between an external feature and an
operational-risk event series:

Feature: {feature_name} (family: {family})
Event series: {risk_id}
Train rate ratio: {rr:.2f} (enrichment when feature is on)

Write the BEST non-causal explanation for this association (common
seasonality, reporting changes, calendar artifacts, pure chance under
multiple testing). Two or three sentences, plain text."""


_LLM_VOTE_PROMPT = """A second reviewer must decide whether an external feature may be
used as an OPTIONAL associational modifier (not causal evidence).

Feature: {feature_name} (family: {family})
Event series: {risk_id}
Rate ratio: {rr:.2f}, FDR q-value: {q:.3f}
Best non-causal explanation offered:
\"{null_explanation}\"

Answer JSON only: {{"verdict": "accept" | "reject", "reason": "<one line>"}}
Reject if the non-causal explanation is at least as plausible as a real
intensifier effect."""


def llm_null_pass(
    entry: FeatureEntry,
    risk_id: str,
    rr: float,
    q_value: float,
    *,
    explain_client: Any,
    vote_client: Any,
    explain_model: str,
    vote_model: str,
    telemetry: TelemetryLogger,
) -> dict[str, Any]:
    """LLM adversarial null pass (spec 10.2 item 5): one model writes the
    best non-causal explanation, a different model votes. Telemetry-
    instrumented; payloads are public-safe (ids, family, numbers only).
    """

    def _chat(client, model, prompt):
        return telemetry.instrumented_call(
            lambda: client.chat(
                [{"role": "user", "content": prompt}],
                model=model, temperature=0.0, max_tokens=2048,
            ),
            node="ext_null_pass",
            phase="eval_harness",
            provider="mlx",
            model_id=model,
            plane="public",
        )

    explanation = _chat(
        explain_client,
        explain_model,
        _LLM_NULL_PROMPT.format(
            feature_name=entry.name, family=entry.family, risk_id=risk_id, rr=rr
        ),
    )
    vote_text = _chat(
        vote_client,
        vote_model,
        _LLM_VOTE_PROMPT.format(
            feature_name=entry.name, family=entry.family, risk_id=risk_id,
            rr=rr, q=q_value, null_explanation=explanation.strip()[:600],
        ),
    )
    import json
    import re

    verdict, reason = "reject", "unparseable vote"
    try:
        data = json.loads(vote_text)
    except (json.JSONDecodeError, TypeError):
        match = re.search(r"\{[^{}]*\}", vote_text or "", re.DOTALL)
        data = {}
        if match:
            try:
                data = json.loads(match.group(0))
            except json.JSONDecodeError:
                data = {}
    if data.get("verdict") in ("accept", "reject"):
        verdict = data["verdict"]
        reason = str(data.get("reason", ""))[:200]
    return {"explanation": explanation.strip()[:600], "verdict": verdict, "reason": reason}


# ---------------------------------------------------------------------------
# Screen driver
# ---------------------------------------------------------------------------


def run_screen(
    registry: FeatureRegistry,
    events: list[IncidentEvent],
    risk_ids: list[str],
    *,
    train_quarters: int,
    n_periods: int,
    min_support: int = DEFAULT_MIN_SUPPORT,
    seed: int = 0,
    llm_null: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Full discovery screen: association tests -> FDR -> holdout ->
    adversarial nulls -> registry status updates.

    ``llm_null``: optional {explain_client, vote_client, explain_model,
    vote_model, telemetry} for the LLM adversarial pass.
    Returns the report dict (registry is updated and saved by the caller).
    """
    screen = test_associations(
        registry, events, risk_ids,
        train_quarters=train_quarters, n_periods=n_periods,
        min_support=min_support, seed=seed,
    )
    survivors = [
        r for r in screen["results"]
        if r.q_value is not None and r.q_value <= FDR_Q and r.holdout_direction_ok
    ]
    accepted_pairs: list[dict[str, Any]] = []
    rejected_pairs: list[dict[str, Any]] = []
    for res in survivors:
        entry = registry.entries[res.feature_id]
        null = time_shift_null(
            entry, events, res.risk_id,
            train_quarters=train_quarters, n_periods=n_periods,
        )
        pair = {
            "feature_id": res.feature_id,
            "risk_id": res.risk_id,
            "rate_ratio_train": round(res.rate_ratio_train, 4) if res.rate_ratio_train else None,
            "p_value": round(res.p_value, 6),
            "q_value": round(res.q_value, 6),
            "log_rr_ci90": [round(x, 4) for x in res.log_rr_ci90] if res.log_rr_ci90 else None,
            "time_shift_null": null,
            "llm_null": None,
            "label": "associational_feature",
        }
        ok = null["null_ok"]
        if ok and llm_null is not None:
            try:
                verdict = llm_null_pass(
                    entry, res.risk_id, res.rate_ratio_train or 1.0, res.q_value,
                    explain_client=llm_null["explain_client"],
                    vote_client=llm_null["vote_client"],
                    explain_model=llm_null["explain_model"],
                    vote_model=llm_null["vote_model"],
                    telemetry=llm_null["telemetry"],
                )
            except Exception as exc:
                # Fail-closed and recorded (P6: the adversarial null is
                # default-on): an unreachable endpoint or a broken client
                # keeps the pair OUT — acceptance never rides on a missing
                # adversarial check, and the failure is on the record.
                verdict = {
                    "explanation": None,
                    "verdict": "reject",
                    "reason": (
                        "llm null pass unavailable — fail-closed: "
                        f"{type(exc).__name__}: {exc}"[:300]
                    ),
                    "error": True,
                }
            pair["llm_null"] = verdict
            ok = verdict["verdict"] == "accept"
        (accepted_pairs if ok else rejected_pairs).append(pair)

    for entry in registry.entries.values():
        if entry.status != "candidate":
            continue
        mine_accepted = [p for p in accepted_pairs if p["feature_id"] == entry.feature_id]
        mine_tested = [r for r in screen["results"] if r.feature_id == entry.feature_id]
        if mine_accepted:
            entry.status = "accepted"
            entry.fdr_q_value = min(p["q_value"] for p in mine_accepted)
            entry.holdout_pass = True
            entry.null_pass = True
            entry.notes = (
                f"associational_feature; accepted pairs: "
                + ", ".join(p["risk_id"] for p in mine_accepted)
            )
        elif mine_tested:
            best = min(r.q_value for r in mine_tested if r.q_value is not None)
            entry.fdr_q_value = round(best, 6) if best is not None else None
            entry.status = "rejected"
            entry.holdout_pass = False
    return {
        "n_features_registered": len(registry.entries),
        "n_features_tested": screen["n_features_tested"],
        "n_pairs_tested": screen["n_tested"],
        "n_pairs_fdr_pass": len(survivors),
        "fdr_q": FDR_Q,
        "min_support": min_support,
        "accepted_pairs": accepted_pairs,
        "rejected_pairs": rejected_pairs,
        # P6: how many pairs the LLM null pass kept out, and how many of
        # those were fail-closed infrastructure errors (endpoint down,
        # broken client) rather than model rejections.
        "n_rejected_llm_null": sum(
            1 for p in rejected_pairs if p.get("llm_null") is not None
        ),
        "n_llm_null_fail_closed": sum(
            1 for p in rejected_pairs if (p.get("llm_null") or {}).get("error")
        ),
        "registry_status": {fid: e.status for fid, e in registry.entries.items()},
    }


def render_screen_md(report: dict[str, Any]) -> str:
    lines = [
        "# External feature screen (algorithm-spec.md section 10)",
        "",
        f"- registered: {report['n_features_registered']} · tested: {report['n_features_tested']}",
        f"- pairs tested: {report['n_pairs_tested']} · FDR q={report['fdr_q']} · "
        f"min support {report['min_support']}",
        f"- FDR survivors (with holdout direction): {report['n_pairs_fdr_pass']}",
        "",
        "## Accepted (associational_feature — modifier candidates only)",
        "",
    ]
    if report["accepted_pairs"]:
        for p in report["accepted_pairs"]:
            lines.append(
                f"- `{p['feature_id']}` x `{p['risk_id']}`: RR {p['rate_ratio_train']}, "
                f"q {p['q_value']}, CI90 {p['log_rr_ci90']}"
            )
    else:
        lines.append("- none")
    lines += [
        "",
        "## Rejected at null pass",
        "",
    ]
    if report["rejected_pairs"]:
        for p in report["rejected_pairs"]:
            lines.append(f"- `{p['feature_id']}` x `{p['risk_id']}` (q {p['q_value']})")
    else:
        lines.append("- none")
    lines += [
        "",
        "## Registry status",
        "",
    ]
    lines += [f"- `{fid}`: {status}" for fid, status in report["registry_status"].items()]
    lines += [
        "",
        "Accepted features are optional modifier candidates requiring human/policy "
        "accept before use in scores; they are never causal evidence.",
        "",
    ]
    return "\n".join(lines)
