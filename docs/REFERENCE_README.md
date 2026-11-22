# Atlas Workbench

> **Reference copy.** This is the original project manual, preserved for its
> method, safety, and governance detail. Relative links are written for the
> repository root (for example `docs/reference/...`), not for this directory.
> For the friendly quick start, see [../README.md](../README.md).

**An operational-risk discovery and preparedness workbench from SW3.0 Labs**

> Map tomorrow’s failure modes before they become today’s losses.

Atlas Workbench turns independently researched public signals into structured
operational-risk scenarios. It helps researchers explore how weak signals,
control failures, dependencies, and external shocks could combine into
material banking disruptions. A separately governed organization workspace
connects those hypotheses to business services, shared dependencies, controls,
owned actions and exercise evidence.

A scenario is a testable hypothesis, not a prediction. The system is designed
to support research, challenge, and preparedness—not to manufacture certainty.

An optional radar extension classifies broad external drivers, tracks signal
maturity, makes the banking transmission path explicit, and revisits scenarios
against strengthening and weakening signposts. A safe scouting command can
propose public-source leads, but it remains outside the governed pipeline and
never promotes a lead into evidence.

## Project boundaries

This is an independent personal research project:

- Atlas Workbench is not affiliated with, sponsored, supplied, approved, or
  endorsed by the author’s employer or any other organization. No employer
  code, data, internal reports, client information, systems, methods, or other
  confidential or proprietary material is included.
