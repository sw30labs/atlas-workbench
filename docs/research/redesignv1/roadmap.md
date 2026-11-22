# Roadmap

## Principles

1. **Phase 1 = smallest falsifiable slice**, not a platform.
2. Prefer **delete multiverse** over multi-year investment if eval says so.
3. Keep the public project limited to synthetic and public inputs.
4. Each phase ends with a written go / iterate / abandon.

---

## Phase 0 — Design freeze (this session) ✅

**Deliverable:** `docs/research/redesignv1/*` pack (renamed from `docs/redesign/`).  
**Exit:** You argue the design; we revise contested RECs; no large code yet.

**Suggested argument list (1 session with yourself / pod):**
- CBGE vs weekly full pairs as primary
- all-must-fire definition
- abandon criteria thresholds
- buyer: scenario team vs capital team

---

## Phase 1 — Falsifiable core (≈ 2–4 weeks, 1–2 engineers)

**Goal:** Prove or kill the **scoring + ranking** claim on synthetic + public/demo data. Multiverse is optional thin slice, not the star.

### 1.1 Build

| Item | Done when |
|------|-----------|
| RiskAtom + ChannelGraph load from `emerging_risks.json` | Tests green |
| IncidentFeature synthetic generator with **planted channels** (R0) | Planted graph recoverable |
| Deterministic scorer v0: marginal F×S + common-factor joint + MC | ScoreRealization written with full components |
| Pair candidate generator: all valid channel-blocked pairs + independent baseline | ≤ few thousand pairs ok |
| Eval harness Task B + C (see evaluation-plan) | `eval_report.json` |
| Telemetry wrapper on any LLM stub (even if det-only path) | 100% call coverage invariant |
| Egress gateway stub: deny-list unit tests with synthetic local-detail strings | Red-team test passes |
| CLI: `atlas-score pairs` and `atlas-eval` | Documented in README section |

### 1.2 Explicitly out of Phase 1

- Full branch multiverse UI  
- Frontier geo fusion mining  
- EVT sophistication beyond stub  
- Operator-supplied event ingestion (not part of the public project)
- LangGraph complexity beyond a thin optional loop  

### 1.3 Success criteria (go)

1. R0 Task C pass (structure recovery).  
2. R1/R0 Task B: dependence-aware scorer lifts AP ≥ 15% relative vs independence baseline **or** clear documented failure.  
3. Cost report runs (even if $0 local).  
4. You would show `eval_report.json` to a skeptical quant without embarrassment.

### 1.4 If fail

Reposition as qualitative scenario library (current Atlas) or stop AI-scoring claims. **Do not** proceed to multiverse platform.

---

## Phase 2 — Search loop + workshop path (≈ 4–6 weeks)

**Only if Phase 1 go.**

| Item | Done when |
|------|-----------|
| CBGE expander + archive K + promotion gates | Loop runs N cycles |
| Checkpoints + branch fork/merge (structural + conflict) | Reconstruct from checkpoint |
| Thin multiverse console + Mermaid projection | Topology matches store |
| Local LLM coherence judge (optional) | Ablation A4 runnable |
| Task D expert resolution (even n=1–3) | Scores recorded |
| A/B configs M1–M3 on public payloads | Cost/quality table |
| Path A (batch) vs Path B (loop) comparison | Pre-registered pass rule |

### Success

- Loop not worse than batch on Task B, **or** ≥30% cost save at same top-k precision.  
- Expert coherence: promo > random.  
- Measured cost per promoted scenario on the evaluation run.

### Fail

- Keep batch scorer + simple ranked list; delete multiverse code paths.

---

## Phase 3 — Synthetic scoring hardening

| Item | Done when |
|------|-----------|
| Synthetic scoring-cell aggregation | Deterministic artifact and report |
| Aggregate/local-detail boundary exercise | No local-detail demo rows admitted by egress |
| Task A + B on denser planted series | Eval report |
| Model inventory / research model registration as required | Ticket IDs |
| Review pack export for fictional scenarios | Used in one evaluation review |

