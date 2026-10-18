# Evaluation Analysis — the falsification record

This document records what has been measured, what it means, and what
remains unproven. Principle (from the redesign): *without this, the
system is demoware; a failed result is a success if it stops a bad
investment.* All numbers are reproducible from single commands (see
`operations.md`).

## 1. Regimes

| Regime | Data | Status |
|--------|------|--------|
| R0 | Synthetic generator, planted channels (15 atoms, 3 channels, 40 quarters) | **primary test regime, passing** |
| R0′ | Same with Poisson counts per firing (richer cells) | used for Task A/NB |
| R1 | Public/demo store (13–16 risks, ~26–33 evidence events) | runs, **small-sample flagged** |
| synthetic-cell-demo | Fictional grouped events with planted channels | boundary and aggregation exercise |

> **Note (2026-08-02):** the R1 signal series is now approved-only on
> both sides of the join (`scoring/r1.py` filters evidence events and risks that
> have not completed human review). Historical R1 counts above predate
> that boundary; current sufficiency reflects the reviewed corpus
> only, which is why a sparse register can report
> `insufficient_data` where earlier runs scored.

## 2. Task B — pair co-occurrence ranking (core product claim)

Claim under test: dependence-aware scoring ranks held-out pair
co-occurrence better than independence and chance.

Protocol: fit on quarters 0–31, score all 105 pairs, labels from
quarters 32–39 (pair positive = both atoms fire in a shared quarter).
Base rate: 39/105 = 0.3714 holdout positives.

| Baseline | AP (95% CI) | AUROC | P@20 |
|----------|-----|-------|------|
| B0 random | 0.3912 [0.279, 0.533] | 0.5245 | 0.35 |
| B1 independence | 0.3979 [0.281, 0.534] | 0.5210 | 0.25 |
| B2 shared-channel † | 0.6971 [0.545, 0.854] | 0.7768 | 0.80 |
| B5 composite † (new, A6) | 0.7160 [0.579, 0.837] | 0.7255 | 0.85 |
| **B3 det batch** | **0.7335** [0.588, 0.846] | **0.7605** | **0.90** |

† reads channel tags — planted truth on R0 (partially oracle, E1);
production tags come from keyword rules. CIs are seeded pair-resampling
bootstrap (B=1000), new in A6.

**AP lift B3 vs B1: +84.3% relative** (pre-registered threshold ≥ 15%) →
**PASS**; bootstrap 95% CI [+47%, +138%] excludes zero. Label-shuffle null
control: no baseline beats base rate on shuffled labels.

**Multi-seed (A6, new):** the single-seed +84% is one draw, not the
typical one. Over 10 pre-registered generative seeds: AP lift
min/q10/median/q90/max = −0.07 / −0.07 / **+0.34** / +0.92 / +1.92;
Task B pass 8/10, Task C pass 8/10, GO 7/10
(`generated/redesign/phase1/multiseed/`). The thesis survives as a
distribution, but the honest headline is the median, not the max.

On R1 the same machinery runs but the holdout has ~11 positives; the
report is flagged directional-only (recommendation ITERATE, not a
verdict).

## 2a. Task B-cal — compound *levels* vs generative truth (new, A6)

Task B ranks; B-cal checks whether p̂ lands near the closed-form
generative truth, stratified by E[N_S|independence] × rarity × support —
including the supported-rare stratum built to catch naive prior fixes.
Pre-registered coverage band [0.80, 0.95], mirroring Task A.

| stratum | n | primary coverage | mixture coverage |
|---------|---|------------------|------------------|
| all | 105 | 0.39 | 0.55 |
| zero-support | 51 | **0.00** | **0.47** |
| supported-rare | 26 | 0.62 | 0.46 |
| p̂ > p_hi rows | — | 16 | **0** |

Reading (the instrument doing its job): the zero-support collapse (T1)
is real and the A1 mixture challenger fixes it (0.00 → 0.47 coverage),
and its model-averaged draws remove the α ≪ 1 point-outside-interval
regime (16 → 0 flagged rows). But the mixture is deliberately
conservative at a single co-fire, and on R0's strong-coupling regime the
supported-rare stratum regresses (0.62 → 0.46). **Both modes FAIL the
pre-registered band; the challenger is not promoted; the primary stays
`shrunk_empirical`.** The full κ / π₀ × c grid is in
`generated/redesign/phase3/ablations/ablations.{json,md}` — sensitivity
is small across it.

## 2b. Task L — severities vs generative truth (new, A6, review E7)

L|S had no evaluation task anywhere. Now: per-atom parameter recovery
and holdout log-likelihood against `r0_truth.json`.

