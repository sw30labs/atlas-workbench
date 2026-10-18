# Loop Design — Generation, Promotion, Multiverse

## 0. Thesis (and disagreement with the brief)

The brief proposes: Cartesian product → evaluate → **promote winner as the new baseline** → re-product around it (Karpathy-style recursive self-improvement).

**What is right:** iterative search, explicit evaluation, checkpoints, branching when assumptions change.

**What is wrong or dangerous:**

1. **Cartesian product as primitive** optimizes for completeness theater, not information. Most subsets are causally incoherent.
2. **Single-baseline promotion** is premature exploitation. Early noisy “wins” re-center the search and starve alternatives (classic bandit failure).
3. **“Self-improvement” language** overclaims. This is **hypothesis-space search with memory**, not guaranteed improvement of a true risk measure.

**Replacement loop identity:**

> Multi-baseline archive search with graph-guided expansion, budgeted exploration, promotion gates, and git-like branching — evaluated against holdout discipline.

---

## 1. Generation strategy (tractable combinatorial spirit)

### 1.1 Do not enumerate \(2^N\)

For \(N=60\), \(2^{60}-1 \approx 10^{18}\). Even pairs are \(\binom{60}{2}=1770\); triples \(\binom{60}{3}=34220\). Pairs+triples are feasible to *score cheaply*, not necessarily to *judge with frontier models*.

### 1.2 Recommended pipeline: **Channel-Blocked Graph Expansion (CBGE)**

```text
Inputs:
  R     risk atoms with channel tags, taxonomy, maturity
  G_ch  undirected (or directed) channel graph: risks linked if
        share channel OR expert edge OR empirical co-occurrence > τ
  B     compute budget (candidate slots this cycle)
  A     baseline archive (up to K scenarios)
  ε     exploration fraction

Expand:
  1. `seed_set` = risks in archive members ∪ top-m by atom “attention”
     (attention = G-intensity proxy: maturity×momentum×data_gap)
  2. BLOCK: only propose pairs (i,j) if edge in G_ch OR same channel
  3. ORDER CAP: default max order k_max = 3
  4. PROMOTION TO k+1: a (k+1)-tuple may be generated only if a k-subset
     is in archive or passed prefilter this cycle (progressive widening)
  5. BEAM: keep top-W partials by cheap score (deterministic p_hat * novelty)
  6. EXPLORATION: ε·B slots reserved for random valid edges / long-range
     cross-channel “wildcard” pairs (explicitly labeled exploration)
  7. Hard cap |C| ≤ B
```

### 1.3 Effective branching factor

Assume average channel degree \(d \approx 4\), \(k_{\max}=3\), beam \(W=50\), archive seeds \(s=30\):

| Stage | Rough scale |
|-------|-------------|
| Valid pairs from seeds | \(O(s \cdot d) \approx 120\) not \(\binom{N}{2}\) |
| Triples via promotion | \(O(W \cdot d) \approx 200\) |
| Wildcards | \(\varepsilon B\) e.g. 20 |
| **Per-cycle candidates** | **~100–300** before LLM; **~20–40** after cheap prefilter |

Vs naive pairs+triples on 60 risks: ~36k scored deterministically might be OK weekly; CBGE is what you run **continuously** and what you send to expensive judges.

### 1.4 Alternatives considered

| Strategy | Verdict |
|----------|---------|
| Order-limited full pairs/triples | Acceptable **batch** weekly; use as baseline comparator in eval |
| Plausibility LLM prefilter alone | Useful as layer 2 filter; **not** sole generator (bias, cost) |
| Pure dependency DAG ancestral sampling | Excellent if DAG trusted; combine with CBGE |
| Bandit over frontier | Use for **budget allocation across branches**, not raw combo gen |
| Progressive widening | **Adopt** (in CBGE step 4) |

**REC:** CBGE + weekly full pair sweep on cheap det scorer as safety net (catch missed edges → propose graph edits).

---

## 2. Evaluation of candidates (before promotion)

For each candidate \(S\), compute (see `algorithm-spec.md`):

| Score | Role in loop |
|-------|----------------|
| \(\hat p(S)\), interval | Likelihood |
| Loss quantiles \| S | Impact |
| `novelty` | Distance from archive (Jaccard on risk sets + score-space) |
| `evidence_support` | Data richness + allowed external features |
| `coherence` | Local LLM/human rubric 0–1 |
| `cost_est` | Telemetry prior for this path |
| `uncertainty_class` | Gate strictness |

**Cheap prefilter (no frontier):** drop if:

- no path in \(G_{ch}\) and not wildcard slot;
- all atoms `fading` / deprecated;
- duplicate of existing archive member (structural hash match);
- det \(\hat p \cdot \mathbf{1}_{q95 > L_{\min}}\) below floor **and** not exploration slot.

---

## 3. Promotion criteria (“what works” numerically)

