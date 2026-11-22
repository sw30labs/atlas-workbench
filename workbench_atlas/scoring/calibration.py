"""Task A — atom frequency calibration (evaluation-plan.md section 3.1).

Per-cell count forecasts on the holdout window. Per spec section 3.1 the
count family is Poisson **or negative binomial**; each atom is fitted by
moments and assigned NB when overdispersed (var > mean), else Poisson:

* **Negative log-likelihood** on holdout vs the naive global-mean baseline
  (works across both families; must beat baseline).
* **Poisson deviance** kept for continuity with the Phase 1 report.
* **PIT** per family; the histogram should be near-uniform.
* **90% interval coverage** from family quantiles. Pass band [0.8, 0.95].
* **Brier score on occurrence** (section 11.2): the sparse-regime view.

Fits use train quarters only. Pure stdlib. Runs on any IncidentEvent
series — R0/R0' and the public synthetic scoring-cell demo.

Known regime caveat (reported, not hidden): on regimes with homogeneous
marginals the pooled baseline is near-optimal and per-atom fits cannot
beat it — the report says so instead of forcing a pass.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

from .models import IncidentEvent

COVERAGE_BAND = (0.80, 0.95)


def _poisson_pmf(lam: float, k: int) -> float:
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    return math.exp(-lam + k * math.log(lam) - math.lgamma(k + 1))


def _poisson_cdf(lam: float, k: int) -> float:
    return sum(_poisson_pmf(lam, i) for i in range(0, k + 1))


def _poisson_quantile(lam: float, p: float) -> int:
    cdf = 0.0
    k = 0
    while k < 10_000:
        cdf += _poisson_pmf(lam, k)
        if cdf >= p:
            return k
        k += 1
    return k


# ---------------------------------------------------------------------------
# Negative binomial (spec section 3.1: "Poisson or negative-binomial")
# ---------------------------------------------------------------------------


def _nb_fit(counts: list[float]) -> tuple[float, float | None]:
    """Moment fit: returns (mean, r) with r=None for the Poisson limit
    (variance <= mean). NB parametrization: mean lam, variance lam(1+lam/r).
    """
    n = len(counts)
    if n == 0:
        return 0.0, None
    mean = sum(counts) / n
    if n < 2 or mean <= 0:
        return mean, None
    var = sum((c - mean) ** 2 for c in counts) / (n - 1)
    if var <= mean:
        return mean, None
    r = mean * mean / (var - mean)
    return mean, max(r, 1e-6)


def _nb_pmf(lam: float, r: float | None, k: int) -> float:
    if r is None:
        return _poisson_pmf(lam, k)
    # P(X=k) = C(k+r-1, k) (r/(r+lam))^r (lam/(r+lam))^k
    p = lam / (r + lam)
    return math.exp(
        math.lgamma(k + r) - math.lgamma(r) - math.lgamma(k + 1)
        + r * math.log(r / (r + lam))
        + k * math.log(p)
    )


def _nb_quantile(lam: float, r: float | None, p: float) -> int:
    cdf = 0.0
    k = 0
    while k < 10_000:
        cdf += _nb_pmf(lam, r, k)
        if cdf >= p:
            return k
        k += 1
    return k


def _nb_cdf(lam: float, r: float | None, k: int) -> float:
    return sum(_nb_pmf(lam, r, i) for i in range(0, k + 1))


def _deviance(counts: list[int], lam: float) -> float:
    total = 0.0
    for k in counts:
        if k > 0 and lam > 0:
            total += 2 * (k * math.log(k / lam) - (k - lam))
        elif lam > 0:
            total += 2 * lam
    return total


@dataclass
class CalibrationResult:
    report: dict
    report_md: str


def calibrate_atoms(
    events: list[IncidentEvent],
    risk_ids: list[str],
    *,
    train_quarters: int,
    n_periods: int,
    seed: int = 20260801,
    eb_strength: float = 4.0,
) -> CalibrationResult:
    """Fit per-atom Poisson rates on train; evaluate on the holdout.

    Rates are empirical-Bayes shrunk toward the pooled mean rate with
    ``eb_strength`` pseudo-quarters (0 = raw per-atom MLE)."""
    test_quarters = n_periods - train_quarters
    if test_quarters <= 0:
        raise ValueError("need a holdout window")

    # counts per atom per quarter (train for fit, test for eval)
    train_counts: dict[str, dict[int, int]] = {r: {} for r in risk_ids}
    test_counts: dict[str, dict[int, int]] = {r: {} for r in risk_ids}
    for e in events:
        target = train_counts if e.period < train_quarters else test_counts
        cell = target.setdefault(e.risk_id, {})
        cell[e.period] = cell.get(e.period, 0) + 1

    total_train = sum(sum(train_counts[r].values()) for r in risk_ids)
    pooled_rate = total_train / max(len(risk_ids) * train_quarters, 1)
    rates: dict[str, float] = {}
    all_train = []
    for r in risk_ids:
        n = sum(train_counts[r].values())
        if eb_strength > 0:
            rates[r] = (n + eb_strength * pooled_rate) / (train_quarters + eb_strength)
        else:
            rates[r] = n / train_quarters
        all_train.append(n / train_quarters)
    lam_naive = sum(all_train) / len(all_train) if all_train else 0.0
    _, r_naive = _nb_fit(all_train)

    rng = random.Random(seed)
    pit_values: list[float] = []
    covered = 0
    n_obs = 0
    dev_model = 0.0
    dev_naive = 0.0
    nll_model = 0.0
    nll_naive = 0.0
    brier_model = 0.0
    brier_naive = 0.0
    per_atom: dict[str, dict] = {}
    p_naive = 1.0 - math.exp(-lam_naive) if lam_naive > 0 else 0.0
    for r in risk_ids:
        lam = max(rates[r], 1e-9)
        # family by moments on the train count series (spec: Poisson or NB)
        train_series = [train_counts[r].get(t, 0) for t in range(train_quarters)]
        _, r_atom = _nb_fit(train_series)
        family = "nb" if r_atom is not None else "poisson"
        lo = _nb_quantile(lam, r_atom, 0.05)
        hi = _nb_quantile(lam, r_atom, 0.95)
        counts = [test_counts[r].get(t, 0) for t in range(train_quarters, n_periods)]
        atom_covered = 0
        p_model = 1.0 - _nb_pmf(lam, r_atom, 0)
        for k in counts:
            n_obs += 1
            if lo <= k <= hi:
                covered += 1
                atom_covered += 1
            u = rng.random()
            pit = _nb_cdf(lam, r_atom, k - 1) + u * _nb_pmf(lam, r_atom, k)
            pit_values.append(pit)
            fired = 1.0 if k > 0 else 0.0
            brier_model += (p_model - fired) ** 2
            brier_naive += (p_naive - fired) ** 2
            nll_model -= math.log(max(_nb_pmf(lam, r_atom, k), 1e-300))
            nll_naive -= math.log(max(_nb_pmf(lam_naive, r_naive, k), 1e-300))
        dev_model += _deviance(counts, lam)
        dev_naive += _deviance(counts, max(lam_naive, 1e-9))
        per_atom[r] = {
            "lambda_train": round(rates[r], 4),
            "family": family,
            "nb_r": round(r_atom, 4) if r_atom is not None else None,
            "interval_90": [lo, hi],
            "coverage": round(atom_covered / len(counts), 4) if counts else None,
            "test_total": sum(counts),
        }

    coverage = covered / n_obs if n_obs else None
    pit_hist = [0] * 10
    for v in pit_values:
        pit_hist[min(int(v * 10), 9)] += 1
    pit_hist = [c / len(pit_values) for c in pit_hist] if pit_values else pit_hist

    beats_naive = nll_model < nll_naive
    coverage_ok = coverage is not None and COVERAGE_BAND[0] <= coverage <= COVERAGE_BAND[1]
    passed = beats_naive and coverage_ok
    brier_model /= max(n_obs, 1)
    brier_naive /= max(n_obs, 1)
    interpretation = []
    if coverage is not None and coverage > COVERAGE_BAND[1]:
        interpretation.append(
            "coverage over-covers: for sparse cells the discrete 90% "
            "interval is almost always [0, 1], so the strict band is "
            "unreachable — a spec limitation at low rates, not necessarily "
            "model failure. Use the Brier comparison as the sparse-regime view."
        )
    if not beats_naive:
        interpretation.append(
            "model NLL loses to the pooled naive rate: at short train windows "
            "per-atom fits are noisy, and pooling wins when true rates are "
            "homogeneous — near-optimal there, so no per-atom model can pass. "
            "EB shrinkage (eb_strength) narrows the gap."
        )

    n_nb = sum(1 for a in per_atom.values() if a["family"] == "nb")
    report = {
        "task": "A — atom frequency calibration",
        "train_quarters": train_quarters,
        "test_quarters": test_quarters,
        "n_atoms": len(risk_ids),
        "n_observations": n_obs,
        "n_nb_atoms": n_nb,
        "n_poisson_atoms": len(risk_ids) - n_nb,
        "nll_model": round(nll_model, 4),
        "nll_naive": round(nll_naive, 4),
        "nll_beats_naive": beats_naive,
        "poisson_deviance_model": round(dev_model, 4),
        "poisson_deviance_naive": round(dev_naive, 4),
        "coverage_90": round(coverage, 4) if coverage is not None else None,
        "coverage_band": list(COVERAGE_BAND),
        "coverage_ok": coverage_ok,
        "pit_histogram": [round(v, 4) for v in pit_hist],
        "brier_model": round(brier_model, 6),
        "brier_naive": round(brier_naive, 6),
        "brier_beats_naive": brier_model < brier_naive,
        "eb_strength": eb_strength,
        "per_atom": per_atom,
        "pass": passed,
        "pass_rule": "coverage in [0.8, 0.95] AND model NLL beats naive rate (Poisson or NB family by moments)",
        "interpretation": interpretation,
    }
    md = "\n".join(
        [
            "# Task A — atom frequency calibration",
            "",
            f"- atoms: {len(risk_ids)} ({n_nb} NB, {len(risk_ids) - n_nb} Poisson) "
            f"· observations: {n_obs} "
            f"(train {train_quarters}q, test {test_quarters}q)",
            f"- NLL: model {report['nll_model']} "
            f"vs naive {report['nll_naive']} "
            f"→ {'beats baseline' if beats_naive else 'LOSES to baseline'}",
            f"- Poisson deviance (continuity): model {report['poisson_deviance_model']} "
            f"vs naive {report['poisson_deviance_naive']}",
            f"- 90% coverage: {report['coverage_90']} "
            f"(band {COVERAGE_BAND}) → {'ok' if coverage_ok else 'out of band'}",
            f"- Brier (occurrence): model {report['brier_model']} "
            f"vs naive {report['brier_naive']} "
            f"→ {'beats baseline' if report['brier_beats_naive'] else 'LOSES'}",
            f"- PIT deciles: {report['pit_histogram']}",
            "",
        ]
        + [f"- {note}" for note in interpretation]
        + [
            "",
            f"## Strict rule: {'PASS' if passed else 'FAIL'}",
            "",
        ]
    )
    return CalibrationResult(report=report, report_md=md)
