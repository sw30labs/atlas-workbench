"""
Generate all figures for the Contingency Atlas scoring white paper (v2).
Every data figure reads the real evaluation artifacts under generated/ —
no hardcoded results. Figure 3 is formula-derived (deterministic), and its
caption in the manuscript says so.

Style follows the tslit-dspy whitepaper conventions; categorical palette
validated with the dataviz six-checks validator (all pass; orange contrast
WARN relieved by direct value labels).
"""

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

OUT = Path(__file__).resolve().parent.parent / "figures"
# docs/research/whitepaper/scripts is four levels under the repo root.
GEN = Path(__file__).resolve().parents[4] / "generated" / "redesign"

# palette (validated): B0 green, B1 blue, B2 orange, B3 red
GREEN = "#27865A"
BLUE = "#2E6DA8"
ORANGE = "#E67E22"
RED = "#C0392B"
DARK_BLUE = "#1A3A5C"
DARK_GRAY = "#2C2C2C"
MID_GRAY = "#555555"
LIGHT_GRAY = "#F2F2F2"

plt.rcParams.update(
    {
        "font.family": "DejaVu Sans",
        "axes.spines.top": False,
        "axes.spines.right": False,
        "figure.dpi": 150,
        "text.color": DARK_GRAY,
        "axes.labelcolor": DARK_GRAY,
        "xtick.color": MID_GRAY,
        "ytick.color": MID_GRAY,
    }
)


def fig1_taskb_baselines():
    """Grouped bars: Task B metrics per baseline, from eval_report.json."""
    er = json.load(open(GEN / "phase1/r0/eval_report.json"))
    b = er["task_b"]["baselines"]
    order = ["B0_random", "B1_independence", "B2_shared_channel", "B3_det_batch"]
    names = ["B0 random", "B1 independence", "B2 shared-channel\n(oracle on R0)", "B3 det. scorer"]
    colors = [GREEN, BLUE, ORANGE, RED]
    metrics = [("average_precision", "Average precision"), ("auroc", "AUROC"), ("precision_at_20", "P@20")]

    fig, ax = plt.subplots(figsize=(10, 4.6))
    fig.patch.set_facecolor("white")
    ax.set_facecolor("white")
    gw, bw = 1.0, 0.19
    for gi, (key, label) in enumerate(metrics):
        for bi, bl in enumerate(order):
            x = gi * gw + (bi - 1.5) * (bw + 0.015)
            v = b[bl][key]
            ax.bar(x, v, width=bw, color=colors[bi], edgecolor="white", linewidth=1.2, zorder=3)
            ax.text(x, v + 0.015, f"{v:.3f}".rstrip("0").rstrip("."), ha="center", va="bottom",
                    fontsize=7.5, color=MID_GRAY)
    ax.set_xticks([gi * gw for gi in range(len(metrics))])
    ax.set_xticklabels([m[1] for m in metrics], fontsize=10)
    ax.set_ylim(0, 1.02)
    ax.set_ylabel("score on holdout ranking", fontsize=9)
    ax.yaxis.grid(True, color=LIGHT_GRAY, lw=0.8, zorder=0)
    ax.set_axisbelow(True)
    handles = [plt.Rectangle((0, 0), 1, 1, color=c) for c in colors]
    ax.legend(handles, names, loc="upper left", frameon=False, fontsize=8.5, ncol=2)
    ax.set_title(
        "Task B — pair co-occurrence ranking on R0 (105 pairs, 39 holdout positives)",
        fontsize=11, color=DARK_BLUE, pad=10,
    )
    fig.tight_layout()
    fig.savefig(OUT / "figure1_taskb_baselines.png", bbox_inches="tight")
    plt.close(fig)


