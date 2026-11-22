"""Task B-cal: compound-level calibration against generative truth (A6).

Task B ranks; it says nothing about *levels*. B-cal closes that gap on R0,
where the generative truth is known in closed form: per pair, the true
quarterly joint under the channel-shock generator is

* same channel c (shared shock):  q* = pi_c * s_i * s_j + (1 - pi_c) * q_i * q_j
* different channels (independent shocks): q* = q_bar_i * q_bar_j
  with q_bar_i = pi_c * s_i + (1 - pi_c) * q_i the true marginal rate,

where s_i = min(q_i * mult_i + add_i, 0.9) is the shocked firing rate
(synthetic.py). Annualized: p* = 1 - (1 - q*) ** horizon.

Metrics per stratum: 90% interval coverage (fraction of pairs with
p* in [p_lo, p_hi]), mean signed level bias (p_hat - p*), and RMSE.
Strata are pre-registered (HYBRID_SCORING_PROPOSAL.md A6):

* E[N_S | independence] = T * q_ind bins [0, 0.25), [0.25, 1), [1, inf)
* rare (fitted q_ind < 0.01) vs common
* supported (N_S >= 1) vs zero-support

including the **supported-rare** stratum — the one where the naive
pseudo-co-event T1 fix regresses, and the stratum any A1 challenger must
win before promotion. Pass band on overall 90% coverage mirrors Task A:
[0.80, 0.95].
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

from .candidates import all_pairs
from .evaluate import _round
from .models import IncidentEvent, RiskAtom
from .scorer import DetScorer, ScorerConfig, _annual

COVERAGE_BAND = (0.80, 0.95)  # pre-registered, same as Task A
RARE_Q_IND = 0.01  # rare/common split on fitted independence product
ESN_BINS = (0.25, 1.0)  # E[N_S | independence] bin edges
MIN_STRATUM_N = 5  # strata smaller than this are reported, not gated


def truth_joint_quarterly(truth: dict, a: str, b: str) -> float:
    """Closed-form P(both atoms fire in one quarter) under the R0 generator."""
    q_base = truth["q_base"]
    mult = truth["shock_mult"]
    add = truth["shock_add"]
    shock_pi = truth["shock_pi"]
    ch_of: dict[str, str] = {}
    for ch, ids in truth["channels"].items():
        for rid in ids:
            ch_of[rid] = ch
    ca, cb = ch_of[a], ch_of[b]
    qa, qb = q_base[a], q_base[b]
    sa = min(qa * mult[a] + add[a], 0.9)
    sb = min(qb * mult[b] + add[b], 0.9)
    if ca == cb:
        pi = shock_pi[ca]
        return pi * sa * sb + (1.0 - pi) * qa * qb
    pi_a, pi_b = shock_pi[ca], shock_pi[cb]
    qa_bar = pi_a * sa + (1.0 - pi_a) * qa
    qb_bar = pi_b * sb + (1.0 - pi_b) * qb
    return qa_bar * qb_bar


def _esn_bin(esn: float) -> str:
    lo, hi = ESN_BINS
    if esn < lo:
        return f"[0,{lo})"
    if esn < hi:
        return f"[{lo},{hi})"
    return f"[{hi},inf)"


def _strata(q_ind: float, n_s: int, n_periods: int) -> list[str]:
    esn = n_periods * q_ind
    rare = q_ind < RARE_Q_IND
    supported = n_s >= 1
    return [
        "all",
        f"esn:{_esn_bin(esn)}",
        "rare" if rare else "common",
        "supported" if supported else "zero_support",
        "supported_rare" if (rare and supported) else "other",
    ]


@dataclass
class BcalResult:
    report: dict
    report_md: str


def run_bcal(
    atoms: list[RiskAtom],
    events: list[IncidentEvent],
    truth: dict,
    *,
    holdout_quarters: int = 8,
    config: ScorerConfig | None = None,
    dependence_modes: tuple[str, ...] = ("shrunk_empirical",),
) -> BcalResult:
    """Level calibration of compound p_hat against generative truth.

    Runs once per dependence mode so the primary and any challenger can be
    compared side by side (the A1 promotion instrument).
    """
    base = config or ScorerConfig()
    n_periods_total = max((e.period for e in events), default=-1) + 1
    if n_periods_total <= holdout_quarters:
        raise ValueError(
            f"need more than {holdout_quarters} quarters of events, got {n_periods_total}"
        )
    train_quarters = n_periods_total - holdout_quarters

    mode_reports: dict[str, dict] = {}
    for mode in dependence_modes:
        cfg = ScorerConfig(**{**base.__dict__, "dependence_mode": mode})
        scorer = DetScorer(cfg).fit(events, atoms, train_quarters)
        strata: dict[str, list[dict]] = {}
        n_above_hi = 0
        for a, b in all_pairs(atoms):
            rlz = scorer.score_pair(a, b)
            ma, mb = scorer.marginals[a], scorer.marginals[b]
            q_ind = ma.q_quarterly * mb.q_quarterly
            n_s = rlz.components["dependence"]["data_support"]
            p_true = _annual(
                truth_joint_quarterly(truth, a, b), cfg.horizon_quarters
            )
            row = {
                "covered": float(rlz.p_interval[0] <= p_true <= rlz.p_interval[1]),
                "err": rlz.p_hat - p_true,
            }
            if rlz.components["interval"]["p_hat_above_hi"]:
                n_above_hi += 1
            for key in _strata(q_ind, n_s, scorer.n_periods):
                strata.setdefault(key, []).append(row)

        stratum_rows: dict[str, dict] = {}
        for key, rows in sorted(strata.items()):
            n = len(rows)
            coverage = sum(r["covered"] for r in rows) / n
            mean_bias = sum(r["err"] for r in rows) / n
            rmse = math.sqrt(sum(r["err"] ** 2 for r in rows) / n)
            stratum_rows[key] = {
                "n": n,
                "coverage_90": _round(coverage),
                "mean_bias": _round(mean_bias, 6),
                "rmse": _round(rmse, 6),
                "gated": n >= MIN_STRATUM_N,
            }
        overall = stratum_rows["all"]
        mode_reports[mode] = {
            "n_pairs": len(all_pairs(atoms)),
            "coverage_90": overall["coverage_90"],
            "mean_bias": overall["mean_bias"],
            "rmse": overall["rmse"],
            "p_hat_above_hi_rows": n_above_hi,
            "strata": stratum_rows,
            "coverage_band": list(COVERAGE_BAND),
            "pass": COVERAGE_BAND[0] <= (overall["coverage_90"] or 0) <= COVERAGE_BAND[1],
        }

    report = {
        "task": "B-cal",
        "description": (
            "compound p_hat coverage and level bias against closed-form "
            "generative truth, stratified by E[N_S|independence], rarity "
            "and support (incl. the supported-rare stratum)"
        ),
        "holdout_quarters": holdout_quarters,
        "train_quarters": train_quarters,
        "pre_registered": {
            "coverage_band": list(COVERAGE_BAND),
            "rare_q_ind_below": RARE_Q_IND,
            "esn_bins": list(ESN_BINS),
            "min_stratum_n": MIN_STRATUM_N,
        },
        "modes": mode_reports,
    }
    return BcalResult(report=report, report_md=_render_bcal_md(report))


def _render_bcal_md(report: dict) -> str:
    lines = [
        "# Task B-cal — compound level calibration vs generative truth",
        "",
        f"coverage band (pre-registered): {report['pre_registered']['coverage_band']} · "
        f"rare: q_ind < {report['pre_registered']['rare_q_ind_below']} · "
        f"strata gated at n >= {report['pre_registered']['min_stratum_n']}",
        "",
    ]
    for mode, m in report["modes"].items():
        lines += [
            f"## {mode} — {'PASS' if m['pass'] else 'FAIL'}",
            "",
            f"overall: coverage {m['coverage_90']} · bias {m['mean_bias']} · "
            f"rmse {m['rmse']} · p_hat>p_hi rows: {m['p_hat_above_hi_rows']}",
            "",
            "| stratum | n | coverage | mean bias | rmse |",
            "|---------|---|----------|-----------|------|",
        ]
        for key, s in m["strata"].items():
            lines.append(
                f"| {key} | {s['n']} | {s['coverage_90']} | {s['mean_bias']} | {s['rmse']} |"
            )
        lines.append("")
    return "\n".join(lines)


def write_bcal_outputs(result: BcalResult, out_dir: str | Path) -> dict[str, Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    json_path = out / "bcal_report.json"
    md_path = out / "bcal_report.md"
    json_path.write_text(json.dumps(result.report, indent=2, sort_keys=True))
    md_path.write_text(result.report_md)
    return {"json": json_path, "md": md_path}
