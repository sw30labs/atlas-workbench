"""Ablation runner — evaluation-plan.md section 5, pre-registered order.

| Ablation | Removes | Expectation if the design is right |
|----------|---------|-------------------------------------|
| A1 | Dependence (force independence) | Task B AP drops |
| A2 | Channel graph (allow all pairs) | Cost up; precision@k down or flat |
| A3 | EVT layer | Tail quantiles unstable; mean metrics flat |
| A4 | LLM judgment | Coherence drops; Task B flat/up |
| A5 | External features | Task B small drop if any |
| A6 | Multiverse (batch only) | See Task B pass rule |

A2, A4 and A6 were measured against the CBGE multiverse loop, which was
**removed from mainline on 2026-08-01** (owner decision: batch is the
production path). They are reported as historical entries with their last
measured results preserved, not re-run. The runnable matrix is A1, A3;
A5 stays gated on the modifier policy path.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import ALGORITHM_VERSION
from .evaluate import run_evaluation
from .scorer import ScorerConfig

#: Pre-registered ablation order (evaluation-plan.md section 5).
ABLATION_ORDER = ("A1", "A2", "A6", "A4", "A3", "A5")

#: Pre-registered sensitivity sets (HYBRID_SCORING_PROPOSAL.md A1/A6):
#: kappa for the shrunk-empirical primary; the pi0 x c grid for the
#: mixture challenger. The challenger-vs-primary B-cal comparison is the
#: promotion instrument — reported, never auto-acted-on.
KAPPA_SENSITIVITY = (2.0, 4.0, 8.0)
MIXTURE_PI0_SENSITIVITY = (0.8, 0.9, 0.95)
MIXTURE_C_SENSITIVITY = (0.5, 1.0, 2.0)

#: Last measured results before the multiverse loop was removed from
#: mainline (2026-08-01, branch history preserves the code that produced
#: them). Kept visible so the matrix never silently loses a row.
_HISTORICAL: dict[str, dict[str, Any]] = {
    "A2": {
        "removes": "channel graph blocking (expansion proposes all pairs)",
        "expectation": "cost up; precision@k down or flat",
        "observed": {
            "n_scored_full": 58, "n_scored_ablated": 91,
            "p_at_20_full": 0.9, "p_at_20_ablated": 0.9,
            "ap_full": 0.7741, "ap_ablated": 0.7316,
        },
        "consistent": True,
        "historical": "measured 2026-08-01 on R0 before loop removal",
    },
    "A6": {
        "removes": "multiverse loop (batch scorer only)",
        "expectation": "see Task B pass rule",
        "observed": {
            "decision": "keep_loop", "quality_ok": True, "cost_ok": True,
            "ap_loop": 0.7741, "ap_batch": 0.7335,
            "n_scored_loop": 58, "n_scored_batch": 105,
        },
        "consistent": True,
        "historical": "measured 2026-08-01 on R0 before loop removal; "
        "loop later deleted on owner decision despite passing — the pass "
        "was on synthetic data at trivial scale",
    },
    "A4": {
        "removes": "LLM judgment (coherence judge)",
        "expectation": "coherence drops; Task B flat/up",
        "observed": {
            "promoted_with_judge": 19, "promoted_det_mode": 33,
            "coherence_measured_with_judge": 119,
            "coherence_measured_det_mode": 0,
        },
        "consistent": True,
        "historical": "measured 2026-08-01 on R0 before loop removal",
    },
}


@dataclass
class AblationReport:
    report: dict[str, Any]
    report_md: str


def run_ablations(
    atoms,
    events,
    *,
    truth: dict | None = None,
    holdout_quarters: int = 8,
    scorer_config: ScorerConfig | None = None,
    regime: str = "R0",
) -> AblationReport:
    scfg = scorer_config or ScorerConfig()

    # --- baseline: full Task B run ----------------------------------------
    full = run_evaluation(
        atoms, events, truth=truth,
        holdout_quarters=holdout_quarters, config=scfg, regime=regime,
    )
    ap_b3_full = full.report["task_b"]["baselines"]["B3_det_batch"]["average_precision"]

    ablations: dict[str, dict[str, Any]] = {}

    # --- A1: remove dependence ------------------------------------------
    a1 = run_evaluation(
        atoms, events, truth=None,
        holdout_quarters=holdout_quarters,
        config=ScorerConfig(
            **{**scfg.__dict__, "dependence_mode": "independence"}
        ),
        regime=regime,
    )
    ap_b3_a1 = a1.report["task_b"]["baselines"]["B3_det_batch"]["average_precision"]
    ablations["A1"] = {
        "removes": "dependence layer (joints collapse to product of marginals)",
        "expectation": "Task B AP drops",
        "observed": {
            "ap_b3_full": ap_b3_full,
            "ap_b3_ablated": ap_b3_a1,
            "ap_drop": round(ap_b3_full - ap_b3_a1, 4) if ap_b3_full is not None and ap_b3_a1 is not None else None,
        },
        "consistent": (
            ap_b3_full is not None and ap_b3_a1 is not None and ap_b3_a1 < ap_b3_full
        ),
    }

    # --- A3: remove EVT layer ----------------------------------------------
    # Measure tail-estimate stability across MC seeds with and without the
    # GPD overlay (expectation: removing EVT makes tail quantiles less
    # stable; mean metrics flat). Top-B3 pairs with enough MC hits.
    from .candidates import all_pairs as _all_pairs, baseline_scores as _baseline_scores
    from .channels import build_channel_graph as _build_graph
    from .scorer import DetScorer as _DetScorer

    n_periods_total = max((e.period for e in events), default=-1) + 1
    train_quarters = n_periods_total - holdout_quarters
    graph = _build_graph(atoms, graph_id="g_ch", version=1)
    scorer_full = _DetScorer(scfg).fit(events, atoms, train_quarters, graph_hash=graph.graph_hash)
    pairs = _all_pairs(atoms)
    b3 = _baseline_scores("B3_det_batch", pairs, scorer_full, atoms)
    top_pairs = [p for p, _ in sorted(b3.items(), key=lambda kv: -kv[1])[:5]]

    def _tail_estimates(evt_on: bool) -> list[list[float]]:
        per_pair: list[list[float]] = [[] for _ in top_pairs]
        for offset in (0, 1, 2):
            cfg = ScorerConfig(
                **{
                    **scfg.__dict__,
                    "seed": scfg.seed + offset,
                    "evt_enabled": evt_on,
                }
            )
            scorer = _DetScorer(cfg).fit(events, atoms, train_quarters, graph_hash=graph.graph_hash)
            for i, pair in enumerate(top_pairs):
                rlz = scorer.score_pair(*pair)
                if rlz.components["mc"]["hit_count"] < 50:
                    continue
                evt = rlz.components.get("evt")
                if evt_on and evt and evt.get("stable") and evt.get("q99_evt"):
                    per_pair[i].append(evt["q99_evt"])
                elif rlz.loss_quantiles.get("0.99"):
                    per_pair[i].append(rlz.loss_quantiles["0.99"])
        return [v for v in per_pair if len(v) >= 2]

    def _cv(estimates: list[list[float]]) -> float | None:
        cvs = []
        for vals in estimates:
            mean = sum(vals) / len(vals)
            if mean > 0:
                var = sum((v - mean) ** 2 for v in vals) / (len(vals) - 1)
                cvs.append(math.sqrt(var) / mean)
        return sum(cvs) / len(cvs) if cvs else None

    cv_evt = _cv(_tail_estimates(True))
    cv_emp = _cv(_tail_estimates(False))
    # mean metrics must be flat: p_hat does not depend on the EVT overlay
    a3_consistent = (
        cv_evt is not None and cv_emp is not None and cv_evt <= cv_emp * 1.05
    )
    ablations["A3"] = {
        "removes": "EVT tail layer (GPD overlay on MC loss sample)",
        "expectation": "tail quantiles unstable without it; mean metrics flat",
        "observed": {
            "q99_cv_with_evt": round(cv_evt, 4) if cv_evt is not None else None,
            "q99_cv_without_evt": round(cv_emp, 4) if cv_emp is not None else None,
            "pairs_measured": len(top_pairs),
            "seeds": 3,
        },
        "consistent": a3_consistent,
        "note": "p_hat is unaffected by the overlay by construction (mean "
        "metrics flat); stability measured on q99 across MC seeds.",
    }

    # --- historical rows (loop removed 2026-08-01) -------------------------
    for key in ("A2", "A6", "A4"):
        ablations[key] = dict(_HISTORICAL[key])

    # --- A5: gated on the modifier policy path ------------------------------
    # --- A5: gated on the modifier policy path ------------------------------
    ablations["A5"] = {
        "removes": "external features",
        "expectation": "Task B small drop if any",
        "observed": None,
        "consistent": None,
        "note": "not_implemented: the external feature registry + FDR screen "
        "exists (ext_features.py), but accepted associational_features are "
        "not wired into scores — spec 10.2 requires human/policy accept "
        "before any modifier applies. The ablation activates with the "
        "modifier path.",
    }

    n_historical = sum(1 for a in ablations.values() if a.get("historical"))
    n_consistent = sum(
        1 for a in ablations.values() if a["consistent"] is True and not a.get("historical")
    )
    n_runnable = sum(
        1 for a in ablations.values() if a["consistent"] is not None and not a.get("historical")
    )

    # --- A6 sensitivity rows: kappa / pi0 x c through Task B + B-cal -------
    sensitivity = None
    if truth is not None:
        sensitivity = run_sensitivity(
            atoms, events, truth,
            holdout_quarters=holdout_quarters, scorer_config=scfg,
        )

    report = {
        "eval_version": "1.0",
        "data_regime": regime,
        "algorithm_version": ALGORITHM_VERSION,
        "seed": scfg.seed,
        "ablation_order": list(ABLATION_ORDER),
        "baseline": {
            "ap_b3_full": ap_b3_full,
            "task_b_pass": full.report["task_b"]["pass"],
        },
        "ablations": {k: ablations[k] for k in ABLATION_ORDER},
        "sensitivity": sensitivity,
        "summary": {
            "n_runnable": n_runnable,
            "n_consistent": n_consistent,
            "n_historical": n_historical,
            "n_not_implemented": sum(
                1 for a in ablations.values() if a["consistent"] is None and a.get("note", "").startswith("not_implemented")
            ),
        },
    }
    return AblationReport(report=report, report_md=_render_md(report))


def run_sensitivity(
    atoms,
    events,
    truth: dict,
    *,
    holdout_quarters: int = 8,
    scorer_config: ScorerConfig | None = None,
) -> dict:
    """Pre-registered sensitivity rows (A6) + the A1 challenger-vs-primary
    B-cal comparison (the promotion instrument — reported, not acted on;
    the primary estimator is unchanged by this table)."""
    from .task_bcal import run_bcal

    scfg = scorer_config or ScorerConfig()
    rows: list[dict] = []

    def _row(cfg: ScorerConfig, **tags):
        ev = run_evaluation(
            atoms, events, truth=None,
            holdout_quarters=holdout_quarters, config=cfg, n_boot=0,
        )
        bc = run_bcal(
            atoms, events, truth,
            holdout_quarters=holdout_quarters, config=cfg,
            dependence_modes=(cfg.dependence_mode,),
        ).report["modes"][cfg.dependence_mode]
        return {
            **tags,
            "ap_b3": ev.report["task_b"]["baselines"]["B3_det_batch"]["average_precision"],
            "coverage_90": bc["coverage_90"],
            "mean_bias": bc["mean_bias"],
            "supported_rare_coverage": bc["strata"]["supported_rare"]["coverage_90"],
            "zero_support_coverage": bc["strata"]["zero_support"]["coverage_90"],
            "p_hat_above_hi_rows": bc["p_hat_above_hi_rows"],
            "bcal_pass": bc["pass"],
        }

    for kappa in KAPPA_SENSITIVITY:
        rows.append(
            _row(
                ScorerConfig(**{**scfg.__dict__, "shrink_kappa": kappa}),
                kind="kappa",
                dependence_mode="shrunk_empirical",
                kappa=kappa,
            )
        )
    for pi0 in MIXTURE_PI0_SENSITIVITY:
        for c in MIXTURE_C_SENSITIVITY:
            rows.append(
                _row(
                    ScorerConfig(
                        **{
                            **scfg.__dict__,
                            "dependence_mode": "mixture",
                            "mixture_pi0": pi0,
                            "mixture_c": c,
                        }
                    ),
                    kind="mixture",
                    dependence_mode="mixture",
                    pi0=pi0,
                    c=c,
                )
            )

    challenger = run_bcal(
        atoms,
        events,
        truth,
        holdout_quarters=holdout_quarters,
        config=scfg,
        dependence_modes=("shrunk_empirical", "mixture"),
    ).report

    return {
        "description": (
            "kappa / pi0 x c sensitivity through Task B + B-cal, and the A1 "
            "challenger-vs-primary comparison (pre-registered per "
            "HYBRID_SCORING_PROPOSAL.md; promotion is a separate gated "
            "decision — the primary is unchanged)"
        ),
        "rows": rows,
        "challenger_vs_primary": challenger["modes"],
    }


def _render_md(report: dict) -> str:
    lines = [
        "# Ablations (evaluation-plan.md section 5)",
        "",
        f"- data regime: **{report['data_regime']}** · seed `{report['seed']}`",
        f"- baseline: AP(B3) {report['baseline']['ap_b3_full']}",
        "- A2, A4, A6 are historical rows (multiverse loop removed from mainline 2026-08-01).",
        "",
        "| ablation | removes | expectation | observed | consistent |",
        "|----------|---------|-------------|----------|------------|",
    ]
    for key in report["ablation_order"]:
        a = report["ablations"][key]
        if a["observed"] is None:
            obs = a.get("note", "—")
            cons = "—"
        else:
            obs = json.dumps(a["observed"], sort_keys=True)
            cons = ("yes (hist.)" if a.get("historical") else "yes") if a["consistent"] else "NO"
        lines.append(f"| {key} | {a['removes']} | {a['expectation']} | {obs} | {cons} |")
    s = report["summary"]
    lines += [
        "",
        f"**{s['n_consistent']}/{s['n_runnable']} runnable ablations consistent "
        f"with the design; {s['n_historical']} historical rows; "
        f"{s['n_not_implemented']} not yet implemented.**",
        "",
    ]
    sens = report.get("sensitivity")
    if sens:
        lines += [
            "## Sensitivity (pre-registered kappa / pi0 x c) + A1 challenger comparison",
            "",
            "| kind | params | AP(B3) | coverage | bias | supp-rare cov | zero-supp cov | p̂>p_hi |",
            "|------|--------|--------|----------|------|---------------|---------------|---------|",
        ]
        for r in sens["rows"]:
            params = (
                f"κ={r['kappa']}" if r["kind"] == "kappa" else f"π₀={r['pi0']} c={r['c']}"
            )
            lines.append(
                f"| {r['dependence_mode']} | {params} | {r['ap_b3']} | "
                f"{r['coverage_90']} | {r['mean_bias']} | "
                f"{r['supported_rare_coverage']} | {r['zero_support_coverage']} | "
                f"{r['p_hat_above_hi_rows']} |"
            )
        lines += ["", "### Challenger vs primary (B-cal)", ""]
        for mode, m in sens["challenger_vs_primary"].items():
            sr = m["strata"]["supported_rare"]
            lines.append(
                f"- `{mode}`: coverage {m['coverage_90']} "
                f"({'PASS' if m['pass'] else 'FAIL'}) · bias {m['mean_bias']} · "
                f"supported-rare {sr['coverage_90']} (n={sr['n']}) · "
                f"zero-support {m['strata']['zero_support']['coverage_90']} · "
                f"p̂>p_hi rows {m['p_hat_above_hi_rows']}"
            )
        lines += [
            "",
            "Challenger table is the promotion instrument from "
            "HYBRID_SCORING_PROPOSAL.md — reported only; the primary "
            "estimator is unchanged by it.",
            "",
        ]
    return "\n".join(lines)


def write_ablation_outputs(result: AblationReport, out_dir: str | Path) -> dict[str, Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    json_path = out / "ablations.json"
    md_path = out / "ablations.md"
    json_path.write_text(json.dumps(result.report, indent=2, sort_keys=True))
    md_path.write_text(result.report_md)
    return {"json": json_path, "md": md_path}