def fig2_pit_calibration():
    """PIT histograms, R0 vs R0': from the two calibration reports."""
    r0 = json.load(open(GEN / "phase3/calibration/calibration_report.json"))
    r0p = json.load(open(GEN / "phase3/calibration-r0prime/calibration_report.json"))

    fig, axes = plt.subplots(1, 2, figsize=(10, 3.9), sharey=True)
    fig.patch.set_facecolor("white")
    panels = [
        (axes[0], r0, "R0 (occurrence regime) — strict FAIL",
         f"coverage {r0['coverage_90']:.3f} (band [0.80, 0.95])\nNLL {r0['nll_model']:.2f} vs naive {r0['nll_naive']:.2f}"),
        (axes[1], r0p, "R0$'$ (count regime) — strict PASS",
         f"coverage {r0p['coverage_90']:.3f} (in band)\nNLL {r0p['nll_model']:.2f} vs naive {r0p['nll_naive']:.2f}"),
    ]
    for ax, rep, title, note in panels:
        ax.set_facecolor("white")
        pit = rep["pit_histogram"]
        xs = [(i + 0.5) / 10 for i in range(10)]
        bars = ax.bar(xs, pit, width=0.092, color=BLUE, edgecolor="white", linewidth=1.0, zorder=3)
        # the paper's §6.3 claim is about the TOP-DECILE mass: 0.233 on R0
        # (pathological, highlighted) vs 0.15 on R0' (labeled, not flagged)
        if pit[-1] > 1.5 * 0.10:
            bars[-1].set_color(ORANGE)
        ax.text(xs[-1], pit[-1] + 0.006, f"{pit[-1]:.3f}", ha="center",
                fontsize=8, color=DARK_GRAY)
        ax.axhline(0.10, color=MID_GRAY, lw=1.2, ls="--", zorder=4)
        ax.text(0.02, 0.104, "nominal 0.10", fontsize=7.5, color=MID_GRAY)
        ax.set_title(title, fontsize=10.5, color=DARK_BLUE)
        ax.text(0.02, 0.235, note, fontsize=8, color=MID_GRAY, va="top")
        ax.set_xlabel("PIT bin", fontsize=9)
        ax.set_ylim(0, 0.26)
        ax.yaxis.grid(True, color=LIGHT_GRAY, lw=0.8, zorder=0)
        ax.set_axisbelow(True)
    axes[0].set_ylabel("bin mass", fontsize=9)
    fig.tight_layout()
    fig.savefig(OUT / "figure2_pit_calibration.png", bbox_inches="tight")
    plt.close(fig)


def fig3_rare_pair_markdown():
    """The T1 mechanism (formula-derived): constant markdown vs. how
    informative a zero actually is."""
    fig, axes = plt.subplots(1, 2, figsize=(10, 3.9))
    fig.patch.set_facecolor("white")

    ax = axes[0]
    ax.set_facecolor("white")
    ts = list(range(0, 81))
    kappa = 4.0
    ax.plot(ts, [kappa / (t + kappa) for t in ts], color=BLUE, lw=2)
    ax.scatter([32], [kappa / 36], color=RED, zorder=5, s=42)
    ax.annotate("defaults: T = 32 →\n$q_S/q_{ind}$ = 0.11 for every\nzero-support pair",
                xy=(32, kappa / 36), xytext=(40, 0.42), fontsize=8.5, color=DARK_GRAY,
                arrowprops=dict(arrowstyle="->", color=MID_GRAY, lw=1))
    ax.set_xlabel("training quarters $T$", fontsize=9)
    ax.set_ylabel(r"$q_S\,/\,q_{ind}$ at $N_S = 0$", fontsize=9)
    ax.set_title("The markdown is constant …", fontsize=10.5, color=DARK_BLUE)
    ax.set_ylim(0, 1.02)
    ax.yaxis.grid(True, color=LIGHT_GRAY, lw=0.8)
    ax.set_axisbelow(True)

    ax = axes[1]
    ax.set_facecolor("white")
    qs = [i / 1000 for i in range(5, 401)]
    ax.plot(qs, [(1 - q * q) ** 32 for q in qs], color=BLUE, lw=2)
    for q, c, txt, xy in [
        (0.05, ORANGE, "rare pair: zero co-fires expected\n(P = 0.92) — markdown is\na prior artifact", (0.10, 0.72)),
        (0.30, GREEN, "common pair: zero co-fires\ninformative (P = 0.05) —\nmarkdown is evidence", (0.16, 0.28)),
    ]:
        p0 = (1 - q * q) ** 32
        ax.scatter([q], [p0], color=c, zorder=5, s=42)
        ax.annotate(txt, xy=(q, p0), xytext=xy, fontsize=8.5, color=DARK_GRAY,
                    arrowprops=dict(arrowstyle="->", color=MID_GRAY, lw=1))
    ax.set_xlabel("per-atom quarterly rate $q_i$ (equal atoms)", fontsize=9)
    ax.set_ylabel(r"$P(N_S = 0 \mid \mathrm{independence})$, $T=32$", fontsize=9)
    ax.set_title("… but what a zero proves is not", fontsize=10.5, color=DARK_BLUE)
    ax.set_ylim(0, 1.05)
    ax.yaxis.grid(True, color=LIGHT_GRAY, lw=0.8)
    ax.set_axisbelow(True)

    fig.tight_layout()
    fig.savefig(OUT / "figure3_rare_pair_markdown.png", bbox_inches="tight")
    plt.close(fig)


