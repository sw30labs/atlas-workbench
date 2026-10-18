# Telemetry Spec — Tokens, Cost, Derived Metrics

## 0. Purpose

1. Know **cost per validated (promoted) scenario** from measurement, not guesswork.
2. Drive model routing (local vs frontier) with evidence.
3. Support the funding ask: one cycle → extrapolate.
4. Feed live console and branch bandit rewards.

---

## 1. Event model

Every LLM call, deterministic score job, egress decision, and cycle boundary emits a `TelemetryEvent`.

### 1.1 Required fields

| Field | Type | Description |
|-------|------|-------------|
| `event_id` | string | UUIDv7 preferred |
| `ts` | datetime UTC | |
| `run_id` | string | outer campaign |
| `cycle_id` | string | one expand–score–promote |
| `branch_id` | string | |
| `checkpoint_id` | string? | |
| `node` | string | graph node name: `expand`, `prefilter`, `score_det`, `score_jdg`, `promote`, `merge`, `eval`, … |
| `phase` | enum | `generation` \| `evaluation` \| `merge` \| `control` \| `eval_harness` |
| `scenario_id` | string? | |
| `realization_id` | string? | |
| `provider` | string | `mlx`, `anthropic`, `openai`, `xai`, `none`, … |
| `model_id` | string | full id |
| `model_version` | string? | if known |
| `plane` | enum | `public` / `aggregate` / `local_detail` routing actual |
| `input_tokens` | int | 0 if N/A |
| `output_tokens` | int | |
| `cache_read_tokens` | int | 0 if N/A |
| `cache_write_tokens` | int | |
| `latency_ms` | int | |
| `retries` | int | |
| `status` | enum | ok/error/timeout/denied_egress |
| `error_class` | string? | |
| `unit_price` | json | snapshot of $/MTok rates used |
| `cost_usd` | float | computed |
| `parent_event_id` | string? | for retries |
| `config_hash` | string | |
| `tags` | json | free-form small |

### 1.2 Cost computation

```text
cost_usd =
  input_tokens/1e6 * price_in
  + output_tokens/1e6 * price_out
  + cache_read_tokens/1e6 * price_cache_read
  + cache_write_tokens/1e6 * price_cache_write
  + fixed_per_call (if any)
```

**Local MLX:** still record tokens + latency; `cost_usd` may use **shadow price** (electricity + amortization) config for true TCO, default **(REC)** `cost_usd=0` with `cost_shadow_usd` populated so frontier vs local comparison stays honest.

Price table versioned: `pricing_version` in `unit_price` blob.

---

## 2. Instrumentation points

| Location | What to emit |
|----------|----------------|
| LLM client wrapper | tokens, model, latency, retries — **single choke point** |
| Egress gateway | deny/allow events (zero tokens ok) |
| `score_det` | duration, M draws, CPU ms; tokens=0 |
| Cycle start/end | summary rollup event |
| Promotion | mark linked events with `promoted=true` tag retroactively or join table |
| Eval harness | separate `run_id` prefix `eval-` |

**Implementation rule:** no LLM call outside the instrumented client.

---

## 3. Derived metrics

### 3.1 Real-time (console)

| Metric | Definition |
|--------|------------|
| `spend_run` | Σ cost_usd for run_id |
| `spend_branch` | Σ cost_usd for branch_id |
| `tokens_run` | Σ in+out |
| `cache_hit_rate` | cache_read / (cache_read + input non-cache) — define per provider docs |
| `active_branches` | count status=active |
| `candidates_scored` | count score events |
| `promotions` | count PromotionRecords |
| `frontier_fraction` | frontier cost / total cost |

### 3.2 Decision metrics (pitch + routing)

| Metric | Definition |
|--------|------------|
| **Cost per promoted scenario** | `spend_run / max(n_promoted, 1)` — also report distribution per promotion attribution |
| **Cost per promoted (attributed)** | For each promotion, Σ costs on nodes that touched `scenario_id` ancestry this run / n |
| **Cost to archive steady state** | Spend until archive size hits K or no new promo in P cycles |
| **Marginal cost last N cycles** | Σ cost on last N cycles / N |
| **Cost by phase** | group by `phase` |
| **Local vs frontier split** | by provider |
| **Cost per 1k candidates** | generation+eval / candidates |
| **Eval overhead fraction** | eval_harness spend / total |

### 3.3 Quality-adjusted (later)

- Cost per Brier-point improvement (Phase 2+)
- Cost per expert-accepted scenario (human resolution)

---

## 4. Attribution model

Ambiguity: shared expand cost across many candidates.

**(REC) two reports:**

1. **Average:** total spend / promotions (simple, pitch-friendly).
2. **Causal attribution:**  
   - direct: events with `scenario_id=S`  
   - shared: cycle-level costs divided equally among candidates scored that cycle  
   - ancestry: include parent checkpoint scoring if fork

Store both in rollup table nightly.

---

## 5. Storage

| Store | Format | Retention |
|-------|--------|-----------|
| Hot | SQLite `telemetry.db` or JSONL append | 90 days hot |
| Cold | Parquet partitions by date | 2+ years for audit |
| Rollups | daily/branch/run aggregates | indefinite |

Schema index: `(run_id)`, `(branch_id, ts)`, `(scenario_id)`, `(node, ts)`.

---

## 6. Reporting surface

1. **Live console** — SSE stream of rollups every 2s (see visualization-spec).
2. **Run report JSON** — written at run end:

```json
{
  "run_id": "...",
  "spend_usd": 0.0,
  "spend_shadow_usd": 0.0,
  "n_promoted": 0,
  "cost_per_promoted_usd": null,
  "by_phase": {},
  "by_model": {},
  "cache_hit_rate": null,
  "marginal_last_5_cycles": null,
  "egress_denies": 0
}
```

3. **Pitch appendix** — one-pager table from last full cycle.

---

## 7. Pricing config

```yaml
# config/pricing.yaml (example)
version: 2026-08-01
models:
  anthropic/claude-sonnet:
    input_per_mtok: 3.0
    output_per_mtok: 15.0
    cache_read_per_mtok: 0.30
    cache_write_per_mtok: 3.75
  local/deepseek-mlx:
    input_per_mtok: 0.0
    output_per_mtok: 0.0
    shadow_input_per_mtok: 0.05
    shadow_output_per_mtok: 0.05
```

Never hardcode prices in application logic.

---

## 8. Privacy

Telemetry must not log prompts or responses containing local-detail content.
Prompt hashes are optional and default off; record token counts and metadata only.

---

## 9. Success criteria for telemetry system

- 100% of LLM calls have an event (tested by counter invariant).
- Run report reproduces sum of events within $0.001.
- Cost per promoted available after the first evaluation cycle with n_promoted≥1.

---

## 10. Open questions

1. Shadow price methodology for electricity — **(REC)** flat configurable $/hr GPU-equivalent for Phase 1.
2. Whether to meter deterministic MC as “cost” — **(REC)** latency only until cloud CPUs matter.