- The project contains no ORX member, subscription, loss-event, news, scenario,
  taxonomy, or other proprietary data. [ORX](https://orx.org/) is linked only
  as an external industry resource. No affiliation or endorsement is implied.
- Nassim Nicholas Taleb’s books *The Black Swan* and *Antifragile* are
  acknowledged as intellectual influences on thinking about uncertainty,
  fragility, and resilience. The software does not reproduce or simulate
  Taleb’s persona, voice, likeness, writings, or views.
- Banking is the principal domain so the project can use a bounded, coherent
  public taxonomy and evaluation surface.
- Outputs are hypothetical research artifacts. They are not financial, legal,
  regulatory, investment, security, or risk-management advice.

Read [DISCLAIMER.md](DISCLAIMER.md), [DATA_PROVENANCE.md](DATA_PROVENANCE.md),
and [DATA_LICENSE.md](DATA_LICENSE.md) before using or contributing data. The
controlled [product roadmap](docs/research/ROADMAP.md) records current delivery status and
acceptance gates.

## What it does

Atlas Workbench runs an eight-phase research workflow:

| Phase | Purpose | Typical output |
|---|---|---|
| 1. Ingest | Normalize user-supplied public-source research | Cited event records |
| 2. Monitor | Identify forward-looking risk signals | Emerging-risk register |
| 3. Generate | Combine signals and event patterns into hypotheses | Candidate scenarios |
| 4. Prioritize | Score likelihood, impact, controllability, and horizon | Ranked scenarios |
| 5. Detect | Test temporal and thematic patterns | Causal findings and challenges |
| 6. Maintain | Refresh or flag aging material; recommend gated merges/retirements | Governed scenario library |
| 7. Join | Score co-active reviewed signals on the public activation series | Compound candidates |
| 8. Graduate | Promote holdout-qualified compounds through the scenario QA gate | Governed compound scenarios |

The generation path uses a bounded judge-and-revise loop before a scenario is
accepted. Deterministic analysis remains available without an LLM. Merge and
retire actions are disabled unless an operator explicitly enables them.

The radar extension adds:

- a project-native external-driver overlay alongside the banking-risk taxonomy;
- signal maturity, momentum, and conservatively derived citation breadth;
- affected capabilities, transmission mechanisms, and second-order effects;
- strengthening, weakening, and option-expiry signposts;
- defensive, resilience, and strategic response options;
- an interactive maturity radar and concise Atlas Briefs in the dashboard; and
- an opt-in, bounded scout that emits pending proposals without writing to the
  evidence stores.

See [Radar, Signposts, and Atlas Briefs](docs/reference/RADAR_AND_BRIEFS.md) for field
semantics and safety boundaries.

## From a signal to an owned decision

The dashboard offers a complete discovery workflow:

1. Add public research from any screen, inspect its sources and review the
   exact record revision with a named decision maker.
2. Explore the scenario portfolio as a map or table, compare two or three
   scenarios, and keep useful filtered views.
3. Activate the separate **Organization** workspace to declare services,
   owners, tolerances, dependencies and failure mechanisms. Map a hypothesis
   to a service with a written rationale.
4. Challenge a control or assumption, assign an action, plan an exercise,
   and record measured or assumed business impacts. Coverage distinguishes
   unassessed, mapped and evidenced tests; it does not declare an empty cell safe.
5. Preview a frozen selection and export a PDF brief, Excel workbook, CSV
   table package, canonical JSON, editable Word brief or PowerPoint deck with
   provenance and checksums. Private context requires explicit scenario
   selection and separate opt-in; baseline committee packs explicitly disclose
   their broader private assessment scope.

CSV/JSON organization imports have column mapping, per-record validation and
an explicit confirmation step. Baselines retain changes for later review.
Evidence diagnostics show source concentration, duplicate citations, missing
fields and stale approvals. Reading mode and reduced motion provide a quieter
working view without removing visual exploration.

See the [workbench guide](docs/reference/enterprise-experience.md),
[organization model](docs/reference/organization-context.md),
[export contract](docs/reference/exports.md),
[committee packs and watchlist digests](docs/reference/assessment-reports.md), and
[pilot validation protocol](docs/reference/pilot-validation.md).

The current maturity is an enterprise evaluation workbench. Ranking results,
calibration and operational readiness are separate verdicts. Customer pilot
outcomes, independent model calibration and production operating controls
must be demonstrated in the deployment where they will be used.

## Optional MITRE ATLAS overlay

Atlas Workbench can add reviewed AI-adversary context from
[MITRE ATLAS™](https://atlas.mitre.org/) to a scenario. The integration is an
optional external-framework overlay; it does not replace the
[Atlas Workbench Risk Taxonomy](docs/reference/TAXONOMY.md), and it never changes
likelihood, impact, urgency, compound generation, graduation, or any other
score.

The workflow is deliberately explicit:

```text
pinned official ATLAS v6 YAML
        -> local catalog import (CLI or Settings)
        -> manual scenario-to-technique proposal
        -> human approval or rejection
        -> approved context in the Atlas Brief
```

No framework data is downloaded at application startup. Select and download a
specific release from the official
[ATLAS data repository](https://github.com/mitre-atlas/atlas-data), then import
that local file. For example, the release manifest current when this workflow
was documented identified content release `2026.07` in format `6.0.0`:

```bash
curl -fL \
  https://raw.githubusercontent.com/mitre-atlas/atlas-data/v2026.07/dist/v6/ATLAS-2026.07.yaml \
  -o /tmp/ATLAS-2026.07.yaml

atlas-framework mitre-import \
  --input /tmp/ATLAS-2026.07.yaml \
  --source-url https://github.com/mitre-atlas/atlas-data/releases/tag/v2026.07 \
  --expected-sha256 0e07bb07fc6423d72cdf24ddc2038a6905bcbc00ba571064153119ee1a5888d4 \
  --activate

atlas-framework status
```

Use `atlas-framework activate VERSION` to make a previously imported release
the active catalog. Imports retain the content version, format version, source
URL, and SHA-256 of the exact input bytes; older releases are not deleted.
`--source-url` is operator-asserted provenance that is recorded, never fetched.
When an official digest is available, use `--expected-sha256` so the importer
verifies the exact bytes before parsing and seals the resulting snapshot.

After import, open a scenario, propose a technique with a written rationale,
and decide the pending mapping in Review. Only approved mappings are displayed.
If meaning-bearing scenario text changes, the prior approval remains visible
as stale audit context, is excluded from current coverage, and must be reviewed
again before becoming current. A monotonic scenario revision prevents an old
approval from silently becoming current when text is restored or a scenario ID
is deleted and recreated. “Applicable” means a reviewer considers the technique
useful for analyzing the hypothesis; it does not mean the technique was
observed or attributed. Related MITRE ATLAS case studies are examples linked
to the technique, not evidence for the Atlas Workbench scenario.

MITRE ATLAS content retains its upstream copyright and Apache-2.0 terms.
MITRE has not affiliated with, sponsored, approved, or endorsed Atlas
Workbench. See [Third-Party Notices](THIRD_PARTY_NOTICES.md),
[Data and Documentation License](DATA_LICENSE.md), and
[ADR-016](docs/decisions/016-mitre-atlas-reviewed-overlay.md).

## Architecture

![Atlas Workbench architecture: public or synthetic evidence enters a provenance-gated eight-phase workflow, which produces governed research products for a review dashboard](docs/reference/architecture-current.svg)

Text alternative:

```text
Optional scout -> pending proposal -> independent researcher review
                                      |
                                      v
Public-source material -> draft-only intake preparation
                  -> operator selects pending evidence + linked risks
                  -> independent researcher review
                                      |
Synthetic fixture -------------------+-> approved corpus
                                          |
                                          v
                          deterministic scenario plan (0..10)
                                          |
                               explicit operator trigger
                                          |
                                          v
                         LangGraph eight-phase research workflow
       |          |             |
       v          v             v
   scenarios   pattern tests   compound tests
       \          |             /
        \         v            /
         governed scenario library
                  |
                  v
       dashboard + review artifacts

Optional local inference: oMLX-compatible endpoint
Optional runtime governance: DriftLab trace and baseline checks
Storage: local SQLite and JSON artifacts
```

## Dashboard tour

Use `scripts/seed_demo.py` to populate the dashboard with a conspicuously
fictional dataset. No real institution or event is depicted.

The sidebar follows the operator's working sequence instead of presenting every
data type and tool as a peer destination:

1. **Research** brings together the **Evidence**, **Signals & Radar**, and
   **Findings** tabs. Evidence contains the provenance-labeled public or
   synthetic event register; Signals & Radar combines the emerging-risk
   register with the maturity-by-external-driver radar; Findings presents the
   persisted pattern tests from Phase 5. These tabs share a research workspace,
   but evidence records, forward-looking signals, and analytical findings keep
   their distinct meanings and review rules. New material begins in
   **Analyze → Add Research**, where one free-form field accepts mixed prose,
   notes, and literal public URLs without positional pairing. Preparation is a
   non-mutating draft; it does not add anything to these registers.
2. **Review** is the human boundary. Its badge counts pending decisions across
   evidence, risks, merge/channel/analogue proposals, and optional
   external-framework mappings. Pending signpost interpretations appear with
   the scenario-maintenance work that produced them.
3. **Analyze** contains **Run cycle**, **Validation**, and **Telemetry**. Run
   cycle starts with the draft-first **Add Research** action and also exposes
   the eight LangGraph phases, due work, background jobs, run history, and
   optional DriftLab governance signals. Validation contains the falsification
   and elicitation tools; Telemetry contains measured token and cost reporting.
4. **Scenarios** contains the searchable, urgency-scored **Library** and the
   **Compound candidates** register. A scenario opens into its Atlas Brief;
   holdout-qualified compound candidates can graduate into the library through
   Phase 8.

**Today** sits above that sequence and answers what needs attention now: pending
review, due analysis, active work, and priority scenarios. **Settings** and
**Method & Help** are utilities rather than workflow steps.

The product loop is therefore **add research → prepare a draft → select items →
review → plan from approved data → explicitly generate/analyze → scenarios**.
The prepared preview groups ready, needs-attention, and already-known items with
their sources, QA history, evidence-to-risk links, and duplicate findings.
Selected valid candidates enter the queue as `pending`; unselected and held
drafts are not persisted. Review admits or rejects them. Only after approval can
they affect analysis or the deterministic `0..10` scenario recommendation, and
the operator must still confirm or override that count and start generation.
Zero is a normal recommendation when the approved corpus has no uncovered gap.

URL badges in the intake preview report literal presence and structurally public
HTTP(S) shape. They do **not** claim that a URL is reachable, canonical, supports
the proposed record, or grants reuse rights. Exact duplicates are held as
already known; semantic similarities are advisory and never auto-merge. See
[ADR-017](docs/decisions/017-unified-draft-first-research-intake.md) for the
graph, bounded QA loop, partial-submission, identity, and approved-only planning
invariants.

In **Review**, pending records and proposals are listed with deterministic
provenance checks
(traffic lights for metadata, synthetic status, and per-ref resolution
against the evidence store) so a reviewer sees *why* a record can or cannot
be approved before deciding. Approval still requires the same two explicit
attestations as `scripts/review_records.py` — the CLI and the card share
one audited code path, serialized by a cross-process register lock. An
optional, clearly-labeled machine pre-analysis (judge model, background
job) can suggest approve/reject with reasons; it is advisory only, never
pre-selects a decision, and every suggestion-versus-decision pair is logged
so drift toward rubber-stamping is measurable.

When a record is blocked for lack of evidence, the card can also run
bounded deep research: a Ralph-style loop that plans queries from the red
flags, searches DuckDuckGo (no API key), fetches a capped number of public
pages (every redirect hop must be a public URL; the model can only select
URLs the search actually returned; PDF sources are parsed locally with
pypdf under their own byte/page caps), and drafts candidate evidence — which
lands in the same queue as ordinary *pending* records, provenance-stamped
`origin: deep_research`. "No credible evidence found" is a first-class
verdict. Approved candidates can then be adopted into the risk's refs with
one click; adoption is refused for anything not fully approved. For pending
channel proposals the loop runs in a gated mode instead: it unlocks only
after the advisory pre-analysis exists (its weaknesses steer the search),
files no candidate evidence, and ends with an advisory draft of the
reviewer's written reason, pre-filled into the decision panel for editing.

## Design principles

- **Draft before persistence.** Free-form intake is classified, linked,
  checked, and previewed without writes. Only operator-selected valid items may
  enter Review, always as pending records.
- **Evidence before synthesis.** Public-source records must be cited at record
  level. Generated narrative never becomes its own source.
- **Provenance is mandatory.** A reviewer must be able to identify the
  publisher, title, URL, publication date, retrieval date, and research status
  for every non-synthetic source record. Ingestion captures and structures
  those fields; rights and factual verification remain pending until a human
  reviewer approves the record.
- **Taxonomy is project-native.** The compact, reviewable
  [Atlas Workbench Risk Taxonomy](docs/reference/TAXONOMY.md) is independently authored and
  versioned; optional MITRE ATLAS mappings remain a separate external overlay.
- **Broad drivers stay separate from banking consequences.** The radar overlay
  records the external change, while the Atlas Workbench Risk Taxonomy and
  transmission path explain why it matters operationally.
- **Hypotheses remain hypotheses.** Scores express model assumptions and
  prioritization choices, not calibrated probabilities or forecasts.
- **Synthetic means explicit.** Demo records must be conspicuously labeled and
  must not describe a real institution as if an event occurred.
- **Local by default.** SQLite, JSON artifacts, and local inference can remain
  on the operator’s machine.
- **Human judgment owns decisions.** The system can organize and challenge
  research; accountable people decide what to accept and what to do.

## Requirements

- Python 3.12 or 3.13
- A virtual environment
- An OpenAI-compatible endpoint for LLM-backed phases; local
  [oMLX](https://github.com/jundot/omlx) (Mac) or a vLLM server on an
  NVIDIA DGX (`provider: dgx`) are the supported options
- No model endpoint is required for the deterministic offline path

## Quick start from a source checkout

For a one-command setup, [`scripts/setup_and_run.sh`](../scripts/setup_and_run.sh) creates the
virtual environment, installs the package with dev and ontology dependencies,
runs the test suite, transactionally upgrades existing local SQLite schemas,
refreshes the ontology guard snapshot
(`ontology/existing.ttl`), and starts the local dashboard. It does **not**
seed demo data — operator-managed runtime stores may be configured at the
repository root; the fictional demo corpus is seeded explicitly with
`python scripts/seed_demo.py` (writes to `generated/demo`):

```bash
./scripts/setup_and_run.sh
```

Or perform the same steps manually:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev,ontology]'
python -m pytest
python scripts/migrate_runtime_stores.py
python ontology/export_existing.py
```

### Optional DriftLab governance integration

DriftLab is not required and is disabled by default. It is not currently
published on PyPI, so install the reviewed 2.1.1 source revision explicitly
before enabling it:

```bash
python -m pip install \
  'driftlab @ git+https://github.com/sw30labs/driftlab-dgx.git@v2.1.1'
export ATLAS_DRIFTLAB_ENABLED=1
```

### Redesign Phase 1 — falsifiable scoring core

The [`docs/research/redesignv1/`](docs/research/redesignv1/README.md) pack repositions Atlas as an
operational-risk **scenario analysis engine**. Phase 1 is the smallest
falsifiable slice (accepted and logged in
[`docs/research/redesignv1/roadmap.md`](docs/research/redesignv1/roadmap.md)): a deterministic
frequency × severity scorer with shrunk-empirical dependence and Monte Carlo
propagation, evaluated against pre-registered baselines on the synthetic R0
regime. No LLM is involved in scoring, and no new dependencies are added.

One-command reproduction of the go/no-go experiment:

```bash
atlas-eval demo            # generate R0 (15 atoms, 3 channels, 40 quarters),
                           # score all pairs, run Task B + Task C on the
                           # held-out last 8 quarters — plus Task B-cal
                           # (levels vs generative truth) and Task L
                           # (severities) in the same report
```

Outputs land in `generated/redesign/phase1/r0/`:

- `eval_report.json` / `eval_report.md` — Task B (pair co-occurrence
  ranking on holdout: B3 dependence-aware scorer vs B0 random, B1
  independence, B2 shared-channel) and Task C (planted-channel structure
  recovery), with pre-registered pass thresholds and a go/iterate/abandon
  recommendation.
- `r0_risks.json`, `r0_events.jsonl`, `r0_truth.json` — the synthetic
  regime (fictional data; planted structure is ground truth).

Individual steps:

```bash
atlas-score r0-generate --out generated/redesign/phase1/r0
atlas-score pairs --risks generated/redesign/phase1/r0/r0_risks.json \
                  --events generated/redesign/phase1/r0/r0_events.jsonl \
                  --out generated/redesign/phase1/pairs
atlas-eval run --risks generated/redesign/phase1/r0/r0_risks.json \
               --events generated/redesign/phase1/r0/r0_events.jsonl \
               --truth generated/redesign/phase1/r0/r0_truth.json \
               --out generated/redesign/phase1/eval --strict
```

`atlas-score pairs` writes one `ScoreRealization` per pair with the full
audit blob from `docs/research/redesignv1/algorithm-spec.md` §9 (atom fits, dependence
parameters, MC seed and hit count, uncertainty class), one telemetry event
per scored pair, and a `run_report.json` cost rollup ($0 on the
deterministic path). The egress gateway stub
(`workbench_atlas/scoring/egress.py`) enforces the sensitive→public
allow-list with a hash-chained audit log; red-team tests live in
`tests/test_scoring_egress.py`.

### Redesign Phase 2 — search loop (REMOVED 2026-08-01)

Phase 2 built a CBGE multiverse search loop (channel-blocked expansion,
promotion gates, archive, checkpoints, branches/merges) on top of the
Phase 1 scorer. On the pre-registered R0 batch-vs-loop test it passed
(52 candidates vs 105 for batch, equal P@20, slightly better AP — decision
`keep_loop`). **The owner removed the loop, multiverse store, Mermaid
projection, coherence judge, A/B harness, expert panel, and workshop
exporter from mainline on 2026-08-01**: at live-data scale batch scoring
does everything production needs, and the machinery wasn't earning its
keep. The pre-registered batch-vs-loop result is preserved in git history
and in the ablation matrix's historical rows (A2/A4/A6). Production pair
scoring is the batch path (Phase 7 compound join, below).
### Redesign Phase 3 — public scoring experiments

The public scoring surface uses synthetic or public inputs only. It exposes no
adapter for operator-supplied event extracts.

- **R1 signal series** — `scoring/r1.py` joins the risk inventory to the
  public evidence store through exact `evidence_refs` into quarterly
  signal activations; `atlas-eval r1` runs Task B on R1 or reports a
  documented insufficiency/small-sample caveat (current demo store: runs,
  but flagged directional-only).
- **Task A calibration harness** — `atlas-eval calibrate` computes
  Poisson deviance vs naive baseline, PIT histogram, 90% interval
  coverage, and Brier-on-occurrence, with sparse-regime caveats surfaced
  in the report (on R0 the strict rule FAILs by construction — discrete
  intervals over-cover at low rates; the report says so).
- **Model inventory entry** — `docs/research/redesignv1/model-inventory.yaml`
  registers the scorer as research-tier with restrictions and evidence
  links.
- **Ablation runner** — `atlas-eval ablations --risks … --events …`
  executes the runnable pre-registered matrix
  (`evaluation-plan.md` §5) and writes `ablations.json/md`: A1 dependence
  removal (AP drops 0.734 → 0.398 on R0) and A3 EVT-tail stability are
  runnable; A2/A4/A6 are preserved as historical rows from the removed
  loop's last run; A5 stays gated on the modifier policy path.
- **Analyze → Telemetry** — `/costs` redirects to the dashboard telemetry tab,
  which aggregates production telemetry (spend, cost
  per promoted compound, phase/node/model breakdown, per-scenario
  attribution) from the compound-join telemetry log.
- **Full LLM telemetry** — `OMLXClient` is the single instrumented choke
  point (telemetry-spec §2): constructed with a telemetry sink in the
  pipeline, the dashboard runner, and the ingest endpoint, so **every**
  LLM call in every phase (1–3, 6, judge QA, 7 det events, 8 drafting +
  judge, ingest extraction + rubric, external-feature null pass) emits a
  per-attempt event (tokens from endpoint `usage`, latency, status, node
  label) to `generated/telemetry/llm.jsonl`. Analyze → Telemetry aggregates
  every log in `generated/telemetry/`. Un-instrumented by default, so
  tests and offline paths stay quiet.

  A2 exposed a real inefficiency — the loop re-scored known states every
  cycle — fixed in `loop.py` (prefilter now dedups against all branch
  scenarios; loop AP improved 0.746 → 0.774 on R0).
- **Synthetic scoring-cell demo** — `atlas-eval synthetic-cell-demo`
  generates fictional events with project-native scoring-cell and segment
  codes, rejects planted invalid rows, strips planted free text, suppresses
  sparse groups, and exercises Task B/C. It writes
  `scoring_features.jsonl`, `synthetic_scoring_events.jsonl`, and
  `aggregation_report.json` under
  `generated/scoring/synthetic-cell-demo/`. Structural features pass the
  `scoring_features` egress allow-list; event rows have no allow-list schema
  and are denied. No public command reads an arbitrary CSV or JSONL extract.
- **External feature registry (FDR discipline)** — `scoring/ext_features.py`
  implements the full `algorithm-spec.md` §10.2 pipeline: pre-registration
  gate (unregistered families cannot be tested), fixed quarterly calendar,
  Fisher exact + rate ratio on train only, block-bootstrap CIs, one-shot
  holdout direction check, Benjamini–Hochberg FDR at q = 0.10, minimum
  support filter, and an adversarial null pass (deterministic time-shift
  null + optional two-model LLM pass). Survivors are labeled
  `associational_feature` — modifier candidates requiring human/policy
  accept, never auto-applied, never causal evidence. `atlas-eval ext-demo
  --risks … --events … [--with-llm-null]` runs it on a planted synthetic
  series: the strong planted feature is recovered (RR ≈ 12–14, q < 0.1),
  noise features fail FDR, a weak planted feature is caught by the support
  filter.
- **Scoring hardening** — three upgrades from the eval reports' own
  findings: (a) empirical-Bayes shrinkage of atom rates toward the pool
  (`eb_strength`, scorer + calibration); (b) an R0′ count regime
  (generated through `R0Config(count_model="poisson_counts")`) with a
  Poisson/NB family choice in the Task A harness — NLL beats the naive
  baseline on the R0′ count regime; on Bernoulli R0 the pooled naive rate
  still wins (coverage also over-covers at low rates). (c) the EVT Layer 5 is
  real: GPD
  peaks-over-threshold on the aggregate MC loss sample with `n_u ≥ 20`
  and threshold-sensitivity falsification — unstable ξ downgrades the tail
  claim to qualitative, per spec §6. Ablation A3 is part of the runnable
  matrix: EVT stabilizes q99 across MC seeds (CV 0.034 vs 0.045 without).
- **Task B-cal + Task L (levels and severities vs generative truth)** —
  `atlas-eval bcal --risks … --events … --truth …` checks compound
  probability *levels* against the closed-form generative truth,
  stratified by E[N_S|independence] × rare × supported with a
  pre-registered coverage band [0.80, 0.95]; `atlas-eval loss` grades
  severity recovery by cell source and holdout log-likelihood (fitted
  −350.2 vs pooled −351.4 vs truth −342.4). Both dependence modes are
  currently outside the B-cal band — ranking passes, calibrated levels
  are not yet proven anywhere (evaluation-analysis §2a/§2b).
- **Multi-seed + bootstrap CIs** — `atlas-eval multiseed` reruns the R0
  go/no-go over 10 pre-registered generative seeds: AP lift
  min/median/max = −0.07 / +0.34 / +1.92, Task B pass 8/10, GO 7/10 —
  the single-seed +84% was one draw, not the typical one. The eval
  report also carries seeded pair-resampling bootstrap CIs (B=1000) on
  AP/AUROC/P@20 and the AP lift.
- **B5 composite baseline** — #shared channels × N_S with an
  independence tie-break; partially oracle on R0 like B2 (flagged in
  reports).
- **A1 mixture challenger (not promoted)** — spike-and-slab dependence
  mode (`dependence_mode: "mixture"`; the primary stays
  `"shrunk_empirical"`): fixes zero-support B-cal coverage 0.00 → 0.47
  and interval flags 16 → 0, but regresses supported-rare 0.62 → 0.46
  on R0's strong-coupling regime — B-cal documented in
  evaluation-analysis §2a, challenger not promoted.
- **Dashboard eval triggers + proposal queues** — Analyze → Validation cards
  run the falsification machinery on click: CRASH TEST (R0 eval suite),
  MULTIVERSE (multi-seed), WHAT BREAKS FIRST? (ablations +
  sensitivity), REALITY CHECK (judgment ledger: record → resolve →
  calibration report), HIDDEN CONNECTIONS and PRECEDENT (OMLX
  channel audit / analogue elicitation), while DARK MATTER JOIN tests
  hypothesized couplings without relaxing the intake gates. An Eval & governance reports
  panel in that tab reads back the latest EVAL/judgment artifacts
  (crash test, parallel worlds, ablations, judgment report) with
  timestamps, rendering "not run yet" for anything missing. Everything
  the elicitors and
  the duplicate scan propose lands in review queues that never
  self-apply: merge proposals (token-Jaccard candidates; reviewer
  decision under the register lock, ADR-014), channel proposals
  (citation URL-clamped, never applied to the production graph), and
  analogue dossiers (pair-keyed, disanalogies mandatory, append-only
  claims).

### Monthly compound join (original pipeline integration)

The redesign engine extends the original six core phases with a monthly Phase
7 step: `compound_join`, a node after Phase 6
(`workbench_atlas/compound_join.py`). On each pipeline run it checks
whether ≥ 28 days passed since the last successful join, joins the current
emerging risks into channel-blocked compound pairs, scores them with the
deterministic engine from the public signal-activation series, and writes
the top 20 into a `compound_scenarios` table plus a
`compound_scenarios.json` artifact. It is fully deterministic (no LLM),
self-gates on cadence and on data sufficiency — when the public signal
series is too sparse it records `insufficient_data` with the reason and
fabricates nothing. Compound scores live in their own table and never map
onto legacy urgency scores. Every scored realization is also appended to
`score_realizations`, an append-only history table (no-update/no-delete
triggers) carrying a stable `pair_id` (`"pair-" + sha256(sorted risk
ids)[:12]`) that survives graph/slug churn — estimator or config changes
surface as new rows instead of silent overwrites, and the Scenarios →
Compound candidates tab's per-row "paper trail" reads this history. When the
signal series is activation-only (`real_loss_fraction == 0`), dollar losses are
suppressed (`loss_units: "activation"`, shown as n/a) rather than
fabricated. A shadow pass (on by default, inert without approved
proposals) additionally scores the pairs that approved channel proposals
*would* add — into the artifact's `shadow` block only, production tables
untouched, graduation frozen by construction — shown in Scenarios → Compound
candidates as the WHAT-IF WATCH strip. Manual run: `python -m
workbench_atlas.compound_join --force`.

### Compound graduation (Phase 8)

A compound that *proves itself* graduates automatically into the scenario
library — no human promotion step. `compound_graduate`, a second new node
(compound_join → compound_graduate → END), applies deterministic
eligibility first: the compound must be in the join's top-N output **and**
all its atoms (any k ≥ 2) must have co-fired in a single holdout quarter
after the scoring window (in-sample-only compounds do not graduate; the
attestation records the matching quarters, window and k — never a
hardcoded `True`). For each eligible compound the LLM drafts a full
scenario document from the structural context, the Phase 3 QA judge gates
it at ≥ 0.75, and the row is inserted with compound provenance
(`origin=compound_graduation` in the scenario column, plus `compound_id` and
`state_hash` in `additional_information`; `ATLAS-CMP-*` is only the display ID
format). Phase 4 excludes these rows by `origin`, never by ID prefix. Score
fields (p̂ band, q95 band) come from the stored realization — the LLM
writes prose, never numbers; on activation regimes the dollar band is
omitted, and display metadata (signal_maturity/momentum/
evidence_strength) is derived from the constituent risk records or
omitted on disagreement. Graduations are ledgered in
`compound_graduations` (idempotent). Phase 8 requires OMLX; the dashboard
card sits next to COMPOUND JOIN and is disabled when the endpoint is
down, with an optional "devil's advocate memos" checkbox (display-only
refutation memos, P6).

### Unified research intake

**Analyze → Add Research** is the primary dashboard intake. Paste mixed public
source text, researcher notes, and literal URLs into one field; no one-to-one
ordering or advance evidence-versus-risk classification is required. The
draft-only preparation graph:

```text
split + harvest literal URLs
  -> propose evidence and linked risk drafts
  -> deterministic provenance/schema checks
  -> semantic QA -> bounded repair when repairable
  -> exact/near-duplicate preflight
  -> ready / needs attention / already known preview
```

An optional **Singularity Atlas** checkbox pulls from the local Neo4j story
graph (`bolt://127.0.0.1:7689` by default) when that database is reachable.
The driver is an extra (`pip install -e '.[singularity]'`). A down or missing
graph does not fail preparation. Only public-feed stories with an admitted
HTTP(S) URL and an operational-risk overlap (security / compute / agency, or
clear infrastructure-risk terms) are offered. Loop archive editions are
excluded on rights grounds. Story ids and URLs already pulled are recorded in
`singularity_intake_seen.jsonl` so the same story is not processed twice; each
prepare writes a rationale table to `singularity_intake_jobs.jsonl` and the
Analyze job console.

The graph accounts for every input unit and is allowed to return holds or zero
usable records. Models may select only a URL literally present in the input;
public-shape validation is not a reachability, attribution, or rights verdict.
An exact duplicate cannot be queued again, while a semantic similarity remains
an advisory flag for a person. Select any valid subset to send to Review.
Nothing persists during preparation, and submission can create only pending
evidence and linked pending risk hypotheses.

After human approval, scenario planning is recomputed from approved evidence,
approved evidence-backed risks, and active-scenario coverage. It returns an
explainable range and a recommendation from 0 to 10. Pending intake never
changes the count, and accepting a recommendation never happens implicitly: the
operator confirms or overrides it and explicitly starts generation.

The phase-specific artifact converter remains available in the individual
Phase 1 and Phase 2 forms for PDF/Office/text/transcript workflows. It converts
to markdown (Microsoft MarkItDown via the optional `[ingest]` extra), performs
bounded chunk extraction and rubric checks, and produces an editable draft for
the lower-level phase interface. The `/api/ingest/convert` and pipeline input
contracts remain documented for programmatic callers; unified intake is the
preferred dashboard path, not a removal of those APIs. See
[ADR-006](docs/decisions/006-review-first-ingestion.md) and
[ADR-017](docs/decisions/017-unified-draft-first-research-intake.md).

### Compound candidates tab (GUI)

The dashboard's **Scenarios → Compound candidates** tab shows Phase 7 output
per item: member-risk titles, p̂ with interval, q95 loss, uncertainty class,
dependence support, a holdout-survival flag (recomputed from the live
signal series), and graduation status with judge score — plus the
join-run ledger. This is the operational view: join → inspect compounds
→ watch them graduate into the library.

With DriftLab enabled, the pipeline emits a decision trace and compares it
with the packaged OSCAL-style baseline. This comparison is a diagnostic
governance signal, not a certification. Trace files can contain prompts,
model responses, source summaries, and other research context. Treat
`.driftlab/` as sensitive runtime data, review it before sharing, and never
add it to the repository.

Create a dashboard-ready local dataset from the conspicuously fictional
fixtures. The seeder writes to `generated/demo` (it refuses the repository
root, where operator-managed stores may be configured, unless `--force` is
passed); the generated
databases and JSON files are ignored by Git. Point the dashboard at the demo
stores via environment variables:

```bash
python scripts/seed_demo.py
ATLAS_DB=generated/demo/atlas.db \
ATLAS_EVIDENCE_DB=generated/demo/event_evidence.db \
ATLAS_EMERGING_RISKS=generated/demo/emerging_risks.json \
ATLAS_SCENARIO_EXPORT=generated/demo/scenario_export.json \
python -m workbench_atlas.web
# then open http://127.0.0.1:8080
```

Configuration precedence is:

```text
explicit Python arguments -> environment variables -> workbench-atlas.yaml -> defaults
```

Keep secrets in environment variables. Do not commit API keys to
`workbench-atlas.yaml` or any other repository file.

For a local OpenAI-compatible endpoint:

```bash
export OMLX_BASE_URL=http://127.0.0.1:8000/v1
export OMLX_API_KEY=replace-with-a-local-secret
export OMLX_MODEL=your-generation-model
export OMLX_MODEL_JUDGE=your-judge-model
# optional: POST /v1/responses instead of /v1/chat/completions
# export OMLX_USE_RESPONSES_API=1
```

To run against an NVIDIA DGX vLLM server instead, select the `dgx`
provider — the whole pipeline reroutes through the `dgx.*` settings
(`DGX_*` env vars, with standard `VLLM_*` variables as aliases):

```bash
export ATLAS_LLM_PROVIDER=dgx
export VLLM_BASE_URL=http://127.0.0.1:8000/v1   # serve-local-llm.sh up <recipe>
export VLLM_API_KEY=test                        # vLLM ignores it; must be non-empty
export VLLM_MODEL=Qwen/Qwen3.6-27B-FP8          # must match GET /v1/models
```

The example configuration intentionally contains no API key.

### Run the deterministic path

```python
from workbench_atlas.config import Settings
from workbench_atlas.pipeline import run_pipeline

result = run_pipeline(Settings(), skip_llm=True)
print(result.summary())
```

### Scout for public-source leads

The packaged source registry covers public feeds from regulators, central
banks, cyber agencies, standards bodies, infrastructure-status publishers,
science and climate agencies, and policy trackers. Validate it without using
the network:

```bash
python scripts/scout_sources.py validate
```

Collection is disabled unless the operator explicitly opts in for that run:

```bash
python scripts/scout_sources.py collect \
  --enable-network \
  --max-sources 3 \
  --max-items-per-source 10 \
  --output generated/scout-proposals.json
```

The command accepts only reviewed registry hosts and enforces public-address
DNS pinning, redirect validation, killable wall-clock deadlines, identity-only
response encoding, response-size, source-count, and item-count boundaries.
Programmatic callers on spawn-based platforms should use a normal
`if __name__ == "__main__"` entry point; pseudo-file and daemonic-process
callers fail closed when killable isolation is unavailable.
Every result is labeled `pending_human_review`, `not_evidence`, and
`pipeline_eligible: false`. It does not write either database, update the
emerging-risk catalog, invoke a model, or run a pipeline phase. A researcher
must inspect the original source and complete the ordinary provenance workflow
before using a lead.

Use `--source SOURCE_ID` one or more times to limit a run. Registry and
proposal details are documented in
[Radar, Signposts, and Atlas Briefs](docs/reference/RADAR_AND_BRIEFS.md).

### Advanced/programmatic intake

For interactive work, prefer **Analyze → Add Research** so the draft graph can
propose structure, expose QA and duplicate findings, and let you select what
enters Review. The lower-level Python pipeline retains its original fields for
automation and backwards compatibility. Only submit material that you are
authorized to process. Retain the record-level citation separately from the
extracted text, and keep each `evidence_texts` item positionally aligned with
its `source_urls` item.

```python
from workbench_atlas.config import Settings
from workbench_atlas.pipeline import run_pipeline

result = run_pipeline(
    Settings(),
    evidence_texts=[
        "A public regulator release reports a hypothetical control pattern..."
    ],
    source_urls=["https://public.example/regulator-release"],
    source_text=(
        "A researcher-authored summary of cited public signals affecting "
        "identity, third-party concentration, and service continuity."
    ),
    risk_source_urls=["https://public.example/risk-research"],
    num_scenarios=3,
)
print(result.summary())
```

This call intentionally demonstrates the lower-level API rather than the
dashboard workflow. `num_scenarios=3` applies only to the corpus that was
already approved when the run began; the evidence and risk material supplied
in the same call remains pending and cannot influence those three scenarios.

Phase 1 and Phase 2 place newly derived public-source records in `pending`
status. Phase 3 and Phase 5 consume only human-approved evidence and risks, so
review new material before a later generation or analysis run:

```bash
python scripts/review_records.py list
python scripts/review_records.py approve-event EVENT_ID \
  --confirm-public-source --confirm-rights
python scripts/review_records.py approve-risk "Risk issue" \
  --confirm-public-source --confirm-rights
python scripts/review_records.py list --kind signpost
python scripts/review_records.py approve-signpost SCENARIO_ID \
  --note "Interpretation checked against cited evidence"
```

The confirmation flags are reviewer attestations, not automated rights or
factual determinations. Rejected and pending material remains outside
model-generation and pattern-analysis context. Phase 3 has no direct raw-signal
input: new signals must enter through Phase 1 or Phase 2, be reviewed, and be
used in a later run.

Risk approval also requires every reference to resolve to a fully approved
public evidence event. Matching source URLs are canonicalized to project event
IDs; an unreviewed bare URL cannot approve a risk.

Phase 6 signpost interpretations use only approved evidence but remain
`pending` until a reviewer approves or rejects the interpretation. That review
changes only `signpost_review_status`; it never activates, retires, or merges a
scenario. Evaluations run only for active scenarios and are appended to the
immutable `signpost_assessments` history table. Approval revalidates the latest
snapshot against the current approved evidence store before atomically
recording the decision and updating the scenario projection.

### Open the local dashboard

```bash
python -m workbench_atlas.web
# then open http://127.0.0.1:8080
```

The dashboard backend binds only to loopback. The default mode supports a
local operator; the optional [shared deployment contract](docs/reference/shared-deployment.md)
adds signed authenticated identity, roles and explicit export permissions
behind a separately configured identity proxy. It does not install an identity
provider or expose the backend directly to a network. Research → **Signals & Radar** includes the
driver-and-maturity radar, and each enriched scenario in Scenarios →
**Library** can be opened as an Atlas Brief for a short, structured team
discussion. Legacy records remain visible with conservative fallback labels.

Jobs retain their state across restarts. Interrupted execution needs an explicit
retry; queued work can be cancelled. The
[operations guide](docs/reference/operations.md) covers verified backups,
restoration into a new directory and the responsibilities of a deployment owner.

### Verify a release candidate

```bash
python scripts/verify_all.py
python scripts/scout_sources.py validate
python scripts/public_release_gate.py
```

After the first commit, add `--history` to scan every reachable Git blob as
well as the current tree.

Before changing repository visibility, complete
[docs/release/PUBLIC_RELEASE_CHECKLIST.md](docs/release/PUBLIC_RELEASE_CHECKLIST.md).

## Runtime data

Runtime databases, write-ahead logs, generated catalogs, exports, reports, and
model artifacts are intentionally ignored by Git. Create them locally or from
explicitly labeled synthetic fixtures. The bundled fixture corpus is covered by
[`examples/fixture_manifest.json`](examples/fixture_manifest.json).

In a source checkout, relative runtime paths resolve under the repository root.
In a regular package installation, they resolve under the current working
directory. Environment variables or explicit `Settings` arguments can override
every data path.

Do not add a real-world record unless it meets
[DATA_PROVENANCE.md](DATA_PROVENANCE.md). In particular:

1. Prefer a primary public source.
2. Record the source metadata and retrieval date.
3. Write an independent factual summary.
4. Distinguish source facts from researcher inference.
5. Mark synthetic content before it enters the pipeline.
6. Complete human review before release.

The public repository must not contain scraped subscription content, employer
material, client or personal data, credentials, private infrastructure
details, or untraceable generated claims.

## Safety and governance

- Machine-producer write seams (ingest, risk register, scenario generation,
  compound join, channel proposals) validate every batch through the ontology
  guard before persisting; a REJECT blocks the write and the previous state
  stands. See [docs/reference/ONTOLOGY.md](docs/reference/ONTOLOGY.md). Set `ATLAS_GUARD=off`
  to disable.
- Stale flags and eligible refreshes may be applied during maintenance.
- Merge and retire actions require the explicit `ATLAS_AUTO_RETIRE=1` safety
  switch because merging retires the merged-away scenario.
- The dashboard cannot override that switch.
- LLM outputs can be inaccurate, incomplete, biased, or fabricated.
- Scenario scores should be independently validated before any real-world use.
- `PipelineResult.state` contains raw run inputs, local runtime paths, and model
  endpoint configuration. Use `PipelineResult.summary()` for ordinary reporting
  and never publish the full state without a deliberate redaction review.
- Operators remain responsible for source terms, privacy, model licensing,
  regulatory obligations, and appropriate human oversight.

See [SECURITY.md](SECURITY.md) for reporting vulnerabilities or accidental
data exposure.

## Repository layout

```text
.
├── workbench_atlas/    # pipeline, phases, local dashboard, and baselines
├── ontology/             # the ontology guard (domain, shapes, validator, fixtures)
├── docs/                 # architecture and radar-method documentation
├── examples/             # explicitly fictional demonstration fixtures
├── scripts/              # verification and maintenance helpers
├── tests/                # unit and integration tests
├── workbench_atlas/web/about.yaml # dashboard About-page content
├── workbench-atlas.yaml # non-secret local inference configuration
├── docs/reference/ONTOLOGY.md    # the two-gate ontology guard: rules, identity, wiring
├── docs/reference/TAXONOMY.md           # Atlas categories, radar overlay, and impact bands
├── DATA_PROVENANCE.md    # source and record-level provenance requirements
├── DATA_LICENSE.md       # licensing boundaries for data and fixtures
└── pyproject.toml        # package metadata and dependencies
```

## Contributing

Contributions are welcome when they preserve the project’s provenance and
non-affiliation boundaries. Start with [CONTRIBUTING.md](CONTRIBUTING.md).
Data-bearing changes receive additional review.

## Acknowledgments

Atlas Workbench builds on open-source tooling and published thinking from a
broader community. See [ACKNOWLEDGMENTS.md](ACKNOWLEDGMENTS.md) and
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## License

Code is licensed under the [Apache License 2.0](LICENSE). Data and documentation
have separate terms described in [DATA_LICENSE.md](DATA_LICENSE.md).