def fig4_interval_violations():
    """p-hat vs. its own 90% interval upper end, from the shipped audit blobs."""
    rows = [json.loads(l) for l in open(GEN / "phase1/pairs/score_realizations.jsonl")]
    FLOOR = 1e-13
    ok_x, ok_y, bad_x, bad_y = [], [], [], []
    worst = None
    for r in rows:
        p, hi = r["p_hat"], r["p_interval"][1]
        x, y = max(p, FLOOR), max(hi, FLOOR)
        if p > hi + 1e-15:
            bad_x.append(x)
            bad_y.append(y)
            ratio = p / max(hi, FLOOR)
            if worst is None or ratio > worst[2]:
                worst = (x, y, ratio, r["scenario_id"])
        else:
            ok_x.append(x)
            ok_y.append(y)

    fig, ax = plt.subplots(figsize=(7.6, 5.2))
    fig.patch.set_facecolor("white")
    ax.set_facecolor("white")
    lo, hi_ax = FLOOR / 2, 1.0
    ax.plot([lo, hi_ax], [lo, hi_ax], color=MID_GRAY, lw=1.2, ls="--", zorder=2)
    ax.text(2e-9, 8e-9, "$\\hat{p} = p_{hi}$", fontsize=9, color=MID_GRAY, rotation=38)
    ax.scatter(ok_x, ok_y, s=26, color=BLUE, alpha=0.75, edgecolors="white",
               linewidths=0.6, zorder=3, label=f"inside interval ({len(ok_x)})")
    ax.scatter(bad_x, bad_y, s=40, color=RED, edgecolors="white", linewidths=0.7,
               zorder=4, label=f"$\\hat{{p}}$ above interval ({len(bad_x)})")
    if worst:
        ax.annotate(
            f"{worst[3]}\n$\\hat{{p}}$ = {worst[0]:.1e} vs upper end {worst[1]:.1e}",
            xy=(worst[0], worst[1]), xytext=(3e-8, 3e-12), fontsize=8.5,
            color=DARK_GRAY, arrowprops=dict(arrowstyle="->", color=MID_GRAY, lw=1),
        )
    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_xlim(1e-13, 1.5)
    ax.set_ylim(1e-13, 1.5)
    ax.set_xlabel("reported point estimate $\\hat{p}$ (posterior mean, annualized)", fontsize=9)
    ax.set_ylabel("90% interval upper end $p_{hi}$", fontsize=9)
    ax.set_title(
        "Shipped audit blobs: point vs. its own interval (105 pair realizations)",
        fontsize=11, color=DARK_BLUE, pad=10,
    )
    ax.legend(loc="upper left", frameon=False, fontsize=9)
    ax.grid(True, which="major", color=LIGHT_GRAY, lw=0.8)
    ax.set_axisbelow(True)
    fig.tight_layout()
    fig.savefig(OUT / "figure4_interval_violations.png", bbox_inches="tight")
    plt.close(fig)


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    fig1_taskb_baselines()
    fig2_pit_calibration()
    fig3_rare_pair_markdown()
    fig4_interval_violations()
    print(f"figures written to {OUT}")
