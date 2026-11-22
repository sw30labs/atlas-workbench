"""Evaluation harness — Task B (pair co-occurrence ranking) and Task C
(synthetic structure recovery) from docs/research/redesignv1/evaluation-plan.md.

Task B is the core product claim under test: a dependence-aware scorer must
rank held-out pair co-occurrence better than independence (B1) by a
pre-registered margin (>= 15% relative average precision), or the redesign
thesis fails. Task C checks that the planted channel structure on R0 can be
recovered from loss data alone (precision >= 0.7 at #planted edges).

Temporal discipline: the scorer is fit on train quarters only; labels come
from the holdout window only. The test window is never used for fitting.
"""

from __future__ import annotations

import json
import random
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import ALGORITHM_VERSION
from .candidates import BASELINES, ORACLE_BASELINES, all_pairs, baseline_scores
from .channels import build_channel_graph, recover_empirical_edges
from .models import IncidentEvent, RiskAtom
from .scorer import DetScorer, ScorerConfig

# Pre-registered thresholds (evaluation-plan.md sections 3.2-3.3).
AP_LIFT_THRESHOLD = 0.15  # B3 vs B1 relative average-precision lift
RECOVERY_PRECISION_THRESHOLD = 0.7
TOP_DECILE_PLANTED_THRESHOLD = 0.7
TOP_K = 20
BOOTSTRAP_DRAWS = 1_000  # pair-resampling bootstrap for metric CIs (A6)


# ---------------------------------------------------------------------------
# Metrics (pure stdlib)
# ---------------------------------------------------------------------------


def _sorted_pairs(scores: dict[tuple[str, str], float]) -> list[tuple[str, str]]:
    """Deterministic ranking: score desc, pair id asc as tie-break."""
    return [p for p, _ in sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))]


def average_precision(
    labels: dict[tuple[str, str], int], scores: dict[tuple[str, str], float]
) -> float | None:
    """Mean of precision@rank at each positive, over all positives."""
    ranked = _sorted_pairs(scores)
    n_pos = sum(labels.get(p, 0) for p in ranked)
    if n_pos == 0:
        return None
    tp = 0
    ap = 0.0
    for rank, pair in enumerate(ranked, start=1):
        if labels.get(pair, 0):
            tp += 1
            ap += tp / rank
    return ap / n_pos


def auroc(
    labels: dict[tuple[str, str], int], scores: dict[tuple[str, str], float]
) -> float | None:
    """Mann-Whitney AUROC with average ranks for tied scores."""
    items = sorted(scores.items(), key=lambda kv: (kv[1], kv[0]))
    ranks: dict[tuple[str, str], float] = {}
    i = 0
    while i < len(items):
        j = i
        while j + 1 < len(items) and items[j + 1][1] == items[i][1]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[items[k][0]] = avg
        i = j + 1
    pos = [p for p in scores if labels.get(p, 0)]
    neg = [p for p in scores if not labels.get(p, 0)]
    if not pos or not neg:
        return None
    rank_sum_pos = sum(ranks[p] for p in pos)
    u = rank_sum_pos - len(pos) * (len(pos) + 1) / 2.0
    return u / (len(pos) * len(neg))


def topk_precision(
    labels: dict[tuple[str, str], int],
    scores: dict[tuple[str, str], float],
    k: int = TOP_K,
) -> float | None:
    ranked = _sorted_pairs(scores)[:k]
    if not ranked:
        return None
    return sum(labels.get(p, 0) for p in ranked) / len(ranked)


# ---------------------------------------------------------------------------
# Bootstrap CIs (A6)
# ---------------------------------------------------------------------------


