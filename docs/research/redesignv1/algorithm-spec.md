# Algorithm Spec — Hybrid Scenario Scoring

## 0. Design principle

> **Novel where novelty buys coverage and auditability of search; boring where numbers must survive model validation.**

| Area | Stance |
|------|--------|
| Combinatorial *search* / multiverse | Novel orchestration OK |
| Frequency × severity | Standard LDA-style decomposition |
| Dependence | Start with rank correlations + vine/copula *where data supports*; BN only with expert DAG |
| Tails | Peak-over-threshold EVT (GP) when severity sample allows; else heavy-tailed parametric with wide priors |
| LLM | Elicited priors + critique; **never** sole source of frequency/severity |
| “Ψ-CHRONOS / mind-blowing joint field” | Research toy only; **not** production score |

A local search experiment demonstrated a useful heuristic, but it is **not**
the scoring algorithm and is outside the public package boundary.

---

## 1. What is being scored

A **compound scenario** \(S\) is:

- a set of emerging-risk atoms \(R_S = \{r_1,\ldots,r_k\}\), \(k \ge 1\);
- optional external condition set \(E_S\) (geo/macro flags, time horizon \(H\));
- a transmission narrative (non-scoring, for humans);
- a **score vector** \(\mathbf{v}(S)\) with uncertainty.

**Output is a distribution**, not a point:

- Loss \(L(S)\) over a defined horizon (e.g. 1-year operational loss conditional on scenario narrative “materializing” under stated assumptions).
- Report: quantiles \(q_{0.5}, q_{0.9}, q_{0.95}, q_{0.99}\), mean, and **probability of materialization** \(p(S)\) as a *research* quantity with calibration tracking — **not** a capital parameter until validated.

Clarify language for validation:

| Term | Meaning here |
|------|----------------|
| \(p(S)\) | Probability that the *scenario event set* occurs within horizon H under model assumptions |
| \(L \mid S\) | Loss severity if it occurs (or unconditional loss via \(p \times\) severity mixture — pick one and stick to it; **(REC)** report both occurrence probability and loss\|occurrence) |
| Urgency | Derived prioritization for workshop queue, not a probability |

---

## 2. Layer stack

```text
[1] Atom frequency models     f_i
[2] Atom severity models      G_i(loss)
[3] Dependence structure      C or DAG
[4] Joint occurrence + loss   Monte Carlo
[5] Tail overlay              EVT on severity excesses (when eligible)
[6] Judgment priors           LLM/expert, protocolized, separate channel
[7] Pooling                   combine det posterior + judgment with disagreement
[8] Diagnostics               calibration, sensitivity, audit package
```

---

## 3. Layer 1–2: Frequency × severity per atom

### 3.1 Frequency

For atom \(r_i\) mapped to a scoring-cell/segment group \(c(i)\):

**If** registered public or planted counts exist for window \(T\) years:

\[
\hat\lambda_i = \frac{N_{c(i)}}{T}, \quad
f_i(H) = 1 - e^{-\hat\lambda_i H}
\]

with Poisson or negative-binomial process; uncertainty via bootstrap or Gamma-Poisson conjugate.

**Else if** only registered public proxy rates exist: use them as a **prior** and inflate variance (explicit `public_proxy` flag).

**Else:** mark `frequency_source = elicited`; no fake precision.

### 3.2 Severity

Fit to **positive loss amounts** in cell \(c(i)\):

1. Prefer empirical distribution + kernel/lognormal/GB2 as candidates; select by holdout log-likelihood.
2. If \(n < n_{\min}\) (**(REC)** \(n_{\min}=30\) for parametric claim; lower → mixture with industry prior).
3. Always store: sample size, fit family, parameters, KS/AD p-values, `severity_source`.

### 3.3 Contested: LDA purity vs scenario narrative

Scenarios are not pure actuarial cells. **(REC):**