### Success

- Calibration not catastrophic (Task A coverage band).  
- At least one scenario analysis forum uses an Atlas-generated compound scenario as discussion input.  
- Cost extrapolation for annual run presented with telemetry basis.

### Fail / pause

- Public evidence remains too sparse → keep claims qualitative.
- Calibration fail → qualitative only.

---

## Phase 4 — Hardening (only after Phase 3 value)

- EVT maturity, severity dependence, richer BN if SMEs own DAG  
- Postgres multi-user  
- Continuous scheduled runs  
- Formal validation docs  
- Optional deployment-specific taxonomy and workflow adapters

**Not** a promise. Re-plan after Phase 3.

---

## Resource sketch

| Phase | People | Compute | External $ |
|-------|--------|---------|------------|
| 1 | 1–2 eng | M3 Ultra local | ~0 |
| 2 | 2 eng | Local + small frontier public-only | low hundreds–low thousands |
| 3 | 2–3 eng + reviewer time | Local runtime + approved APIs | deployment-dependent |
| 4 | TBD | TBD | TBD |

---

## Monday morning start (Phase 1 checklist)

1. Create package `contingency_atlas/scoring/` skeleton (det only).  
2. Synthetic event generator with 3 channels, 15 atoms, 40 quarters.
3. Fit marginals; score all channel-blocked pairs; dump components JSON.  
4. Implement independence baseline; compute AP on held-out last 8 quarters.  
5. Write first `eval_report.md` by end of week 1 — even if numbers are ugly.

**Verifiable week-1 success:** a holdout AP number for dependence vs independence exists and is reproducible from a single command.

---

## Decision log (fill as you go)

