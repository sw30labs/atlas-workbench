# Architecture — Atlas Workbench Redesign

## 1. Purpose and non-goals

**Purpose:** Run continuous, auditable **scenario analysis** for operational risk: generate compound risk hypotheses, score them with hybrid frequency–severity and dependence models, fuse external context carefully, and retain full lineage across an explicit data boundary.

**Non-goals (explicit):**
- Not a capital model, AMA/SMA replacement, or ICAAP calculator in Phase 1–2.
- Not a real-time cyber SOC or threat intel platform.
- Not “predict next quarter’s losses.”
- Not a substitute for the human scenario workshop; it multiplies candidates those workshops can challenge.

---

## 2. Current system vs target

| Current (repo) | Target |
|----------------|--------|
| Six-phase public-signal foresight pipeline | Scenario-analysis engine with multiverse search |
| Scenarios from LLM + qualitative urgency | Hybrid F×S + dependence + MC + EVT; LLM for judgment only |
| SQLite + JSON risks; provenance gate | Dual-plane stores; state machine over checkpoints/branches |
| Linear LangGraph: phase1→…→phase6 | Search graph: generate → evaluate → promote/fork/merge |
| Fictional scoring events | Synthetic scoring-cell exercise plus public evidence timelines |
| DriftLab optional governance | Mandatory telemetry + model-config A/B harness |

**Reuse:** taxonomy ideas, provenance/review discipline, local OMLX client patterns, dashboard shell concepts, Ralph-loop/judge patterns. **Do not** reuse qualitative-only scoring as the production score path.

---

## 3. Logical architecture

```text
┌──────────────────────────────────────────────────────────────────────────┐
│                         CONTROL PLANE (orchestration)                      │
│  Search Controller · Branch Manager · Promotion Gate · Merge/Prune · A/B   │
│  LangGraph (or equivalent) · Checkpoint Store · Telemetry Aggregator       │
└───────────────┬───────────────────────────────┬────────────────────────────┘
                │                               │
    ┌───────────▼───────────┐       ┌───────────▼───────────┐
    │  LOCAL COMPUTE        │       │  PUBLIC COMPUTE       │
    │                       │       │  External APIs allowed│
    │                         │       │                         │
    │  Fictional event rows │       │  Emerging-risk catalog│
    │  Aggregate builder    │       │  Public evidence TL   │
    │  Local model calls    │       │  Public framework data│
    │  Deterministic F×S/MC │       │  Frontier assistance  │
    │  Calibration / eval   │       │  Retrieval over public│
    └───────────┬─────────────┘       └───────────┬─────────────┘
                │                                 │
                │     ┌───────────────────────────┘
                │     │  ONLY: allow-listed aggregates,
                │     │  scoring codes, public text,
                │     │  and abstract structures
                ▼     ▼
         ┌────────────────────┐
         │  CONFIDENTIALITY    │
         │  ENFORCEMENT POINT  │
         │  (egress gateway)   │
         └────────────────────┘
                │
                ▼
         ┌────────────────────┐
         │  PRODUCT SURFACE    │
         │  Scenario library   │
         │  Console + gitGraph │
         │  Pitch telemetry    │
         └────────────────────┘
```

### 3.1 Components

| Component | Responsibility | Plane |
|-----------|----------------|-------|
| **Risk Inventory Service** | CRUD emerging risks, taxonomy tags, causal channels, maturity | Public or local |
| **Synthetic Feature Builder** | Build aggregate counts, severity distributions, and co-occurrence matrices from generated events | Local |
| **External Context Service** | Geopolitical/macro/public loss timelines; retrieval indices | Public |
| **Candidate Generator** | Graph-guided expansion (not full Cartesian); order caps; progressive widening | Control + local |
| **Scorer (deterministic core)** | F×S, dependence, MC, EVT; pure code; versioned | Local |
| **Judgment Adapter** | LLM elicitation of priors, coherence critiques, narrative drafts under protocol | Split by data class |
| **Promotion Gate** | Numeric + policy checks; writes promotion records | Control |
| **Multiverse Store** | Checkpoints, branches, merges, lineage | Control (metadata) + plane-tagged payloads |
| **Telemetry** | Tokens, cost, latency per node/branch | Control |
| **Eval Harness** | Holdout, temporal CV, A/B model configs | Synthetic and public inputs |
| **Viz Projector** | Mermaid gitGraph + console from store projections | Control |

---

## 4. Data flow (one search cycle)