def bootstrap_metric_cis(
    labels: dict[tuple[str, str], int],
    scores_by_baseline: dict[str, dict[tuple[str, str], float]],
    *,
    n_boot: int = BOOTSTRAP_DRAWS,
    seed: int = 0,
    ap_pair: tuple[str, str] = ("B3_det_batch", "B1_independence"),
    top_k: int = TOP_K,
) -> dict:
    """Percentile bootstrap CIs (95%) over pair resampling, seeded.

    Resamples pairs with replacement; duplicate draws become distinct items
    sharing score and label, so the multiset metrics stay consistent with
    the deterministic ranking (score desc, pair id asc).
    """
    pairs = sorted(labels)
    rng = random.Random(f"{seed}:bootstrap")
    metric_names = ("average_precision", "auroc", "precision_at_k")
    acc: dict[str, dict[str, list]] = {
        name: {m: [] for m in metric_names} for name in scores_by_baseline
    }
    lifts: list[float] = []
    n = len(pairs)
    for _ in range(n_boot):
        sub_labels: dict[tuple[str, str], int] = {}
        sub_scores: dict[str, dict[tuple[str, str], float]] = {
            name: {} for name in scores_by_baseline
        }
        for j in range(n):
            p = pairs[rng.randrange(n)]
            key = (p[0], f"{p[1]}#{j}")
            sub_labels[key] = labels[p]
            for name, sc in scores_by_baseline.items():
                sub_scores[name][key] = sc[p]
        for name, sc in sub_scores.items():
            acc[name]["average_precision"].append(average_precision(sub_labels, sc))
            acc[name]["auroc"].append(auroc(sub_labels, sc))
            acc[name]["precision_at_k"].append(topk_precision(sub_labels, sc, k=top_k))
        a, b = ap_pair
        ap_a = acc[a]["average_precision"][-1]
        ap_b = acc[b]["average_precision"][-1]
        if ap_a is not None and ap_b:
            lifts.append((ap_a - ap_b) / ap_b)

    def _ci(samples: list[float | None]) -> list[float] | None:
        xs = sorted(s for s in samples if s is not None)
        if not xs:
            return None
        return [
            _round(xs[int(0.025 * (len(xs) - 1))]),
            _round(xs[int(0.975 * (len(xs) - 1))]),
        ]

    return {
        "n_boot": n_boot,
        "ci_level": 0.95,
        "baselines": {
            name: {m: _ci(vals[m]) for m in metric_names}
            for name, vals in acc.items()
        },
        "ap_lift_b3_vs_b1_relative": _ci(lifts),
    }


# ---------------------------------------------------------------------------
# Labels and splits
# ---------------------------------------------------------------------------


def pair_labels(
    events: list[IncidentEvent],
    test_start: int,
    n_periods: int,
    pairs: list[tuple[str, str]],
) -> dict[tuple[str, str], int]:
    """Positive if both atoms have >= 1 event in the same holdout quarter."""
    by_period: dict[int, set[str]] = {}
    for e in events:
        if test_start <= e.period < n_periods:
            by_period.setdefault(e.period, set()).add(e.risk_id)
    cooccurring: set[tuple[str, str]] = set()
    for fired in by_period.values():
        for a in fired:
            for b in fired:
                if a < b:
                    cooccurring.add((a, b))
    return {p: (1 if p in cooccurring else 0) for p in pairs}


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


@dataclass
class EvalResult:
    report: dict
    report_md: str


def evaluation_verdicts(
    ranking_pass: bool | None,
    calibration_report: dict | None,
    dependence_mode: str,
) -> dict:
    """Keep ranking evidence, probability calibration and deployment distinct.

    Neither synthetic recovery nor public-report prediction validates an
    enterprise's operational event likelihoods or usefulness to operators.
    """
    calibration = ((calibration_report or {}).get("modes") or {}).get(dependence_mode)
    calibration_pass = calibration.get("pass") if calibration else None

    def status(value: bool | None) -> str:
        return "not_validated" if value is None else "pass" if value else "fail"

    return {
        "ranking": {
            "status": status(ranking_pass),
            "scope": "held_out_co_occurrence_ranking_and_available_structure_checks",
        },
        "calibration": {
            "status": status(calibration_pass),
            "scope": "compound_probability_levels_in_the_tested_data_regime",
            "coverage_90": calibration.get("coverage_90") if calibration else None,
        },
        "enterprise_readiness": {
            "status": "fail" if ranking_pass is False or calibration_pass is False else "not_validated",
            "reasons": [
                "Enterprise operational probabilities require independent calibration on relevant observations.",
                "Prospective enterprise validation and human usefulness assessment are not provided by this harness.",
            ],
        },
    }