| Date | Decision | Rationale |
|------|----------|-----------|
| 2026-08-01 | Design pack v1 | This document set |
| 2026-08-01 | **Accepted**; Phase 1 started on branch `redesign/nc/phase1` | Smallest falsifiable slice; boring scoring core with pre-registered kill criteria; zero new runtime deps (pure stdlib); existing six-phase pipeline untouched. Deviations: SQLite/JSON instead of DuckDB (not a dependency yet); R1 Task B deferred (demo evidence too sparse for quarterly signal series — documented in eval report); EVT stub only per §1.2. |
| 2026-08-01 | Phase 1 **GO** (Task B +84% rel. AP lift; Task C 0.77 precision); Phase 2 started on `redesign/nc/phase2` | Both pre-registered gates passed on R0. Phase 2 scope: CBGE loop, gates, archive, checkpoints, branch ops, Mermaid projection, Path A/B kill test. Deferrals: LLM coherence judge (G6 skipped in det mode), web console, Task D panel, M1–M3 A/B (need LLM endpoints / human raters). Latest realization stored on the scenario row instead of a separate table (Phase 2 deviation, documented). |
| 2026-08-01 | Path A/B kill test: **keep_loop** (loop: 52 scored vs batch 105 at equal P@20 0.9, AP 0.746 vs 0.734 — both legs pass) | Multiverse complexity earns its budget on R0. Remaining Phase 2 items delivered: `/multiverse` console (projection-only), coherence judge with egress-gated frontier path (G6 enforced when judge active; stub judges until endpoints wired), M1–M3 A/B harness, Task D blind panel instrument. Task D ratings remain human input. |
| 2026-08-01 | Phase 2 **closed**; Task D instrument validated via **simulated** panel (OMLX raters, artifacts labeled `simulated`) | Simulated ratings exercise the instrument end-to-end; they are not human expert resolution and do not count as the Task D pass (evaluation-plan §3.4). Human panel pack exported and ready for raters. |
| 2026-08-01 | Simulated panel outcome: **FAIL** (DeepSeek-V4-Flash diff −0.15 p=0.51 n=40; Qwen3.6-27B diff +0.01 p=1.0 n=31; pooled diff −0.1) | Read honestly: local LLM raters cannot separate promoted compounds from random pairs on titles+channels alone. Expected to some degree on R0 — all 15 atoms are plausible tech-ops risks, so title-level coherence discrimination is weak by construction. Does NOT affect go/no-go (human Task D still pending); it shows the instrument does not rubber-stamp. |
| 2026-08-01 | Phase 3 **started** on `redesign/nc/phase3` | Built: R1 signal-series machinery (`atlas-eval r1` — runs on demo store with small-sample caveat), Task A calibration harness (`atlas-eval calibrate` — strict rule FAILs on R0 by discreteness/noise, caveats surfaced not hidden; Brier view included), review-pack export, research model-inventory entry. |
| 2026-08-01 | Ablations A1–A6 runner done (`redesign/nc/ablations`); **4/4 runnable ablations consistent** | A1 AP drop 0.734→0.398 ✓; A2 cost 58→91 scored at flat P@20, AP down ✓; A6 keep_loop ✓; A4 judge gating ✓ (Task D pending); A3/A5 `not_implemented` (EVT stub, no external registry). Building A2 exposed loop re-scoring churn — fixed (prefilter dedups vs all branch states); loop AP 0.746→0.774. Scorer gained `dependence_mode` (A1); loop gained `allow_all_pairs` (A2). |
| 2026-08-01 | Synthetic scoring-cell demo green | Fictional generated rows → validation + planted-prose stripping → segment-group suppression → two tiers (`aggregate` `scoring_features` egress-allowed; `local_detail` synthetic event rows always denied). Default demo: 65/68 rows accepted, Task B +43% lift PASS, Task C 0.77 PASS, egress boundary verified both directions. |
| 2026-08-01 | External feature registry done (`redesign/nc/ext-features`); spec §10.2 discipline fully mechanized | Pre-registration gate, Fisher + rate ratio on train only, block-bootstrap CIs, one-shot holdout direction, BH-FDR q=0.10, min-support filter, adversarial null pass (time-shift + optional two-model LLM). Demo on planted series: strong planted accepted (RR≈12–14, q<0.1), noise rejected, weak planted caught by support filter. Survivors are `associational_feature` modifier candidates — human/policy accept required before score wiring (A5 ablation stays `not_implemented` until then). |
| 2026-08-01 | Hardening done (`redesign/nc/hardening`): EB shrinkage, R0′ count regime + NB family, real EVT layer; **5/5 runnable ablations consistent** | (a) EB rate shrinkage (`eb_strength=4`) closes most of the calibration deviance gap; (b) R0′ Poisson-count regime + per-atom Poisson/NB moments fit — NLL beats naive on both regimes; coverage over-cover at low rates documented as regime property; (c) GPD overlay on MC loss sample with threshold-sensitivity falsification (ξ stable → EVT q95/q99 alongside empirical; unstable → downgraded, per spec §6). A3 now measures q99 stability across MC seeds: 0.034 with EVT vs 0.045 without. Only A5 pending (human/policy modifier gate). |
| 2026-08-01 | Live console done (`redesign/nc/live-console`): SSE stream + scenario detail page | `/api/multiverse/events` streams rollup/promo/topology/egress_deny/finished from store + `run_state.json` (atomic per-cycle writes by `atlas-loop run`); console page renders live with ticker and run-state badge; `/scenario?id=…` detail page (members, decomposition, gates, provenance, ancestry). Stream closes connection at `finished` (HTTP/1.0 EOF semantics, tested). Viz-spec §2.2/§3/§4.4 now satisfied; remaining viz deferral: pause/resume controls. |
| 2026-08-01 | Redesign engine wired into the original pipeline as the monthly `compound_join` node (phase6 → compound_join → end) | 28-day due gate against a run ledger; joins emerging risks into channel-blocked pairs, scores from the public signal series, writes top-20 to a dedicated `compound_scenarios` table + JSON artifact (never maps onto legacy urgency). Self-gates on sufficiency: records `insufficient_data` instead of fabricating scores. First live run on repo data: 66 blocked pairs scored, top 20 written. Also fixed a real Beta-posterior boundary bug (cells firing every period) found by the new tests. |
| 2026-08-01 | Automatic compound **graduation** added as Phase 8 (`compound_graduate` node, LLM-driven per owner decision) | Proven compounds become full library scenarios without a human promotion step — but eligibility is deterministic (join top-N + co-occurrence in the post-scoring holdout window; in-sample-only never graduates), the LLM only drafts the document (Phase 3 judge ≥ 0.75 gates), and score fields come from stored realizations, never from the LLM. Provenance: `ATLAS-CMP-*` ids + origin/state_hash. Ledgered, idempotent. Dashboard Phase 8 card (REQUIRES OMLX). |
| 2026-08-01 | **No Validation GUI page** (owner decision) | Algorithm validation is a one-off gate per algorithm version (re-run on algorithm change, not an operational view). The record lives in eval artifacts (`generated/**/eval_report.*`, `ablations.*`, `calibration_report.*`), this decision log, and `docs/research/redesignv1/model-inventory.yaml`. A permanent page would be maintenance for something read ~twice a year. Remaining GUI item: Compound Scenarios page (per-item operational view). |
| 2026-08-01 | **Compounds page shipped** (`/index` → Compounds nav) | Phase 7 output is now visible per item: member-risk titles, p̂ + interval, q95, uncertainty class, dependence support, holdout-survival flag (recomputed from the live signal series), graduation status with judge score, plus the join-run ledger. Closes the loop: join → see compounds → watch them graduate. |
| 2026-08-01 | **Costs / telemetry page shipped** (`/costs`) | The measured economics view per telemetry-spec §3–4: spend + shadow, cost per promoted, tokens + cache hit rate, spend by phase/node/model, per-scenario attributed cost (direct + even share of cycle-level), recent events, pricing versions. Reads the telemetry log next to the active multiverse store. $0 on the deterministic path today; becomes the economics evidence page the moment LLM roles are active. |
| 2026-08-01 | **Multiverse console dropped** (owner decision: "doesn't give me anything useful") | `/multiverse` page, summary/topology APIs, and the SSE stream removed. Kept: `/scenario?id=…` detail page + API, `/costs`, and the engine's multiverse store + `atlas-loop` CLI (search state is not GUI). Mermaid module stays for CLI artifacts. |
| 2026-08-01 | **Multiverse loop removed from mainline** (owner decision: batch is the production path) | Deleted: `loop.py`, `multiverse.py`, `mermaid.py`, `loop_eval.py`, `ab_configs.py`, `judgment.py`, `workshop.py`, `expert_panel.py`, `panel_sim.py`, `runstate.py`, `atlas-loop` CLI, `/scenario` page + API, and ~45 loop-era tests. The loop passed its pre-registered kill test on R0 (52 vs 105 scored, equal P@20, AP 0.746 vs 0.734) but at live-data scale batch does everything; the result is preserved in git history and as historical rows A2/A4/A6 in the ablation matrix. Kept: deterministic batch scoring (Phase 7), Phase 8 graduation, `/costs` (now reads the new compound-join telemetry log at `generated/telemetry/compound-join.jsonl`, promoted count from the compound register), external features, calibration, EVT, egress. Task D panel/judge/workshop can be re-instrumented against `compound_scenarios` if ever needed — git history has the code. |
| 2026-08-01 | **Artifact ingestion added to Phase 1** (upload → convert → rubric → review-first prefill) | PDF/Office/text/transcripts convert to markdown (MarkItDown optional `[ingest]` extra; honest degradation), bounded chunk-map extraction with deterministic merge, rubric completeness check (missed-sources enumeration, attribution fidelity with quoted spans, body fidelity, load-bearing facts, deterministic URL gate — user rejected confidence scores), lane-labeled sources (literal / LLM-resolved amber / unresolved). `/api/ingest/convert` writes nothing; the Phase 1 modal prefills editable fields and only RUN PHASE mutates. Live-verified on the local OMLX box. Image captioning via multimodal model and audio via whisper remain future lanes (honest "unsupported" today). |
| 2026-08-01 | **Artifact ingestion extended to Phase 2** (`mode` parameter end-to-end) | Same upload in the Risk Monitor modal: `mode=intelligence` switches the extraction focus to emerging-risk signals (what is new/changing and why it matters) while lanes, merge, rubric, and the review-first gate stay identical. Verified live: risk-focused notes (concentration, single point of failure, joint-failure), BIS reference correctly unresolved. |
| 2026-08-01 | **Full LLM telemetry via choke point** | Audit found 13 uninstrumented LLM call sites (legacy phases 1–3/6, judge, Phase 8, ingest extraction/rubric). Fixed at the spec's choke point: `OMLXClient` takes an optional telemetry sink + node label and emits one event per attempt (tokens from endpoint `usage`, latency, status); pipeline, dashboard runner, and ingest endpoint construct clients with the production sink (`generated/telemetry/llm.jsonl`). Costs page aggregates all of `generated/telemetry/*.jsonl`. Live-verified: ingest extraction + rubric logged with real token counts. Un-instrumented by default for tests/offline. |
| 2026-08-03 | **Correction** (whitepaper review D1): the 2026-08-01 hardening row above overstates the calibration result — "NLL beats naive on both regimes" is false | The stored R0 artifact records `nll_beats_naive: false` (76.34 vs 72.47; Brier also loses 0.18634 vs 0.18542; coverage 0.9667 over-covers). NLL beats naive on **R0′ only** — and there the strict rule in fact passes in full (NLL 105.38 vs 135.81, Brier 0.1708 vs 0.1766, coverage 0.9167 in band; artifact now stored at `generated/redesign/phase3/calibration-r0prime/`, generated 2026-08-03 — none existed before). Log rows are append-only, so this row corrects rather than edits. See `docs/research/whitepaper/WHITEPAPER_v2.md` §6.3 and `WHITEPAPER_REVIEW.md` D1/D10. |
| 2026-08-03 | **Hybrid scoring program landed** (waves 1–6 of HYBRID_SCORING_PROPOSAL.md, owner's locked sequencing; `algorithm_version` 0.1.0 → **0.2.0**) | **A7 auditability:** stable `pair_id`, `data_hash` + `judgment_version` in realization identity, append-only `score_realizations` history under the `compound_scenarios` current view, horizon hashed in quarters, regime-aware loss suppression (activation regimes never render dollars), k≥3 graduation attestation computed (no more bypass/hardcoded `True`), `origin=compound_graduation` excluded from legacy urgency without parsing IDs. **A3+A6:** two-level Fréchet-clipped intervals with median + `p_hat>p_hi` flag (16/105 rows flagged with the primary — now visible); bootstrap CIs; multi-seed R0 (AP lift median +0.34 over 10 seeds — the single-seed +84% was one draw); **Task B-cal** (closed-form truth, supported-rare stratum); **Task L**; B5 composite baseline. **A1 mixture challenger:** spike-and-slab reproduces the proposal's table exactly, fixes zero-support coverage 0.00→0.47 and interval flags 16→0, but regresses supported-rare 0.62→0.46 — **not promoted**, primary stays `shrunk_empirical`. **P8/P6:** merge proposals + decision functions (ADR-014), refutation memos, LLM-null default-on. **P1/P2 display+shadow:** channel proposals + analogue dossiers (citation-clamped), shadow join never touches production tables, egress schemas. **P7:** hash-chained judgment ledger with frozen forecasts + resolution job. Not in program (unchanged): P2 conversion, P3, P5, L2/L3, ADR-001 amendment; `judgment_channel: none`. Suite 492+1 → GUI triggers (Crash Test / Parallel Worlds / What Breaks First? / Reality Check / Hidden Connections / History Rhymes, dedup scan, shadow strip, paper trail) → **511 passed + 1 skipped**. |
