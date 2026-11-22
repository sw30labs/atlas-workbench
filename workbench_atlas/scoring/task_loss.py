"""Task L: loss-side evaluation against generative truth (A6, review E7).

L|S — the severity model given a scenario fires — has had no evaluation
task anywhere: every gate in the program measures occurrence, not loss.
On R0 the per-atom severity parameters are known (``r0_truth.json``
``sev_mu`` / ``sev_sigma``), so the fitted lognormals can be checked two
ways:

* **parameter recovery** — |mu_fit - mu_true|, |sigma_fit - sigma_true|
  per atom, summarized by severity source (cell_data / shrunk_to_pool /
  pooled_prior): the shrinkage ladder must not degrade as cells thicken;
* **holdout severity log-likelihood** — holdout loss amounts scored under
  the fitted per-atom lognormals vs the pooled model vs the truth
  (oracle ceiling). Fitted beating pooled is the quantity the P5
  proxy-prior kill rule will compare against later.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

from .evaluate import _round
from .models import IncidentEvent, RiskAtom
from .scorer import DetScorer, ScorerConfig, _fit_lognormal


def _lognorm_logpdf(x: float, mu: float, sigma: float) -> float:
    z = (math.log(x) - mu) / sigma
    return -math.log(x) - math.log(sigma) - 0.5 * math.log(2.0 * math.pi) - 0.5 * z * z


@dataclass
class TaskLossResult:
    report: dict
    report_md: str


def run_task_loss(
    atoms: list[RiskAtom],
    events: list[IncidentEvent],
    truth: dict,
    *,
    holdout_quarters: int = 8,
    config: ScorerConfig | None = None,
    scorer: DetScorer | None = None,
) -> TaskLossResult:
    """Evaluate fitted severities against r0_truth severity parameters."""
    cfg = config or ScorerConfig()
    n_periods_total = max((e.period for e in events), default=-1) + 1
    if n_periods_total <= holdout_quarters:
        raise ValueError(
            f"need more than {holdout_quarters} quarters of events, got {n_periods_total}"
        )
    train_quarters = n_periods_total - holdout_quarters
    if scorer is None:
        scorer = DetScorer(cfg).fit(events, atoms, train_quarters)

    sev_mu_true = truth["sev_mu"]
    sev_sigma_true = truth["sev_sigma"]

    per_atom: list[dict] = []
    for a in atoms:
        m = scorer.marginals[a.risk_id]
        per_atom.append(
            {
                "risk_id": a.risk_id,
                "sev_source": m.sev_source,
                "n_events": m.n_events,
                "mu_fit": _round(m.sev_mu, 6),
                "mu_true": _round(sev_mu_true[a.risk_id], 6),
                "abs_mu_error": _round(abs(m.sev_mu - sev_mu_true[a.risk_id]), 6),
                "sigma_fit": _round(m.sev_sigma, 6),
                "sigma_true": _round(sev_sigma_true[a.risk_id], 6),
                "abs_sigma_error": _round(abs(m.sev_sigma - sev_sigma_true[a.risk_id]), 6),
            }
        )

    by_source: dict[str, dict] = {}
    for row in per_atom:
        agg = by_source.setdefault(
            row["sev_source"], {"n": 0, "mu_err": 0.0, "sigma_err": 0.0}
        )
        agg["n"] += 1
        agg["mu_err"] += row["abs_mu_error"]
        agg["sigma_err"] += row["abs_sigma_error"]
    recovery = {
        src: {
            "n": agg["n"],
            "mean_abs_mu_error": _round(agg["mu_err"] / agg["n"], 6),
            "mean_abs_sigma_error": _round(agg["sigma_err"] / agg["n"], 6),
        }
        for src, agg in sorted(by_source.items())
    }

    train_amounts = [
        e.loss_amount
        for e in events
        if 0 <= e.period < train_quarters and e.loss_amount is not None and e.loss_amount > 0
    ]
    mu_pool, sigma_pool = _fit_lognormal(train_amounts)
    holdout_amounts = [
        e for e in events
        if e.period >= train_quarters and e.loss_amount is not None and e.loss_amount > 0
    ]
    ll_fitted = ll_pooled = ll_truth = 0.0
    for e in holdout_amounts:
        m = scorer.marginals[e.risk_id]
        ll_fitted += _lognorm_logpdf(e.loss_amount, m.sev_mu, m.sev_sigma)
        ll_pooled += _lognorm_logpdf(e.loss_amount, mu_pool, sigma_pool)
        ll_truth += _lognorm_logpdf(
            e.loss_amount, sev_mu_true[e.risk_id], sev_sigma_true[e.risk_id]
        )

    report = {
        "task": "L",
        "description": (
            "loss-side (severity) evaluation against generative truth: "
            "parameter recovery by shrinkage source + holdout log-likelihood "
            "fitted vs pooled vs truth"
        ),
        "holdout_quarters": holdout_quarters,
        "train_quarters": train_quarters,
        "parameter_recovery_by_source": recovery,
        "per_atom": per_atom,
        "holdout_loglik": {
            "n_amounts": len(holdout_amounts),
            "fitted": _round(ll_fitted, 3),
            "pooled": _round(ll_pooled, 3),
            "truth": _round(ll_truth, 3),
            "fitted_minus_pooled": _round(ll_fitted - ll_pooled, 3),
            "fitted_minus_truth": _round(ll_fitted - ll_truth, 3),
            "interpretation": (
                "fitted_minus_pooled > 0: per-atom shrinkage beats one pooled "
                "severity model. fitted_minus_truth <= 0 by construction "
                "(truth is the oracle ceiling)."
            ),
        },
    }
    return TaskLossResult(report=report, report_md=_render_loss_md(report))


def _render_loss_md(report: dict) -> str:
    lines = [
        "# Task L — loss-side evaluation vs generative truth",
        "",
        "## Parameter recovery (by severity source)",
        "",
        "| source | n | mean |mu err| | mean |sigma err| |",
        "|--------|---|---------------|------------------|",
    ]
    for src, r in report["parameter_recovery_by_source"].items():
        lines.append(
            f"| {src} | {r['n']} | {r['mean_abs_mu_error']} | {r['mean_abs_sigma_error']} |"
        )
    ll = report["holdout_loglik"]
    lines += [
        "",
        "## Holdout severity log-likelihood",
        "",
        f"- amounts: {ll['n_amounts']}",
        f"- fitted: {ll['fitted']} · pooled: {ll['pooled']} · truth: {ll['truth']}",
        f"- fitted − pooled: {ll['fitted_minus_pooled']} "
        f"(> 0 means shrinkage earns its keep)",
        f"- fitted − truth: {ll['fitted_minus_truth']} (oracle gap)",
        "",
    ]
    return "\n".join(lines)


def write_loss_outputs(result: TaskLossResult, out_dir: str | Path) -> dict[str, Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    json_path = out / "task_loss.json"
    md_path = out / "task_loss.md"
    json_path.write_text(json.dumps(result.report, indent=2, sort_keys=True))
    md_path.write_text(result.report_md)
    return {"json": json_path, "md": md_path}
