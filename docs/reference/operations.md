# Operations Guide

## 1. Setup

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev,ontology]'
# optional, for PDF/Office artifact conversion:
python -m pip install -e '.[ingest]'
```

Requirements: Python 3.12 or 3.13 (project pins `>=3.12,<3.14`); an
OpenAI-compatible endpoint for LLM phases — local
[OMLX](https://github.com/jundot/omlx) at `http://127.0.0.1:8000/v1` by
default, or an NVIDIA DGX vLLM server with `provider: dgx`. No endpoint
is required for deterministic phases (4–7).

Configuration (precedence: explicit args → env → `contingency-atlas.yaml`
→ defaults): `provider` selects the backend (`omlx` | `dgx`; env
`ATLAS_LLM_PROVIDER`). OMLX keys: `omlx.base_url`, `omlx.model`,
`omlx.model_judge`, `omlx.model_final_judge`, `omlx.timeout`,
`omlx.max_tokens`, `omlx.use_responses_api`. DGX keys mirror them under
`dgx.` (plus `dgx.enable_thinking`); env `DGX_*` with standard
`VLLM_*` aliases. `use_responses_api` (env `OMLX_USE_RESPONSES_API`)
sends OMLX traffic to `POST /v1/responses` instead of
`/v1/chat/completions`; Settings exposes the checkbox only when the
running OMLX build has that route. Restart after changing it.
API keys only via `OMLX_API_KEY` / `DGX_API_KEY` env (never in files).

`setup_and_run.sh` selects the backend automatically. It does not inspect the
machine — both backends default to the same endpoint and differ only in model
id, so reachability proves nothing: an OMLX-configured run on a DGX connects
happily and then fails every call on a model vLLM does not serve. Instead it
asks the endpoint for its model list and picks the provider whose configured
model is actually served:

```bash
python -m contingency_atlas.provider_detect -v   # prints omlx|dgx, explains on stderr
```

Exit 1 with no provider means undecided, and the reason says which: nothing
answered, the endpoint serves neither configured model (it names what it does
serve — the diagnostic that matters), or both backends are up and the choice
is genuinely yours. The script then falls back to the platform default and
says so. `--provider omlx|dgx` or `ATLAS_LLM_PROVIDER` skips detection
entirely; detection is never wired into `Settings`, so config resolution stays
offline and deterministic.

## 2. Run the dashboard

```bash
python -m contingency_atlas.web            # http://127.0.0.1:8080
python -m contingency_atlas.web --port 9000
```

Loopback-only by design (the `--host` must be loopback; APIs verify
loopback peer + Host header). Restart after pulling code changes —
**Python does not hot-reload** (the classic "page 404s after an update"
symptom is a stale process).

The sidebar reflects the operator journey:

| Destination | Purpose |
|-------------|---------|
| **Briefing** | Current attention queue: pending review, due work, active jobs, and priority scenarios |
| **1 · Research** | **Evidence**, **Signals & Radar**, and **Findings** tabs for the governed research inputs and pattern outputs |
| **2 · Review** | Human decisions on pending records and proposals, with deterministic checks before machine assistance |
| **3 · Analyze** | **Run cycle**, **Validation**, and **Telemetry** tabs for execution, falsification, run history, and measured costs |
| **4 · Scenarios** | **Library** and **Compound candidates** tabs for discussion-ready hypotheses and the Phase-7 candidate register |
| **Settings** | Local model, framework, feed, and store configuration |
| **Method & Help** | Workflow, method boundaries, limitations, and documentation links |

The primary journey is **research → review → analyze approved data →
scenarios**. Evidence, forward-looking signals, and findings share Research for
orientation, but they remain separate record types with separate semantics.

### Durable background work

Dashboard work is recorded in a private SQLite journal at
`<configured scenario database directory>/generated/operations/jobs.sqlite3`.
The queue admits at most 20 outstanding jobs and executes one phase at a time
under a process-safe lock. Job history survives refreshes and restarts; it is
not limited to the 25 entries formerly kept in memory.

The persisted states are `queued`, `running`, `completed`, `failed`,
`interrupted`, and `cancelled`. Existing dashboard consumers retain the
compatible `status` field (`running`, `done`, `error`); new consumers should
use `state` for the precise outcome. A phase that returns an error list is
marked failed and retains its partial result. Completion does not hide errors.