```text
1. Load risk inventory R + channel graph G + base-rate tables B
2. Select expansion frontier F from multiverse (baselines + exploration slots)
3. GENERATE candidates C = Expand(F, G, budget)     # local, cheap
4. PREFILTER C with deterministic filters + optional local LLM cheap reject
5. For each c ∈ C_filtered:
     a. SCORE_DET: F×S + dependence + MC → loss distribution + components
     b. SCORE_JDG: optional LLM prior/coherence (plane-routed)
     c. POOL: log-odds / opinion pool; retain disagreement
     d. WRITE checkpoint node + telemetry
6. RANK; apply PROMOTION GATE to top-k
7. PROMOTE → new baseline leaves; FORK on assumption edits; MERGE if equivalent
8. PRUNE abandoned branches; emit console + gitGraph projection
9. Every N cycles / on schedule: EVAL harness on holdout window
```

---

## 5. Confidentiality split (binding)

### 5.1 Classification

| Class | Examples | Allowed compute |
|-------|----------|-----------------|
| **`public`** | Public evidence, public macro series, project taxonomies | Any model / API |
| **`aggregate`** | Synthetic scoring-cell counts, severity quantiles, segment codes | Local by default; structural demo features may cross the tested allow-list |
| **`local_detail`** | Synthetic event rows and planted red-team prose | Local only; **never** to frontier API |

### 5.2 Enforcement point (not a convention)

**Egress Gateway** is the only path from local processing to external networks.

Responsibilities:
1. Schema validation of outbound payloads against an **allow-list schema** (scoring codes, numeric features, scenario structural IDs, public URLs).
2. Deny if payload contains local-detail fields, email-like tokens, account numbers, or narrative fields.
3. Redaction scan (regex + optional local NER) on any residual text.
4. Cryptographic audit log of every egress request (hash of body, decision, caller node).
5. Network policy: local-detail processing has no direct route to the public internet; only the gateway does.

**Capability lost at the boundary:**
- Frontier models cannot read local-detail demo rows or planted prose.
- No arbitrary event-file adapter is exposed by the public CLI.

**How to minimize loss:**
- Invest in **feature quality**: good severity curves, co-occurrence, and planted-truth diagnostics.
- Use local models for any task needing aggregate or local-detail content.
- Frontier models get **public analogues** and approved abstract scenario structures for narrative and challenge, not for inventing base rates.

### 5.3 Dual-runtime deployment

| Environment | Local compute | Public compute |
|-------------|-----------------|--------------|
| Offline development | Local runtime and generated artifacts | Disabled |
| Public-context evaluation | Aggregate and local-detail processing stays local | Approved public-only API path through the gateway |

---

## 6. Agent / graph topology

### 6.1 Recommendation: **Search graph over linear pipeline**

The six-phase pipeline is a good **data hygiene** story. The product loop is a **search** problem. Implement as LangGraph with persistent checkpointer.

```text
                    ┌─────────────┐
                    │  scheduler  │
                    └──────┬──────┘
                           ▼
                    ┌─────────────┐
           ┌───────│  expand     │◄──────────────┐
           │       └──────┬──────┘               │
           │              ▼                      │
           │       ┌─────────────┐               │
           │       │  prefilter  │               │
           │       └──────┬──────┘               │
           │              ▼                      │
           │       ┌─────────────┐               │
           │       │  score_det  │  (local)      │
           │       └──────┬──────┘               │
           │              ▼                      │
           │       ┌─────────────┐               │
           │       │  score_jdg  │  (routed)     │
           │       └──────┬──────┘               │
           │              ▼                      │
           │       ┌─────────────┐               │
           │       │  promote?   │──no──► archive│
           │       └──────┬──────┘               │
           │            yes                      │
           │              ▼                      │
           │       ┌─────────────┐               │
           │       │  checkpoint │               │
           │       └──────┬──────┘               │
           │              ▼                      │
           │       ┌─────────────┐    diverge    │
           └───────│  branch_ops │───────────────┤
                   │ fork/merge  │               │
                   │ prune       │───────────────┘
                   └─────────────┘
```

Optional **Ralph outer loop** (your `dcode-tenacity` pattern): goal = “N promoted scenarios with calibration report under budget B”; inner graph = one expand–score–promote cycle. Outer loop stops on goal, budget, or eval failure.

### 6.2 Model roles (defaults; A/B will override)

| Role | Default | Why |
|------|---------|-----|
| Deterministic scorer | Code, not LLM | Auditability |
| Cheap reject / expand assist | Local MLX (DeepSeek-class) | Volume; aggregate/local-detail allowed |
| Coherence / red-team critique | Local or mid-tier | Adversarial “explain the null” |
| Public geo enrichment | Frontier with web/tools **only on public payloads** | Freshness |
| Narrative draft for workshop | Frontier on allow-listed structure + public context | Quality prose |
| Judge / rubric | Separate model from generator | Reduce self-preference |

See `evaluation-plan.md` for A/B harness.

---

## 7. Technology choices