- Use cell \(c(i)\) as **anchor** base rates.
- Scenario-specific **modifiers** \(\delta_i\) only when pre-registered (control environment factor, known concentration) and bounded (e.g. \(\delta \in [0.5, 2.0]\)).
- LLM may *propose* modifiers; **cannot apply** them without human or policy accept.

---

## 4. Layer 3: Dependence (the actual product)

### 4.1 What not to do

- Independent product \(\prod f_i\) for joint occurrence of compound scenarios (systematically wrong in the tail).
- LLM: “these feel 70% correlated.”

### 4.2 Data-supported dependence **(REC ladder)**

| Data richness | Method |
|---------------|--------|
| Co-occurrence counts in rolling windows | Empirical copula / rank correlation (Kendall \(\tau\), Spearman \(\rho\)) between atom indicator series |
| Enough severity pairs | Gaussian or t-copula on transformed margins; **t-copula** preferred for tail dependence when df identifiable |
| Expert structure + light data | Bayesian network with expert DAG; CPTs smoothed with Dirichlet; parameters partially from data |
| Sparse everything | Conditional independence given **shared channel** node (common-factor model): one latent “channel shock” per causal channel |

### 4.3 Causal channel common-factor model (default when sparse)

Each risk tagged with channels \(Ch \subseteq \{\text{identity, cloud, payments, model, credit-chain, physical, legal, \ldots}\}\).

\[
Z_c \sim \text{Bern}(\pi_c) \text{ or intensity process}, \quad
\mathbb{P}(r_i \mid Z) = g_i(Z_{ch(i)}, \text{idiosyncratic})
\]

Joint materialization driven by shared \(Z_c\) yields natural compound scenarios **along channels** — aligns generation with scoring (see `loop-design.md`).

### 4.4 Contested: full BN vs copula

| Option | When | Risk |
|--------|------|------|
| Copula on indicators | Time series of co-activations exist | Correlation ≠ causation |
| Expert BN | Clear mechanisms, workshop buy-in | CPT elicitation cost; false confidence |
| **Hybrid **(REC)** | Common-factor for generation+base joint; copula residual dependence if data | Implementation complexity medium |

**REC: hybrid common-factor + residual correlation cap** (\(|\rho| \le \rho_{\max}\) unless data rejects).

---

## 5. Layer 4: Monte Carlo propagation

For scenario \(S\) with atoms \(R_S\):

1. Draw channel shocks and/or copula uniforms \(U^{(m)}\), \(m=1..M\) (**(REC)** \(M=20{,}000\) Phase 1; more for tails).
2. Map to atom occurrence indicators \(I_i^{(m)}\).
3. **Scenario occurrence** definition **(REC, explicit):**

   \[
   I_S^{(m)} = \mathbf{1}\{\text{all atoms in } R_S \text{ occur in horizon}\}
   \]

   Alternative (softer): threshold on weighted sum — document if used; default is **all-must-fire** for “compound” purity, with parallel scores for “any k of n” as sensitivity.

4. If \(I_S^{(m)}=1\), draw severities from margins (with dependence if severity copula exists) and aggregate:

   \[
   L^{(m)} = \sum_i I_i^{(m)} \cdot X_i^{(m)} \cdot \alpha_{\text{overlap}}
   \]

   \(\alpha_{\text{overlap}} \in (0,1]\) prevents pure sum double-counting when losses share the same P&L event (**(REC)** default 0.7 for same-BU same-quarter, 1.0 otherwise; sensitivity report).

5. Output empirical distribution of \(L \mid I_S=1\) and \(\hat p(S) = \frac{1}{M}\sum I_S^{(m)}\).

---

## 6. Layer 5: Tail treatment

- For severity excesses over high threshold \(u\) (e.g. 90th percentile of cell): fit GPD \((\xi, \beta)\).
- Use GPD only if \(n_u \ge 20\) **(REC)**; else report “tail = parametric extrapolation, low confidence.”
- Scenario tail: apply EVT on **aggregate** MC sample of \(L \mid I_S=1\) if enough positive hits; else atom-level EVT then aggregate with caveat.