- Holdout severity log-likelihood (26 amounts): fitted −350.2 vs pooled
  −351.4 vs truth −342.4 — per-atom shrinkage beats one pooled model
  (+1.2), the oracle gap is −7.8.
- Recovery is graded by cell thickness exactly as designed: pooled-prior
  cells |Δμ| ≈ 1.06 (n=2) vs shrunk cells |Δμ| ≈ 0.63 (n=13).

## 3. Task C — structure recovery

Planted channels hidden; edges recovered from train-window lift-ranked
co-occurrence.

- Precision@30 planted edges: **0.7667** (threshold ≥ 0.7; random base
  rate 30/105 = 0.286) → PASS
- Planted fraction of top-decile U ranking: **0.9091** (threshold ≥ 0.7)
  → PASS

## 4. Task A — atom frequency calibration

Strict rule (coverage ∈ [0.8, 0.95] AND model beats naive):

| Regime | NLL model vs naive | Coverage | Verdict |
|--------|--------------------|----------|---------|
| R0 | **beats** (NB + EB shrinkage) | 0.983 (over-cover) | FAIL on coverage |
| R0′ | **beats** | 0.983 (over-cover) | FAIL on coverage |

Honest reading (reported, not hidden): at R0 rates the discrete 90%
interval is almost always [0,1], so the band is unreachable — a
regime property, not necessarily model failure. The Brier and NLL views
both favor the model. The earlier Poisson-only harness lost to naive;
empirical-Bayes shrinkage plus the NB family choice closed most of that
gap (166 → 98 deviance on R0; naive 93).

## 5. Ablation matrix (evaluation-plan §5)

| Ablation | Result | Status |
|----------|--------|--------|
| A1 remove dependence | AP 0.734 → 0.398 ✓ | runnable, consistent |
| A2 remove channel graph | cost 58→91 scored, P@20 flat, AP down ✓ | **historical** (loop removed) |
| A3 remove EVT | q99 CV 0.034 with vs 0.045 without ✓ | runnable, consistent |
| A4 remove judgment | gating effect recorded; Task B flat by construction ✓ | **historical** (judge removed) |
| A5 remove external features | activates when modifiers are wired | not implemented (policy gate) |
| A6 batch vs loop | keep_loop (52 vs 105, equal P@20) ✓ | **historical** (loop removed; owner then deleted it anyway) |

5 runnable-consistent at peak; currently 2/2 runnable consistent + 3
historical rows preserved.

## 6. External feature screen

Planted synthetic screen: strong planted feature accepted (RR 12–14,
q < 0.1, bootstrap CI excludes 0), noise features rejected (FDR/support
filter), zero false channel attribution. The screen enforces
pre-registration, train-only discovery, BH-FDR q = 0.10, one-shot
holdout direction, min support ≥ 8 quarters, and adversarial null passes.

## 7. Confidentiality red team

Egress gateway: clean structural payloads pass; synthetic local-detail content (emails,
account-like digit runs, narrative keys, overlong prose) denied; audit
hash chain detects tampering. The synthetic scoring-cell demo admits its
aggregate feature rows and denies sampled local-detail event rows.

## 8. What is NOT proven

1. **Generalization beyond synthetic and public-demo inputs.** Every PASS
   above comes from one of those two regimes.
2. **Human preference (Task D).** The blind panel instrument exists; a
   **simulated** LLM panel ran and *failed* (diff ≈ 0, p ≈ 0.5–1.0) —
   informative (local models can't separate promoted from random on
   titles+channels, partly by construction) but explicitly not human
   evidence. Human raters pending.
3. **Calibration on busier cells.** Task A's strict band may only be
   reachable on denser series than the shipped demonstrations.
4. **The loop's economics.** Removed before the real-data comparison;
   preserved in history (ADR-003).
5. **Compound *levels*, even on R0 (new, A6).** Task B-cal shows both
   the primary and the A1 mixture challenger outside the pre-registered
   coverage band (§2a): zero-support pairs were crushed ~9× below truth
   (T1), the challenger fixes that stratum but regresses supported-rare
   on this regime. Ranking passes; calibrated levels are not yet proven
   anywhere.

## 9. Kill criteria status (evaluation-plan §6)

| Criterion | Status |
|-----------|--------|
| Task B fail on R1 and R0 after two iterations | not triggered (R0 pass; R1 small-sample) |
| Task D fail (promotions not more coherent than random) | **open** — simulated panel failed; human panel pending |
| Egress gateway not airtight | not triggered (red team passes) |
| Cost per promoted above ceiling with no quality win | not triggered (det path ≈ $0) |
| Audit: score not reconstructable from components | not triggered (full components stored) |