On startup, recovery marks a previously running phase **interrupted**. It does
not replay that phase: some writes may already have committed. An operator
inspects its result and explicitly retries, creating a new job linked to the
original. Queued work resumes only when its saved configuration fingerprint
matches the current settings. A changed configuration requires an explicit
retry. A live worker in another process is respected during reconciliation.

Queued jobs can be cancelled before execution. Running Python phases cannot
be safely preempted or rolled back, so cancellation of an active phase is
explicitly refused. The runner does not claim that cancelling a request undoes
its effects.

```bash
python -m contingency_atlas.operations jobs
python -m contingency_atlas.operations cancel-job JOB_ID --actor "Operator name"
python -m contingency_atlas.operations retry-job JOB_ID --actor "Operator name"
python -m contingency_atlas.operations recover-jobs --no-resume
```

Job arguments are retained locally so explicit retries can reproduce the
request. Only supported named scalar parameters are retained; configuration
credentials and unknown request fields are excluded. Source text is private
journal data, not telemetry, and is omitted from job API/list responses.
The journal is created with owner-only file permissions inside a private
operations directory. Retain this directory with the governed stores.

The runner API provides `list_jobs`, `recover_jobs`, `cancel_job`, and
`retry_job` alongside the compatible `start_job` and `get_job` calls. Short
lived CLI callers use `wait_for_jobs` to avoid exiting before their dispatcher
has finished.

## 3. Cadence (intended)

| When | What runs |
|------|-----------|
| Daily | Phase 1 ingest (as public evidence and artifacts arrive) |
| Weekly | Phase 2 risk monitor |
| Monthly | Phase 3 generate · Phase 5 patterns · Phase 7 compound join (28-day due gate) · Phase 8 graduate |
| Quarterly | Phase 4 prioritize · Phase 6 maintain |

Manual intake starts at **Analyze → Add Research**. Individual phase RUN
controls remain under the advanced disclosure; background jobs poll in the
job console.

The human step in the rhythm is **Review**: phases 1–2 write
`pending` records, synthesis phases consume approved-only — so the
execution rhythm is *run → review → run*. The first run can stage new
research for review; only a later run can incorporate the newly approved
records into synthesis. An empty full-cycle run is a pure re-synthesis pass
over the approved corpus.

## 3b. Review workflow (the human boundary)

1. Open **Review** (nav badge = pending count). Each record shows
   deterministic provenance checks and, for risks, per-ref resolution
   against the evidence store — the reasons a record can or cannot be
   approved, *before* deciding.
2. Optional: **PRE-ANALYZE ALL** (or per-record RE-ANALYZE) runs the
   advisory judge-model analysis as a background job; suggestions are
   cached and marked STALE if the record changes. Advisory only. For
   pending channel proposals the model produces no verdict — just a
   plain-English explanation of why the machinery proposed the tag/edge/
   channel, plus weaknesses to probe; the decision stays pure human
   judgment (ADR-014).
3. Optional, for evidence-poor records: **DEEP-RESEARCH** (seed sites
   optional) hunts public sources under hard caps and files candidate
   evidence as ordinary `pending` records in the same queue.
   **DEEP-RESEARCH ALL** does this as one batch job for every pending
   record with a fresh pre-analysis (capped at 10 per run; records
   whose memo is still current are skipped). Review
   and approve/reject those candidates like any evidence, then
   **ADOPT APPROVED REFS** attaches them to the risk under the
   register lock. Conversely, the refs table's per-row ✕ (or bulk
   **DETACH N UNRESOLVED**) removes refs that don't belong — packet
   URLs a monitoring cycle unioned in — under the same lock with a
   provenance stamp. For channel proposals the sequence is gated —
   DEEP-RESEARCH only unlocks after the pre-analysis exists (its
   weaknesses steer the search), it files no candidate evidence, and
   it ends with an advisory draft of your written reason, pre-filled
   into the decision panel for you to edit; a RE-ANALYZE afterwards
   grounds the explanation in the research findings.
4. Decide: both attestation checkboxes (approval) or a reason
   (rejection). The same audited functions serve the CLI
   (`scripts/review_records.py`) — use whichever front end you like.