| Concern | Choice **(REC)** | Alternatives | Why |
|---------|------------------|-------------|-----|
| Orchestration | LangGraph + SQLite/Postgres checkpointer | Temporal, custom | You already ship LangGraph; durable state fits multiverse |
| Local demo store | JSONL + SQLite/DuckDB for generated artifacts | Pure SQLite | Analytical queries over synthetic features |
| Public store | SQLite/JSON as today + vector index for retrieval | — | Continuity |
| Deterministic compute | NumPy/SciPy or pure Python + optional PyMC later | Full probabilistic programming day 1 | Phase 1 simplicity |
| Local inference | MLX / OMLX OpenAI-compatible | llama.cpp | Existing client |
| Frontier | Approved APIs only for `public` payloads that pass the gateway | — | Enforced boundary |
| Viz | Server-sent events + Mermaid render; thin web console | Full React rewrite | Spec first, ship thin |
| Compression / context | **Structured slots + retrieval** (not LLMLingua at API) | Local compress sidecar later | Fidelity + simplicity |

### 7.1 Context management **(REC)**

**Primary:** hierarchical **structured context slots** + retrieval, not stuffing.

| Slot | Content | Eviction |
|------|---------|----------|
| `risk_atoms` | Active combination members | Never within a score call |
| `base_rates` | Only relevant cells of B | By scoring-cell/segment match |
| `dep_subgraph` | Local Markov blanket | Hop radius 1–2 |
| `ext_window` | Retrieved timeline ±W days around scenario horizon | Top-k by relevance |
| `lineage_summary` | Rolling summary of branch decisions | Summarize when >T tokens; store hash of preimage |
| `rubric` | Scoring rubric version | Immutable per run |

**Fidelity cost:** rolling summaries can drop minority-dissenting assumptions. Mitigation: store full assumption set in checkpoint; summary is display-only; scoring always reloads structured fields, never the prose summary.

**Not recommended as primary:** API-side LLMLingua. Optional local compression sidecar only for bulk public evidence before frontier use.

---

## 8. Contested decisions

### 8.1 Cartesian product vs graph expansion

| Option | Pros | Cons |
|--------|------|------|
| A. Full/order-limited Cartesian | Simple, “complete” | Most combos meaningless; cost explodes; false discovery hell |
| B. Channel-blocked pairs/triples only | Tractable, explainable | Misses true cross-channel cascades |
| C. **Graph-guided + progressive widening + promotion **(REC)** | Matches dependence story; budget-aware | Needs graph curation |

**REC: C.** See `loop-design.md`.

### 8.2 Where does “source of truth” for loss distribution live?

| Option | Pros | Cons |
|--------|------|------|
| A. LLM point estimates | Fast | Unvalidatable |
| B. **Deterministic engine + LLM priors only when sparse **(REC)** | Audit path | More engineering |
| C. Pure actuarial, no LLM | Clean validation | Weak on novel risks |

**REC: B.**

### 8.3 Single vs multi-baseline archive

| Option | Pros | Cons |
|--------|------|------|
| A. Single promoted baseline (brief) | Simple loop story | Premature convergence |
| B. **Pareto archive of baselines **(REC)** | Explores multiple modes | Merge complexity |

**REC: B** with fixed archive size K (e.g. 20) and crowding distance.

---

## 9. Relation to regulatory scenario analysis vocabulary

Map product language to what the risk function already funds:

| Atlas concept | Op-risk vocabulary |
|---------------|-------------------|
| Emerging risk inventory | BEICF inputs / risk taxonomy overlay |
| Compound scenario | Scenario analysis unit of work |
| Synthetic scoring features | Reproducible boundary and ranking exercise |
| Public evidence timeline | Time-indexed external context |
| Score components | Frequency, severity, dependence assumptions |
| Promotion | Scenario accepted into workshop library (not capital) |
| Telemetry | Run cost / model inventory inputs |

---

## 10. Minimal LangGraph skeleton (spec-level only)

```python
# Illustrative only — do not treat as production code.
class SearchState(TypedDict, total=False):
    run_id: str
    branch_id: str
    checkpoint_id: str
    frontier: list[str]          # scenario_ids
    candidates: list[dict]
    scores: list[dict]
    promotions: list[str]
    telemetry_batch: list[dict]
    config_hash: str
    plane_route: str             # "public" | "aggregate" | "local_detail"

# Nodes: expand → prefilter → score_det → score_jdg → promote → branch_ops
# Checkpointer: durable per branch_id
# Middleware: egress_gateway on any node that might call frontier
```

Full loop semantics: `loop-design.md`. Schemas: `data-model.md`.

---

## 11. Open questions

1. Postgres vs SQLite for multiverse — **(REC)** SQLite Phase 1; Postgres when multi-user.
2. Whether aggregate payloads ever need external processing — **(REC)** local first; require a new allow-list review for each schema.