**Falsification:** if GPD \(\xi\) unstable under threshold sensitivity (mean excess plot), downgrade tail claim to qualitative.

---

## 7. Layer 6: LLM / expert elicitation protocol

### 7.1 When allowed

- No or thin registered history for an atom (`frequency_source = elicited` or `severity_source = elicited`).
- Coherence critique: “what would falsify this scenario?”
- Narrative for workshop pack.

### 7.2 Protocol (mandatory fields)

1. **Elicitation prompt version** + model id + temperature.
2. Ask for **quantiles** (p10, p50, p90) of frequency and severity, not a single number.
3. Ask for **reference class** the expert/model used.
4. Ask for **disconfirming evidence**.
5. Run **second model** (or human) on same protocol; store both.
6. Convert quantiles to a parametric prior (e.g. lognormal) with explicit method.
7. Tag all derived numbers `judgment`; never mix into `observation` without review flag.

### 7.3 What is forbidden

- “Given this incident text, estimate the probability geopolitical factor X caused it” as a *finding*.
- Unprompted causal stories stored as evidence edges.
- Using frontier models on local-detail text.

---

## 8. Layer 7: Aggregation and disagreement

**(REC)** Logarithmic opinion pool for probabilities from independent judgment sources:

\[
\log p_{\text{pool}} \propto \sum_j w_j \log p_j, \quad \sum w_j = 1
\]

Default equal weights; optional weight by historical calibration of that model role.

**Retain:**
- range \([p_{\min}, p_{\max}]\) across sources;
- `disagreement = p_{\max} - p_{\min}` as first-class uncertainty signal;
- do **not** hide disagreement by averaging into false precision.

Deterministic MC output is one “source” with weight based on data richness (`n`, stability).

---

## 9. Auditable score vector \(\mathbf{v}(S)\)

Every scenario persists:

```text
v = {
  p_hat, p_interval,                # occurrence
  loss_quantiles: {0.5, 0.9, 0.95, 0.99},
  loss_mean,
  components: {
    atoms: [{risk_id, lambda, sev_family, sev_params, n, source}],
    dependence: {type, params, data_support},
    modifiers: [{name, value, accepted_by}],
    judgment: [{role, model, quantiles, weight}],
    mc: {M, seed, p_hat_mc, hit_count},
    evt: {used, u, xi, beta, n_u} | null,
  },
  uncertainty_class: "statistical" | "mixed" | "judgment_dominant",
  disagreement,
  novelty,                          # see loop-design
  evidence_support,                 # citation / feature support score
  model_config_hash,
  algorithm_version,
}
```

**Uncertainty class rules:**
- `statistical`: all atoms have \(n \ge n_{\min}\) and dependence from data.
- `judgment_dominant`: any critical atom elicited or dependence expert-only.
- `mixed`: otherwise.

---

## 10. External context fusion (disciplined)

### 10.1 Pushback (required reading)

Time-aligning “what else was happening” with sparse operational losses is a machine for **just-so stories**:

- Post-hoc narrative fitting
- LLM causal fluency
- Multiple comparisons across thousands of (geo feature × loss cell) pairs
- Reporting bias (only some incidents recorded)
- Tiny independent sample for true compounds

### 10.2 Allowed fusion

1. **Pre-register** feature families (e.g. “sanctions regime active,” “rate-hike quarter,” “major cloud outage week”) before mining.
2. Build binary/count external features on a fixed calendar.
3. Test association with **loss counts** using temporal structure:
   - train on \([t_0, t_1]\), validate on \([t_1, t_2]\);
   - block bootstrap for dependence;
   - **Benjamini–Hochberg FDR** across tested pairs at \(q=0.10\) **(REC)**.
4. Minimum support: feature must be “on” in ≥ \(s_{\min}\) periods **(REC)** 8 quarters or equivalent.
5. **Adversarial null pass:** local model must write the best *non-causal* explanation (common seasonality, reporting changes). Human or second model accepts/rejects promotion of the feature to the allowed set.
6. Features that pass become **optional modifiers or channel intensifiers**, not proof of causation. Label: `associational_feature`.