Promotion admits \(S\) into the **baseline archive** (not “truth”).

### 3.1 Hard gates (all must pass)

| Gate | Rule **(REC)** |
|------|----------------|
| G1 Structure | \(1 \le k \le k_{\max}\) (or exception ticket for k=4) |
| G2 Channel | Connected in \(G_{ch}\) OR wildcard with `exploration=true` and coherence ≥ 0.7 |
| G3 Audit | Full score vector present; `algorithm_version` set; MC seed stored |
| G4 Uncertainty honesty | If `judgment_dominant`, requires second judgment source OR human attest |
| G5 Novelty | Jaccard to nearest archive member ≤ 0.66 **or** loss profile distance ≥ δ |
| G6 Coherence | Rubric ≥ 0.65 (local judge) |
| G7 Evidence floor | `evidence_support` ≥ θ_e **or** explicitly tagged `speculative_workshop` with lower rights |
| G8 Safety | No local-detail text in public fields; egress log clean |

### 3.2 Soft score for ranking into archive slots

\[
U(S) = w_p \cdot \tilde{p} + w_L \cdot \tilde{q}_{0.95}
      + w_n \cdot \text{novelty} + w_e \cdot \text{evidence}
      - w_u \cdot \mathbf{1}_{\text{judgment\_dominant}}
      - w_d \cdot \text{disagreement}
\]

Normalize tildes to [0,1] within-cycle. Weights versioned (**(REC)** start equal on p, L, novelty, evidence; small penalty on judgment/disagreement).

### 3.3 Archive policy (multi-baseline)

- Capacity \(K\) **(REC)** 20–50.
- Insert if \(U(S)\) beats worst member **or** improves Pareto front on \((\tilde{p}, \tilde{q}_{95}, \text{novelty})\).
- Crowding: prefer diverse risk-set coverage (hypervolume / niche count).

### 3.4 What promotion is *not*

- Not capital approval.
- Not “validated true.”
- Not deletion of losers (losers go to cold store with reason codes).

---

## 4. Loop structure (per cycle)

```text
function CYCLE(archive A, graph G, budget B, branch_id):
  checkpoint = SNAPSHOT(A, G, config, rng)
  C = CBGE_EXPAND(A, G, B)
  C = PREFILTER(C)
  for S in C:
      v = SCORE(S)                    # det + optional judgment
      WRITE_NODE(branch_id, S, v, parent=checkpoint)
  promoted = []
  for S in RANK(C):
      if PASSES_GATES(S) and ARCHIVE_ACCEPT(A, S):
          A = INSERT(A, S)
          promoted.append(S)
  ALLOCATE_BUDGET(branches)           # bandit on branch-level reward
  return A, promoted, checkpoint
```

**Outer Ralph-style goal example:**

- Goal: `|promoted| ≥ n_target` AND `eval_harness.last_status != FAIL` AND `spend ≤ budget`.
- Stop: goal met, spend cap, wall-clock, or consecutive cycles with zero promotions **and** exploration exhausted.

---

## 5. Checkpoints

### 5.1 Checkpoint contents (reconstructability)

```text
Checkpoint {
  checkpoint_id, branch_id, parent_checkpoint_id,
  created_at, run_id,
  config_hash,           # algorithm_version, weights, model_config
  graph_hash,            # G_ch snapshot
  archive_ids,           # scenario ids in A
  rng_state,
  assumptions[],         # structured key-value (not prose-only)
  feature_registry_hash, # allowed external features
  code_git_sha,          # engine version
  plane_manifest,        # what data classes were in scope
  telemetry_cursor,      # pointer into telemetry log
  state_hash             # see data-model.md
}
```

**Rule:** any checkpoint can rebuild the *decision* (not necessarily bit-identical LLM prose) given config + seeds + inputs.

### 5.2 When to write

- Start of cycle
- After each promotion
- Before fork
- On merge
- On human assumption edit

---

## 6. Branching, revert, rectify, fork

### 6.1 Operations

| Op | Meaning |
|----|---------|
| **Branch** | New `branch_id` from checkpoint; copy archive snapshot by reference |
| **Revert** | Set branch head to prior checkpoint (soft); mark intervening nodes `superseded` |
| **Rectify** | Edit `assumptions[]` or graph edge or modifier; requires new checkpoint + reason code |
| **Fork** | Revert + rectify + continue under new branch_id |
| **Prune** | Mark branch `abandoned` with reason; retain freeze for audit |
| **Merge** | Join two branches when equivalence holds (below) |

### 6.2 Branch pruning / abandonment

Abandon if any:

- `cycles_without_promotion ≥ P` **(REC)** P=10 **and** exploration budget spent on branch;
- cumulative cost > branch_cap without archive insert;
- dominated: another branch has superseding archive coverage (set of promoted risk-pairs covers this branch’s) **and** better calibration diagnostics;
- human kill switch;
- eval harness FAIL attributable to branch-specific assumption.