5. The footer tracks suggestion-vs-decision agreement — if you always
   agree with the machine, you have stopped reviewing (ADR-013).

Both review jobs report inline in Review (running / done with counts / error
with the actual message) and in the **Analyze → Run cycle** job console.

### Scenario provenance

Every scenario carries an explicit `origin`: `operator_authored`,
`pipeline_generated`, `compound_graduation`, `synthetic_fixture`, or
`legacy_unknown`. Writers assign the value at insertion; migration uses
`legacy_unknown` when the historical source cannot be established. Preserve
this field in imports and exports. Do not infer provenance from an ID prefix.

The **Research → Evidence** tab presents the **Event register**. Its records are
the domain objects documented as **Evidence Events**: public or synthetic
examples with explicit provenance. No public command accepts an
operator-supplied event extract.

## 3c. Optional MITRE ATLAS workflow

The dashboard never downloads framework data. An operator explicitly selects
and obtains one official v6 file, verifies its expected digest during import,
and then uses the ordinary human review boundary. **Settings**
is the GUI path for the same local-file import (`local_path` or a file picker)
and shows the active content version, import time, and source hash. The CLI
path is unchanged. For the pinned `2026.07` example documented with this
release:

```bash
curl -fL \
  https://raw.githubusercontent.com/mitre-atlas/atlas-data/v2026.07/dist/v6/ATLAS-2026.07.yaml \
  -o /tmp/ATLAS-2026.07.yaml

atlas-framework mitre-import \
  --db atlas.db \
  --input /tmp/ATLAS-2026.07.yaml \
  --source-url https://github.com/mitre-atlas/atlas-data/releases/tag/v2026.07 \
  --expected-sha256 0e07bb07fc6423d72cdf24ddc2038a6905bcbc00ba571064153119ee1a5888d4 \
  --activate

atlas-framework status --db atlas.db
```

`--source-url` is operator-asserted provenance: the importer records it but
never fetches or authenticates it. `--expected-sha256` is the independent
exact-byte pin and is checked before YAML parsing. The import is bounded,
atomic, and sealed before activation; a failure leaves no partial release.

To complete the workflow:

1. Restart the dashboard if it was already running, open a scenario, and use
   **Propose MITRE ATLAS mapping** to search the active release.
2. Select a technique and write the hypothesized-applicability rationale. The
   action creates a pending proposal only.
3. Open **Review**, inspect the pinned technique and scenario snapshot, then
   approve or reject it with a named reviewer and written reason.
4. Open the scenario's Atlas Brief. An approved mapping appears with its
   pinned release, tactic, mitigation, maturity, platforms, and related case
   studies. Pending and rejected mappings do not appear.

Use `atlas-framework activate VERSION --db atlas.db` to select another sealed
imported release for new proposals. Older releases and approved mappings stay
readable. If the scenario changes, the old approval remains visible as stale
audit context and requires a new proposal and review; restoring the original
text does not reactivate it.

## 4. Evaluation commands (the falsifiable core)

```bash
atlas-eval demo                       # R0 generate + score + Task B/C → eval_report.{json,md}
atlas-eval ablations --risks … --events …
atlas-eval calibrate --risks … --events …
atlas-eval r1
atlas-eval ext-demo --risks … --events …
atlas-eval synthetic-cell-demo
atlas-eval multiseed                  # R0 go/no-go as a distribution over 10 generative seeds
atlas-eval bcal --risks … --events … --truth …   # Task B-cal: levels vs generative truth
atlas-eval loss --risks … --events … --truth …   # Task L: severity recovery + holdout log-lik
atlas-eval ledger --record --resolve  # judgment ledger: record → resolve → calibration report
atlas-score r0-generate
atlas-score pairs --risks … --events …
```

Eval outputs write under `generated/redesign/**`; the judgment ledger
lives in `generated/judgment/` (all gitignored).

## 4b. Validation triggers in the dashboard

**Analyze → Validation** hosts the same machinery as one-click cards
(background jobs, same outputs as the CLI):

