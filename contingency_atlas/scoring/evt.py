"""Layer 5 — tail treatment (algorithm-spec.md section 6).

Peaks-over-threshold EVT: fit a generalized Pareto distribution (GPD) to
severity excesses over a high threshold, by method of moments (Hosking &
Wallis). Rules from the spec:

* GPD is used only when ``n_u >= 20`` excesses exist; otherwise the tail
  claim is "parametric extrapolation, low confidence".
* **Falsification**: if the fitted shape ``xi`` is unstable under
  threshold sensitivity (sign flip or large relative change across the
  0.85/0.90/0.95 quantile thresholds), the tail claim is downgraded to
  qualitative — the overlay reports ``stable=False`` and no EVT quantiles.

Applied in the scorer on the aggregate MC sample of L|S (spec section 6:
scenario tail from the aggregate sample when enough positive hits).
"""

from __future__ import annotations

import math


def fit_gpd_mom(excesses: list[float]) -> tuple[float, float] | None:
    """Method-of-moments GPD fit. Returns (xi, beta) or None."""
    n = len(excesses)
    if n < 2:
        return None
    mean = sum(excesses) / n
    if mean <= 0:
        return None
    var = sum((x - mean) ** 2 for x in excesses) / (n - 1)
    if var <= 0:
        return (0.0, mean)
    xi = 0.5 * (1.0 - (mean * mean) / var)
    beta = 0.5 * mean * ((mean * mean) / var + 1.0)
    if beta <= 0:
        return None
    return (xi, beta)


def gpd_tail_quantile(p: float, u: float, n_u: int, n: int, xi: float, beta: float) -> float:
    """Quantile at level p (> threshold CDF level) under the GPD tail model:
    Q = u + beta/xi * ( ((1-p) / (n_u/n))^{-xi} - 1 ); xi -> 0 gives the
    exponential limit Q = u - beta * ln((1-p) / (n_u/n))."""
    exceed_prob = n_u / n
    tail = (1.0 - p) / exceed_prob
    if tail <= 0:
        return u
    if abs(xi) < 1e-9:
        return u - beta * math.log(tail)
    return u + (beta / xi) * (tail ** (-xi) - 1.0)


def _quantile(sorted_sample: list[float], q: float) -> float:
    n = len(sorted_sample)
    idx = min(n - 1, max(0, math.ceil(q * n) - 1))
    return sorted_sample[idx]


def evt_overlay(
    sorted_sample: list[float],
    *,
    threshold_quantile: float = 0.9,
    min_excess: int = 20,
    sensitivity: tuple[float, ...] = (0.85, 0.95),
) -> dict | None:
    """GPD overlay on a sorted positive-loss sample.

    Returns None when the sample is too small for any tail claim
    (n_u < min_excess): "parametric extrapolation, low confidence".
    Otherwise the overlay dict, with ``stable=False`` (and no EVT
    quantiles) when the shape parameter flips sign or swings under
    threshold sensitivity.
    """
    n = len(sorted_sample)
    u = _quantile(sorted_sample, threshold_quantile)
    excesses = [x - u for x in sorted_sample if x > u]
    n_u = len(excesses)
    if n_u < min_excess:
        return None
    fit = fit_gpd_mom(excesses)
    if fit is None:
        return None
    xi, beta = fit

    # threshold sensitivity: refit at neighbouring thresholds
    xis = {"main": xi}
    for tq in sensitivity:
        u2 = _quantile(sorted_sample, tq)
        exc2 = [x - u2 for x in sorted_sample if x > u2]
        fit2 = fit_gpd_mom(exc2) if len(exc2) >= min_excess else None
        if fit2 is not None:
            xis[str(tq)] = fit2[0]
    stable = True
    for other_tq, other_xi in xis.items():
        if other_tq == "main":
            continue
        if (xi >= 0) != (other_xi >= 0):
            stable = False
        elif abs(other_xi - xi) > 0.5 * max(abs(xi), 0.1):
            stable = False

    overlay = {
        "used": True,
        "u": u,
        "n_u": n_u,
        "xi": round(xi, 4),
        "beta": round(beta, 4),
        "xi_sensitivity": {k: round(v, 4) for k, v in xis.items()},
        "stable": stable,
    }
    if stable:
        overlay["q95_evt"] = gpd_tail_quantile(0.95, u, n_u, n, xi, beta)
        overlay["q99_evt"] = gpd_tail_quantile(0.99, u, n_u, n, xi, beta)
    else:
        overlay["note"] = (
            "xi unstable under threshold sensitivity; tail claim downgraded "
            "to qualitative (spec section 6 falsification)"
        )
    return overlay