def _git_sha(cwd: str | None = None) -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=10,
        )
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def run_evaluation(
    atoms: list[RiskAtom],
    events: list[IncidentEvent],
    *,
    truth: dict | None = None,
    holdout_quarters: int = 8,
    config: ScorerConfig | None = None,
    regime: str = "R0",
    repo_dir: str | None = None,
    n_boot: int = BOOTSTRAP_DRAWS,
) -> EvalResult:
    """Run Task B (+ Task C when a truth manifest is provided).

    With ``truth`` the level tasks run alongside: Task B-cal (compound p̂
    coverage/bias vs closed-form generative truth) and Task L (severity
    recovery + holdout log-likelihood) — the instruments A6 adds (E4/E7).
    """
    cfg = config or ScorerConfig()
    n_periods_total = max((e.period for e in events), default=-1) + 1
    if n_periods_total <= holdout_quarters:
        raise ValueError(
            f"need more than {holdout_quarters} quarters of events, got {n_periods_total}"
        )
    train_quarters = n_periods_total - holdout_quarters

    # Channel graph from inventory tags (B2 baseline + graph hash). On R0
    # the scorer never sees the tags; it infers dependence from train data.
    graph = build_channel_graph(atoms, graph_id="g_ch", version=1)

    scorer = DetScorer(cfg).fit(
        events,
        atoms,
        train_quarters,
        graph_hash=graph.graph_hash,
        feature_registry_hash="",
    )

    pairs = all_pairs(atoms)
    labels = pair_labels(events, train_quarters, n_periods_total, pairs)
    n_pos = sum(labels.values())

    baseline_rows: dict[str, dict] = {}
    scores_by_baseline: dict[str, dict[tuple[str, str], float]] = {}
    b3_scores: dict[tuple[str, str], float] = {}
    for name in BASELINES:
        scores = baseline_scores(name, pairs, scorer, atoms, seed=cfg.seed)
        scores_by_baseline[name] = scores
        baseline_rows[name] = {
            "average_precision": _round(average_precision(labels, scores)),
            "auroc": _round(auroc(labels, scores)),
            f"precision_at_{TOP_K}": _round(topk_precision(labels, scores)),
        }
        if name == "B3_det_batch":
            b3_scores = scores

    ap_b1 = baseline_rows["B1_independence"]["average_precision"]
    ap_b3 = baseline_rows["B3_det_batch"]["average_precision"]
    ap_lift = None
    if ap_b1 is not None and ap_b3 is not None and ap_b1 > 0:
        ap_lift = (ap_b3 - ap_b1) / ap_b1
    task_b_pass = ap_lift is not None and ap_lift >= AP_LIFT_THRESHOLD

    bootstrap = (
        bootstrap_metric_cis(labels, scores_by_baseline, n_boot=n_boot, seed=cfg.seed)
        if n_boot > 0
        else None
    )

    task_b = {
        "description": "pair co-occurrence ranking on holdout",
        "n_pairs": len(pairs),
        "n_holdout_positives": n_pos,
        "base_rate": _round(n_pos / len(pairs)) if pairs else None,
        "baselines": baseline_rows,
        "ap_lift_b3_vs_b1_relative": _round(ap_lift),
        "pass_threshold": AP_LIFT_THRESHOLD,
        "pass": task_b_pass,
        "bootstrap": bootstrap,
        # A6/E1: channel tags are planted truth on R0 — baselines reading
        # them are partially oracle; production tags come from keyword rules.
        "oracle_baselines": list(ORACLE_BASELINES),
        "metric_note": (
            "AP/AUROC/P@20 as implemented: deterministic ranking "
            "(score desc, pair id asc); positive = both atoms fire in the "
            "same holdout quarter. B5 = #shared channels x N_S with "
            "independence tie-break."
        ),
    }

    task_c: dict | None = None
    if truth is not None:
        planted = {tuple(sorted(e)) for e in truth["planted_edges"]}
        recovered = recover_empirical_edges(
            [e for e in events if e.period < train_quarters],
            [a.risk_id for a in atoms],
            train_quarters,
            top_n=len(planted),
        )
        recovered_set = {tuple(sorted(e)) for e in recovered}
        precision = (
            len(recovered_set & planted) / len(planted) if planted else None
        )
        n_top = max(1, -(-len(pairs) // 10))  # top decile, ceil
        top_decile = set(_sorted_pairs(b3_scores)[:n_top])
        planted_in_top = len(top_decile & planted) / n_top if n_top else None
        task_c_pass = (
            precision is not None
            and precision >= RECOVERY_PRECISION_THRESHOLD
            and planted_in_top is not None
            and planted_in_top >= TOP_DECILE_PLANTED_THRESHOLD
        )
        task_c = {
            "description": "planted channel structure recovery from train data",
            "n_planted_edges": len(planted),
            "recovered_at_planted_count": len(recovered_set),
            "precision_at_planted_count": _round(precision),
            "precision_threshold": RECOVERY_PRECISION_THRESHOLD,
            # A6/E6: report the base rate the metric must beat
            "random_baseline_precision": (
                _round(len(planted) / len(pairs)) if pairs else None
            ),
            "metric_note": (
                "lift-ranked empirical edges (N_ij*T/(N_i*N_j)), top_n = "
                "#planted; top-decile share uses the B3 U-ranking."
            ),
            "top_decile_size": n_top,
            "top_decile_planted_fraction": _round(planted_in_top),
            "top_decile_threshold": TOP_DECILE_PLANTED_THRESHOLD,
            "pass": task_c_pass,
        }

    # A6 level tasks: B-cal (compound levels vs truth) + Task L (severities)
    bcal_report: dict | None = None
    task_loss_report: dict | None = None
    if truth is not None:
        from .task_bcal import run_bcal
        from .task_loss import run_task_loss

        # B-cal's closed-form truth needs the R0 channel-shock generator
        # parameters; Task L needs per-atom severity parameters. Other
        # Other synthetic truth manifests skip unsupported level checks.
        if {"q_base", "shock_mult", "shock_add", "shock_pi"} <= set(truth):
            bcal_report = run_bcal(
                atoms,
                events,
                truth,
                holdout_quarters=holdout_quarters,
                config=cfg,
                dependence_modes=(cfg.dependence_mode,),
            ).report
        if {"sev_mu", "sev_sigma"} <= set(truth):
            task_loss_report = run_task_loss(
                atoms,
                events,
                truth,
                holdout_quarters=holdout_quarters,
                config=cfg,
                scorer=scorer,
            ).report

    passes = [task_b_pass] + ([task_c_pass] if task_c else [])
    if all(passes):
        recommendation = "go"
    elif any(passes):
        recommendation = "iterate"
    else:
        recommendation = "abandon"

    report = {
        "eval_version": "1.1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "data_regime": regime,
        "git_sha": _git_sha(repo_dir),
        "algorithm_version": ALGORITHM_VERSION,
        "model_config_hash": cfg.config_hash(),
        "seed": cfg.seed,
        "splits": {
            "n_quarters_total": n_periods_total,
            "train_quarters": [0, train_quarters - 1],
            "test_quarters": [train_quarters, n_periods_total - 1],
            "holdout_quarters": holdout_quarters,
        },
        "task_b": task_b,
        "task_c": task_c,
        "task_bcal": bcal_report,
        "task_loss": task_loss_report,
        "ablations": None,  # Phase 2 (evaluation-plan.md section 5)
        "fdr_summary": None,  # no external features in Phase 1
        "limitations": [
            "R1 Task B runs on the public signal-activation series "
            "(atlas-eval r1) but the demo store stays small-sample and "
            "directional-only; R0 remains the decision regime.",
            "EVT overlay (evt.py) is advisory: empirical MC quantiles stay "
            "authoritative; the overlay reports alongside or downgrades "
            "when unstable.",
            "alpha_overlap fixed at 1.0; severity dependence not modelled.",
            "p_interval: two-level posterior draws (marginal posteriors, "
            "then the dependence posterior given the drawn marginals), "
            "Fréchet-clipped per draw; posterior median reported beside the "
            "mean; p_hat > p_hi rows are flagged in "
            "components.interval.p_hat_above_hi.",
        ],
        "recommendation": recommendation,
        "recommendation_scope": "ranking_and_structure_only",
        "verdicts": evaluation_verdicts(all(passes), bcal_report, cfg.dependence_mode),
    }
    return EvalResult(report=report, report_md=_render_markdown(report))


def run_r0_multiseed(
    *,
    n_seeds: int = 10,
    base_seed: int = 20260801,
    holdout_quarters: int = 8,
    config: ScorerConfig | None = None,
    n_quarters: int = 40,
) -> dict:
    """Multi-seed R0 (A6): the go/no-go metrics as a *distribution* over
    generative seeds, not a single draw. Seeds are the pre-registered
    sequence base+i; per-seed bootstrap is skipped (the distribution is
    the interval)."""
    from .synthetic import R0Config, generate_r0

    rows: list[dict] = []
    for i in range(n_seeds):
        seed = base_seed + i
        ds = generate_r0(R0Config(n_quarters=n_quarters, seed=seed))
        cfg = ScorerConfig(**{**(config or ScorerConfig()).__dict__, "seed": seed})
        res = run_evaluation(
            ds.atoms,
            ds.events,
            truth=ds.truth,
            holdout_quarters=holdout_quarters,
            config=cfg,
            n_boot=0,
        )
        tb = res.report["task_b"]
        tc = res.report["task_c"] or {}
        rows.append(
            {
                "seed": seed,
                "ap_lift": tb["ap_lift_b3_vs_b1_relative"],
                "task_b_pass": tb["pass"],
                "task_c_precision": tc.get("precision_at_planted_count"),
                "task_c_pass": tc.get("pass"),
                "bcal_coverage": (
                    res.report["task_bcal"]["modes"][cfg.dependence_mode]["coverage_90"]
                    if res.report.get("task_bcal")
                    else None
                ),
                "recommendation": res.report["recommendation"],
            }
        )

    lifts = sorted(r["ap_lift"] for r in rows if r["ap_lift"] is not None)

    def _q(xs: list[float], p: float) -> float | None:
        return _round(xs[int(p * (len(xs) - 1))]) if xs else None

    n = len(rows)
    summary = {
        "n_seeds": n_seeds,
        "base_seed": base_seed,
        "ap_lift": {
            "min": _round(lifts[0]) if lifts else None,
            "q10": _q(lifts, 0.10),
            "median": _q(lifts, 0.50),
            "q90": _q(lifts, 0.90),
            "max": _round(lifts[-1]) if lifts else None,
        },
        "task_b_pass_fraction": _round(sum(bool(r["task_b_pass"]) for r in rows) / n),
        "task_c_pass_fraction": _round(sum(bool(r["task_c_pass"]) for r in rows) / n),
        "go_fraction": _round(sum(r["recommendation"] == "go" for r in rows) / n),
    }
    return {
        "task": "multi-seed R0",
        "description": (
            "Task B/C/B-cal as a distribution over generative seeds "
            "(pre-registered sequence base+i, per-seed bootstrap skipped)"
        ),
        "holdout_quarters": holdout_quarters,
        "n_quarters": n_quarters,
        "seeds": rows,
        "summary": summary,
    }


def _round(x: float | None, nd: int = 4) -> float | None:
    return None if x is None else round(x, nd)


def _render_markdown(report: dict) -> str:
    tb = report["task_b"]
    lines = [
        "# Eval Report — Contingency Atlas redesign Phase 1",
        "",
        f"- data regime: **{report['data_regime']}**",
        f"- algorithm_version: `{report['algorithm_version']}`",
        f"- git sha: `{report['git_sha']}`",
        f"- seed: `{report['seed']}`",
        "- recommendation scope: ranking and structure only; not enterprise readiness",
        (
            f"- split: train quarters {report['splits']['train_quarters']}, "
            f"test quarters {report['splits']['test_quarters']}"
        ),
        "",
        "## Task B — pair co-occurrence ranking (holdout)",
        "",
        f"pairs: {tb['n_pairs']} · holdout positives: {tb['n_holdout_positives']} "
        f"· base rate: {tb['base_rate']}",
        "",
        "| baseline | AP | AUROC | P@20 |",
        "|----------|----|-------|------|",
    ]
    boot = (tb.get("bootstrap") or {}).get("baselines") or {}
    for name, row in tb["baselines"].items():
        ci = boot.get(name, {})
        ci_txt = ""
        if ci.get("average_precision"):
            ci_txt = f" (AP 95% CI {ci['average_precision']})"
        oracle = " †" if name in tb.get("oracle_baselines", []) else ""
        lines.append(
            f"| {name}{oracle} | {row['average_precision']} | {row['auroc']} | "
            f"{row['precision_at_20']} |{ci_txt}"
        )
    lines += [
        "",
        "† reads channel tags — planted truth on R0 (partially oracle); "
        "production tags come from keyword rules.",
        "",
        (
            f"**AP lift B3 vs B1 (relative): {tb['ap_lift_b3_vs_b1_relative']}** "
            f"(pre-registered threshold >= {tb['pass_threshold']}) → "
            f"{'PASS' if tb['pass'] else 'FAIL'}"
        ),
    ]
    lift_ci = (tb.get("bootstrap") or {}).get("ap_lift_b3_vs_b1_relative")
    if lift_ci:
        lines.append(f"AP lift 95% bootstrap CI: {lift_ci}")
    lines.append("")
    if report.get("task_c"):
        tc = report["task_c"]
        lines += [
            "## Task C — planted structure recovery (R0)",
            "",
            (
                f"- precision@{tc['n_planted_edges']} planted edges: "
                f"{tc['precision_at_planted_count']} "
                f"(threshold >= {tc['precision_threshold']}; "
                f"random base rate {tc['random_baseline_precision']})"
            ),
            (
                f"- planted fraction of top-decile U ranking: "
                f"{tc['top_decile_planted_fraction']} "
                f"(threshold >= {tc['top_decile_threshold']})"
            ),
            f"- → {'PASS' if tc['pass'] else 'FAIL'}",
            "",
        ]
    if report.get("task_bcal"):
        bc = report["task_bcal"]
        lines += ["## Task B-cal — compound levels vs generative truth", ""]
        for mode, m in bc["modes"].items():
            sr = m["strata"].get("supported_rare", {})
            lines += [
                f"- `{mode}`: coverage {m['coverage_90']} "
                f"(band {m['coverage_band']}) · bias {m['mean_bias']} · "
                f"rmse {m['rmse']} · p̂>p_hi rows {m['p_hat_above_hi_rows']} → "
                f"{'PASS' if m['pass'] else 'FAIL'}",
                f"  - supported-rare stratum: n={sr.get('n')} coverage "
                f"{sr.get('coverage_90')} bias {sr.get('mean_bias')}",
            ]
        lines.append("")
    if report.get("task_loss"):
        ll = report["task_loss"]["holdout_loglik"]
        lines += [
            "## Task L — severities vs generative truth",
            "",
            (
                f"- holdout loglik fitted − pooled: {ll['fitted_minus_pooled']} "
                f"(> 0: shrinkage earns its keep) · fitted − truth: "
                f"{ll['fitted_minus_truth']} (oracle gap) · n={ll['n_amounts']}"
            ),
            "",
        ]
    lines += [
        "## Limitations",
        "",
    ]
    lines += [f"- {lim}" for lim in report["limitations"]]
    lines += [
        "",
        f"## Recommendation (ranking and structure only): **{report['recommendation'].upper()}**",
        "",
        "Go concerns the available ranking and structure checks only. It does not "
        "establish calibrated probabilities or enterprise readiness.",
        "",
    ]
    for name, verdict in report.get("verdicts", {}).items():
        lines.append(f"- {name.replace('_', ' ')}: **{verdict['status'].upper()}**")
    if report.get("interpretation"):
        lines.extend(["", report["interpretation"]])
    return "\n".join(lines)


def write_eval_outputs(result: EvalResult, out_dir: str | Path) -> dict[str, Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    json_path = out / "eval_report.json"
    md_path = out / "eval_report.md"
    json_path.write_text(json.dumps(result.report, indent=2, sort_keys=True))
    md_path.write_text(result.report_md)
    return {"json": json_path, "md": md_path}