| Card | What it runs |
|------|--------------|
| CRASH TEST | Full R0 eval suite: Task B ranking, Task C structure, B-cal levels, Task L severity |
| MULTIVERSE | Multi-seed R0 — the go/no-go metrics as a distribution over generative seeds |
| WHAT BREAKS FIRST? | Pre-registered ablations + κ / π₀×c sensitivity grid + challenger-vs-primary B-cal |
| REALITY CHECK | Judgment ledger: record → resolve → Brier over resolved claims (honest n=0) |
| HIDDEN CONNECTIONS | OMLX channel audit → pending channel proposals in Review (needs endpoint) |
| PRECEDENT | OMLX analogue elicitation per top compound; optional contamination canary (needs endpoint) |
| DARK MATTER JOIN | OMLX search for currently unmodeled couplings; records directed steering and keeps graduation frozen (needs endpoint) |

Two more surface elsewhere: **Scenarios → Compound candidates** carries the
WHAT-IF WATCH strip (enriched-graph pairs scored in shadow only, graduation
frozen) and a per-row "paper trail" (`GET
/api/compounds/history?pair_id=…` — the pair's append-only score
history), and the Review toolbar's FIND LOOKALIKE RISKS button (`POST
/api/review/scan-duplicates`) runs the deterministic duplicate scan;
candidates land as pending merge proposals in the same queue.

## 5. Monthly compound flow

1. `python -m contingency_atlas.compound_join` (due-gated; `--force` to
   override) — writes `compound_scenarios` (table + JSON artifact) and
   telemetry to `generated/telemetry/compound-join.jsonl`.
2. **Scenarios → Compound candidates** shows the register with
   holdout-survival flags.
3. Phase 8 (RUN from **Analyze → Run cycle**, needs OMLX) graduates
   survivors into the library as `ATLAS-CMP-*`.

## 6. Telemetry and costs

- Every LLM call lands in `generated/telemetry/llm.jsonl` via the
  instrumented client (tokens from endpoint usage, latency, node label;
  Phase-3 generation/judge calls also carry `scenario_id`, which drives
  per-scenario attribution in **Analyze → Telemetry**).
- **Analyze → Telemetry** aggregates all telemetry logs; the legacy `/costs`
  route redirects to that tab, and token-usage rollups read
  `/api/tokens/usage`.
- Dollar figures require a pricing config: pass `pricing={...}` to
  `TelemetryLogger` (see `telemetry.load_pricing`), with
  `input_per_mtok`/`output_per_mtok` (and `shadow_*` for local models).

## 7. Troubleshooting

| Symptom | Cause / fix |
|---------|-------------|
| New page 404s | Stale dashboard process — restart it |
| "LLM backend unreachable" | Endpoint down; phases 1–3, 8 disabled; 4–7 run offline |
| Phase 1 rejects artifact | > 8 MB, or PDF/Office without the `[ingest]` extra, or image without captioning support |
| Compound join writes nothing | Due gate (< 28 days) or `insufficient_data` (sparse series) — read `compound_join_runs.note`. The series is approved-only: pending/rejected records do not count toward the 20-activation sufficiency bar |
| Pre-analysis fails on every record instantly | Judge model id not served by the active backend — compare `model_judge` with `GET <base_url>/models` |
| Deep research ends `budget_exhausted` | Page/iteration caps hit mid-hunt — RESEARCH AGAIN continues; caps are constants in `research.py` |
| Research page shows "no title or text content" | JavaScript-shell page (e.g. YouTube) — not text-fetchable; ingest the transcript via the Phase 1 artifact path |
| Decide/adopt returns 409 "register is locked" | Monitoring cycle or another reviewer holds the register lock — retry shortly; the lock auto-releases if its process dies |
| Approve button greyed with attestations ticked | A validation check is failing — hover the button; the tooltip names the failing checks |
| ATLAS import reports a SHA-256 mismatch | The local bytes do not match `--expected-sha256`; confirm the selected v6 release asset and independently obtained digest rather than bypassing the pin |
| ATLAS search says no active release | Import a local v6 file with `--activate`, or run `atlas-framework activate VERSION`; application startup deliberately does not download one |
| An approved ATLAS card says stale | A meaning-bearing scenario revision changed; file and review a new proposal if the mapping still applies. Restoring text is intentionally insufficient |
| Suite flake: `test_run_rejects_oversized_body_before_dispatch` | Timing-sensitive under load; passes standalone and on rerun |

## 8. Safety rails (do not bypass casually)

- `ATLAS_AUTO_RETIRE` gates merge/retire actions in Phase 6; the UI
  cannot override it.
- The egress gateway audit log is append-only; `verify_audit_chain()`
  detects tampering.
- Runtime artifacts (`emerging_risks.json`, `emerging_risks.json.lock`,
  `compound_scenarios.json`, `generated/`) are gitignored — do not
  commit them, and do not hardcode their contents in tests.
- Machine assistance at the review boundary is advisory-only: nothing
  the pre-analysis or deep research produces can reach `approved`
  without the human attestations (ADR-013). Do not "fix" that.
- MITRE ATLAS content is untrusted, display-only context. Do not insert it into
  LLM prompts or scoring paths, bypass the proposal review, unseal a release,
  or rewrite pinned provenance.

## 9. Verified backups and recovery into a new directory

The operations CLI backs up the **configured** scenario database, evidence
database and risk register, including their human decision history. It uses
SQLite's online backup API, so committed WAL data is included; copying only a
live `.db` file is insufficient. During capture, it excludes pipeline workers,
holds the risk-register lock and reserves the SQLite stores against writes.
A busy worker causes an explicit refusal. Retry after the work finishes.

The bundle also includes a safe configuration reference, the job journal when
present, the configured organization context database and its sibling
`integration-outbox.db` when present, and the judgment/egress ledgers under the scenario database's sibling
`generated/` directory. If those legacy artifacts live elsewhere, supply their
explicit `generated/` location with `--artifacts-root`. Environment variables,
raw configuration files, API keys and URL credentials are not copied. Governed
research inputs, private frozen integration snapshots and reviewer names remain private operational data, so store
the complete bundle outside the public code repository.

```bash
python -m contingency_atlas.operations backup \
  ~/atlas-private-backups/example-snapshot --actor "Operator name"
