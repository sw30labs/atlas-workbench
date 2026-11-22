# Evaluation Plan — How We Know This Works (or Kill It)

## 0. Stance

Without this plan, Contingency Atlas is demoware. With it, a failed result is still useful: you stop before an unsupported funding request becomes a governance problem.

**Primary claim under test (Phase 1–2):**

> Graph-guided compound scenario search + hybrid F×S scoring ranks **pair co-occurrence** and produces **workshop-usable** scenarios better than (a) random pairs and (b) independent F×S without dependence — at measurable cost.

**Not under test in Phase 1:** capital accuracy, regulatory approval, geopolitical causal discovery.

---

## 1. Data regimes

| Regime | Data | Use |
|--------|------|-----|
| R0 Synthetic | Known generative process (planted channels + noise) | Unit test the scorer recovers structure |
| R1 Public/demo | Current Atlas fixtures + public timelines | Engineering integration |
| Synthetic cell demo | Fictional grouped events | Aggregation and boundary exercise |

**Go/no-go uses R0 + R1.** The synthetic cell demo adds a separate boundary
exercise, not a new evidence regime.

---

## 2. Holdout and temporal cross-validation

### 2.1 Time splits **(REC)**

For monthly/quarterly series of length T:

```text
Train:  [1, t1]
Val:    (t1, t2]     # tune thresholds, graph edge τ, weights
Test:   (t2, T]      # locked once; single look
```

Expanding window CV optional on train/val only. **Test is sacred.**

### 2.2 Nested discipline for external features

```text
For each outer fold:
  On train only: candidate external features
  FDR control on train associations
  Freeze accepted feature set
  Fit dependence + frequency on train
  Evaluate on val/test without refitting features
```

No “peeking” at test geopolitics to invent features.

---

## 3. Tasks and metrics

### 3.1 Task A — Atom frequency calibration

- Predict period counts per cell.
- Metrics: Poisson deviance, PIT histogram, coverage of 90% intervals.
- **Pass:** coverage in [0.8, 0.95] on test; deviance beats naive mean-rate baseline.

### 3.2 Task B — Pair co-occurrence ranking (core product claim)

Define historical pair label: atoms i,j “co-occur” if both have ≥1 loss (or signal activation) in same quarter in mapped cells.

- Model outputs score \(U_{ij}\) or \(\hat p_{ij}\).
- Metrics: AUROC, average precision, top-k precision (k=20), calibration of \(\hat p\) if base rate allows.
- Baselines:
  - **B0** Random
  - **B1** Product of marginal frequencies (independence)
  - **B2** Shared-channel indicator only
  - **B3** Full pair batch det scorer without multiverse loop
  - **B4** CBGE multiverse loop (treatment)

**Pass (pre-registered):** B4 or B3 achieves AP lift ≥ **15% relative** over B1 on test, and B4 is not **worse** than B3 by more than 5% AP **or** B4 achieves ≥30% lower cost for same top-k precision.

If B4 loses to B3 on quality and does not win on cost → **abandon multiverse loop**, keep batch scorer.

### 3.3 Task C — Synthetic recovery (R0)

Plant known channel graph; generate losses; hide graph.

- **Pass:** recovered edges precision@top edges ≥ 0.7; planted high-risk pairs rank in top decile of \(U\).

### 3.4 Task D — Expert resolution (human)

Blind panel (you + 2 engineers or risk SMEs): for 20 promoted vs 20 random compound scenarios, rate coherence and actionability 1–5.

- **Pass:** mean coherence promo > random by ≥ 0.75 points (paired test p<0.05).

### 3.5 Task E — Calibration of judgment

Where LLM priors used, compare to later statistical estimates when data arrives; track Brier for elicited binary calls.

- **Pass:** not required for Phase 1 kill criteria; report only.

### 3.6 Task F — A/B model harness

Same branch / same candidates, configs:

| Config | Generator critique | Prior elicitation | Public enrich |
|--------|--------------------|-------------------|---------------|
| M1 | local only | local | none |
| M2 | local | local | frontier, public only |
| M3 | frontier, public structure only | local | frontier, public only |

Metrics: Task D scores, cost per promo, disagreement, egress deny rate.

**Pass:** pick default config by constrained optimization: maximize expert score s.t. cost ≤ budget and zero local-detail egress.

### 3.7 Task B-cal — Compound level calibration (added 2026-08-03, A6)