Abandoned branches are **not deleted**.

---

## 7. State equivalence and merge (the hard part)

### 7.1 Two different notions (do not collapse them)

| Kind | Definition | Use |
|------|------------|-----|
| **Structural identity** | Same canonical risk-id set, same horizon, same assumption_hash, same algorithm_version major | Dedup / automatic merge of identical work |
| **Soft equivalence** | High similarity of risk sets **and** score vectors **and** dependence family | Candidate for merge review |

### 7.2 Canonical state hash (structural) **(REC)**

```text
state_hash = sha256(
  canonical_json({
    "risk_ids": sorted(risk_ids),
    "horizon": H,
    "assumption_hash": sha256(canonical_assumptions),
    "graph_hash": G_ch hash,
    "feature_registry_hash": ...,
    "algorithm_major": "1.x",
    "compound_def": "all_must_fire",
  })
)
```

**Note:** scores are **not** in the state hash. Scores are derived; two runs with different MC seeds are same state, different realizations → store `realization_id`.

### 7.3 Soft equivalence for merge candidates

Merge *candidate* if:

1. Jaccard\((R_a, R_b) \ge 0.8\), and
2. same `compound_def` and horizon bucket, and
3. score vector distance:

\[
d = \sqrt{
  w_1 ( \log p_a - \log p_b )^2 +
  w_2 ( \log q95_a - \log q95_b )^2 +
  w_3 ( \text{disagreement}_a - \text{disagreement}_b )^2
}
\]

with \(d \le d^*\); **(REC)** calibrate \(d^*\) so that only “obvious duplicates” auto-flag.

### 7.4 Merge policy

| Case | Action |
|------|--------|
| Identical `state_hash` | Auto-merge nodes; keep max evidence; union telemetry |
| Soft equivalent, scores agree within CI overlap | Auto-merge with `merge_method=ci_overlap`; lineage both parents |
| Soft equivalent, scores **disagree** beyond CI | **No auto-merge**. Create `conflict` record; keep both; optional third “dispute” branch with pooled interval widened to cover both |
| Human forces merge | Allowed with attest; store both preimages |

### 7.5 Failure modes

| Failure | Mitigation |
|---------|------------|
| **Premature merge** collapses distinct hypotheses (e.g. same risks, different transmission / BU scope) | Put transmission_id and scope in assumptions; hash them |
| **Never-merge** explodes storage | Soft-dup detector + archive crowding; cold storage after 90 days |
| **Score washing** by merging high and low to average | Forbidden: disagreement → widen interval, don’t mean-merge point estimates |
| **Hash churn** from volatile fields | Exclude timestamps, LLM prose, raw tokens from hash |

### 7.6 Lineage

Every scenario node stores:

```text
parent_ids[], branch_id, root_id, merge_parents[],
assumption_diff_from_parent, promotion_record_id?
```

Final workshop pack must render ancestry to root (see viz).

---

## 8. Exploration vs exploitation

| Mechanism | Purpose |
|-----------|---------|
| ε-wildcard slots | Cross-channel surprises |
| Multi-baseline archive | Avoid single-mode collapse |
| Branch bandit | Spend more cycles on branches that promote + calibrate |
| Weekly full pair sweep | Catch graph misspecification |
| Forced diversity in archive | Crowding distance |

**Failure mode if ignored:** loop “converges” to a small cluster of tech risks that score well on synthetic priors and ignores physical/legal channels.

---

## 9. Termination conditions

| Condition | Type |
|-----------|------|
| Outer goal met (n promotions + eval pass + spend≤B) | Success stop |
| Global spend / token cap | Budget stop |
| Wall-clock | Ops stop |
| Eval FAIL (see evaluation-plan) | Safety stop — freeze promotions |
| Human halt | Always |

There is **no** “proved optimum” stop. Report archive snapshot + diagnostics.

---

## 10. Contested: is the recursive loop worth it?

| View | Argument |
|------|----------|
| Pro | Focuses compute on neighborhoods of plausible compounds; human-interpretable search path |
| Con | Can amplify early bias; more moving parts than “score all pairs weekly + sort” |

**REC:** Implement **both** in Phase 1:

- Path A: batch score all valid pairs (det only)
- Path B: CBGE multiverse loop for 1 week

If Path B does not improve holdout pair-ranking or expert resolution over Path A by a pre-registered margin, **kill the multiverse complexity** and keep batch scoring + simple archive. The loop is a hypothesis about search efficiency, not an identity.

---

## 11. Open questions

1. \(k_{\max}=3\) vs allow curated 4-tuples — **(REC)** 3 default, 4 by human request only.
2. Archive size K — **(REC)** 30 for Phase 1.
3. Auto-merge CI rule aggressiveness — **(REC)** conservative; prefer conflict records early.