python -m contingency_atlas.operations verify \
  ~/atlas-private-backups/example-snapshot
python -m contingency_atlas.operations restore \
  ~/atlas-private-backups/example-snapshot ~/atlas-restore-review \
  --actor "Operator name" --confirm-restore
```

Both backup and restore destinations must be new directories. Restore cannot
overwrite even an existing empty directory and never writes into the live
stores. It verifies every manifest size and SHA-256 digest, checks SQLite
integrity and core JSON shape, copies only recognized paths, and verifies the
copied bytes again before publishing the result. The manifest is published
after the data files; a failed operation removes its unfinished staging data.
Hashes detect changed bytes against this manifest; they do not authenticate
an untrusted manifest or replace independently retained backup storage.

A restore receipt records the named operator, time and source. The CLI returns
the `ATLAS_DB`, `ATLAS_EVIDENCE_DB` and `ATLAS_EMERGING_RISKS` values for the new
directory. Inspect the restored records and `configuration.json` first, then
explicitly point a separate application session at those paths. Credentials
must be supplied again through the environment. Nothing switches the live
application automatically. Restored queued jobs retain their original
configuration fingerprint and require operator review if store paths change.
When present, the private context and integration outbox restore together and
the result also reports `ATLAS_CONTEXT_DB`; activation remains an explicit
operator choice. An enabled but missing context database blocks backup rather
than silently omitting it. Existing disabled private stores are retained.
Proxy signing secrets and replay-protection nonce databases are excluded.
Restart any proxy with fresh signing material when activating a recovered
deployment, and allow outstanding signed requests to expire before cutover.

Schedule backup execution and retention in the deployment's existing operating
environment, retain an independent private copy, and periodically verify a
restore into a fresh directory. The older `scripts/data_history.py` remains
useful for local diffs, but it is not a substitute for this configured-store
backup/restore path or protection against loss of the local disk.

```bash
python -m contingency_atlas.operations readiness
```

Readiness performs no network requests. It reports primary-store availability,
private-store integrity, process-lock support, durable-job counts and interrupted
work. It distinguishes local mode from explicitly configured signed-proxy mode
and reports invalid access configuration. It cannot verify an external login
deployment. See [Shared deployment](shared-deployment.md) for the single-tenant
identity-proxy contract; tenant isolation and an identity provider are not
supplied by the backup or readiness controls.