Task B ranks; B-cal checks *levels* against the closed-form generative truth on R0 (`scoring/task_bcal.py`). Per pair, truth is exact under the channel-shock generator (same-channel: π·s_i·s_j + (1−π)·q_i·q_j; cross-channel: product of marginal means, s = min(q·mult+add, 0.9)).

- Metrics: 90% interval coverage, mean signed bias, RMSE — stratified by E[N_S|independence] bins [0, 0.25), [0.25, 1), [1, ∞) × rare (fitted q_ind < 0.01) × support (N_S ≥ 1), **including the supported-rare stratum** (the one naive prior fixes regress on).
- **Pass (pre-registered):** overall coverage in [0.80, 0.95] (mirrors Task A's band). Strata are reported for diagnosis; promotion of any challenger estimator additionally requires the supported-rare stratum not to regress.
- Also the promotion instrument for the A1 mixture challenger: challenger-vs-primary comparison runs with the sensitivity grid in `atlas-eval ablations`.

### 3.8 Task L — Loss-side evaluation (added 2026-08-03, A6/review E7)

L|S had no task anywhere. On R0 the per-atom severity parameters are known (`scoring/task_loss.py`).

- Metrics: per-atom |μ_fit − μ_true|, |σ_fit − σ_true| by shrinkage source (cell_data / shrunk_to_pool / pooled_prior); holdout severity log-likelihood under fitted vs pooled vs truth (oracle ceiling).
- **Pass:** report-only for now; the fitted-beats-pooled margin is the quantity the future proxy-prior kill rule compares against.

---

## 4. Multiple comparisons control

- All feature discoveries: Benjamini–Hochberg FDR \(q=0.10\) **(REC)** on train.
- Report number of tests, number rejected.
- Scenario promotions are **not** each a hypothesis test; the **ranking quality** is the test (Task B).
- Forbid publishing individual geo–loss “findings” that did not pass FDR + holdout.

---

## 5. Ablations

| Ablation | Remove | Expectation if design right |
|----------|--------|----------------------------|
| A1 | Dependence (force independence) | Task B AP drops |
| A2 | Channel graph (allow all pairs) | Cost up; precision@k down or flat |
| A3 | EVT layer | Tail quantiles unstable; mean metrics flat |
| A4 | LLM judgment | Coherence (Task D) drops; Task B flat/up |
| A5 | External features | Task B small drop if any |
| A6 | Multiverse (batch only) | See Task B pass rule |

Pre-register ablation order; run on R0/R1 first.

---

## 6. Falsification / abandon criteria

**Stop the multiverse product thesis** if any:

1. Task B fail on R1 and R0 after two algorithm iterations.
2. Task D fail (promotions not more coherent than random).
3. Egress gateway cannot be made airtight in adversarial test (red team injects local-detail content into outbound).
4. Cost per promoted scenario exceeds pre-agreed ceiling **and** quality fails to beat B1.
5. Model validation SME review concludes score decomposition is not reconstructable from stored components (audit fail).

**Stop the hybrid scoring thesis** (more severe) if Task A continues to fail
across the planted regimes after data-quality fixes — then Atlas remains a
**qualitative workshop aid** only; do not claim probabilistic scenario
analysis.

**Do not abandon** solely because LLM narratives are mediocre — narratives are secondary.

---

## 7. Adversarial and null tests

1. **Explain-the-null:** for every accepted external feature, require a stored null explanation; second reviewer votes.
2. **Label shuffle:** shuffle pair labels; model must not achieve AP ≫ chance (pipeline leak detector).
3. **Time shift:** features from future of train must not be available (asof join tests).
4. **Prompt injection / data exfil:** try to get scorer to send incident text to frontier; must deny.

---

## 8. Reporting template

Each eval run produces `eval_report.json` + short MD:

- data regime, splits, git sha, algorithm_version  
- metrics tables vs baselines  
- ablations  
- FDR summary  
- cost telemetry  
- go / iterate / abandon recommendation  

---

## 9. Phase mapping

| Phase | Must pass |
|-------|-----------|
| 1 | R0 Task C; R1 Task B vs B0/B1; telemetry invariant; egress red team on synthetic local-detail content |
| 2 | Synthetic-cell Task A + B; Task D; A/B model pick |
| 3 | Stability over two quarters; documentation for model inventory |

---

## 10. Open questions

1. Pair co-occurrence definition for the public series — **(REC)** approved
   signal activation on R1; planted event activation in synthetic regimes.
2. Expert panel size — **(REC)** ≥3 raters when available; 1 is weak but ok for Phase 1 engineering.