### 10.3 Forbidden fusion

- Free-form “root cause was geopolitics” edges from LLM.
- Using the same period for discovery and for scoring without nested CV.
- Publishing a correlation as a scenario without FDR and holdout.

---

## 11. Calibration and backtesting

### 11.1 What can be calibrated

| Target | How |
|--------|-----|
| Atom frequency | Compare predicted counts vs realized in holdout years (Poisson deviance, PIT) |
| Severity | PIT / reliability of quantile forecasts |
| Scenario \(p(S)\) | **Hard**: compounds are rare. Use **proxy tasks**: (a) pair co-occurrence rates for promoted *pairs*; (b) synthetic injection tests; (c) workshop expert resolution scores |
| Loss quantiles \| S | Only when enough historical analogues exist; else qualitative |

### 11.2 Metrics **(REC)**

- Brier score on discretized occurrence for atoms and for pairs with adequate base rate
- Reliability diagrams (atoms)
- Interval coverage: 90% predictive intervals should cover ~90% of holdout losses
- Sharpness (narrow intervals preferred *given* coverage)
- Calibration-by-source: judgment vs statistical

### 11.3 Falsification criteria (algorithm-level)

Declare the scoring approach **failed** for production pitch if, after Phase 1–2 eval:

1. Atom frequency: reliability curve slope consistent with 0 (uncalibrated) after recalibration attempt; or
2. Pair co-occurrence: promoted high-\(p\) pairs do not appear in holdout more often than matched low-\(p\) pairs (permutation \(p > 0.2\)); or
3. Judgment-only scenarios dominate promotions and fail expert resolution (experts reject >50% as incoherent); or
4. EVT/tail claims flip sign of \(\xi\) under routine sensitivity.

See `evaluation-plan.md` for operationalization.

---

## 12. Novelty vs standard methods — explicit map

| Component | Use standard? | Innovation allowed? |
|-----------|---------------|---------------------|
| Poisson/NB frequency | Yes | No |
| Severity fitting | Yes | No |
| Copula / common-factor | Yes | Mild (channel graph shared with generator) |
| MC | Yes | No |
| EVT POT | Yes | No |
| Opinion pool | Yes | No |
| Graph-guided scenario search | Emerging practice | **Yes** — product differentiator |
| Multiverse checkpoints | Engineering | **Yes** |
| FDR-controlled external features | Yes (stats) | Application to op-risk scenarios |
| LLM scoring of loss | **No** | Forbidden as primary |

---

## 13. Worked micro-example (illustrative numbers)

Scenario \(S\): {API distillation, AI financing stress}, horizon 1y.

1. Map to cells: model-risk / cyber-adjacent; finance / credit-adjacent (project taxonomy).
2. \(\hat\lambda\) from sparse public + elicited prior → wide interval.
3. Shared channel `model-economy` intensifier from pre-registered macro feature “AI-capex concentration high” *if* FDR-pass; else \(\pi_c\) baseline only.
4. MC → \(\hat p(S)=0.02\) with 90% CI \((0.005, 0.06)\), judgment_dominant.
5. Loss\|S: heavy-tailed draws; \(q_{0.95}\) reported with “mixed uncertainty.”
6. Workshop pack: narrative from frontier on allow-listed structure + public articles; scores from local engine.

---

## 14. Versioning

- `algorithm_version`: semver; breaking change if score components change meaning.
- Every score row stores `algorithm_version` + `model_config_hash` + `rng_seed`.

---

## 15. Open questions

1. All-must-fire vs k-of-n compound definition — **(REC)** all-must-fire primary; k-of-n sensitivity.
2. Severity dependence between atoms — **(REC)** independent severities Phase 1; add if data.
3. Continuous-time vs annual bucket — **(REC)** annual/quarterly discrete for Phase 1.
