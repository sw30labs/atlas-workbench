# API Reference

The dashboard server (`python -m contingency_atlas.web`, default
`http://127.0.0.1:8080`) is loopback-only: API paths require a loopback
peer **and** a loopback Host header; mutation requests additionally
require a loopback Origin. Bodies are JSON with size caps.

## Pages

| Path | Page |
|------|------|
| `GET /` | Command deck: Briefing → Research → Review → Analyze → Scenarios, plus Settings and Method & Help |
| `GET /costs` | Legacy alias — 302 redirect to `/#costs`, the Analyze → Telemetry tab |
| `GET /favicon.svg` | Icon |

## Read APIs

### `GET /api/health`

```json
{"ok": true}
```

### `GET /api/data`

Aggregate dashboard payload: `meta`, `scenarios`, `evidence_events`, `risks`,
`runs`, `patterns`, `causal`, `compounds`, `compound_join_runs`. Metadata uses
`evidence_event_count`; no legacy evidence aliases are emitted. The
`compounds` rows carry member titles, holdout-survival flag, and
graduation status. `meta.mitre_atlas` reports the optional local catalog
status. Each scenario carries `mitre_atlas.active_release` and
`mitre_atlas.mappings`; that list contains approved claims only, marks each
one `current` or `stale`, and resolves technique context plus source URL and
SHA-256 from the mapping's pinned release. Stale approvals remain visible as
audit context but are not treated as current. These fields are display-only
and never enter a score.

`Evidence Events` is the canonical public label for the `evidence_events`
collection. Loss amounts and regions are optional attributes of an evidence
event, not a separate private-data lane.

### `GET /api/frameworks/mitre-atlas/status`

Reports installed local releases, active version, exact-byte provenance,
sealed state, and object counts. With no import it returns `installed: false`,
`active_release: null`, and zero counts; the rest of the dashboard continues
to work.

### `GET /api/frameworks/mitre-atlas/techniques`

Searches the active local release. Query parameters are `q` (name, ID, or
description), `platform`, `maturity`, and `limit` (1–50, default 20). Results
include the resolved `content_version`; the endpoint never downloads framework
data. Invalid filters and bounds return 400.

### `GET /api/settings`

Operator Settings payload for the Command Deck: MITRE ATLAS catalog status
and recommended local pin, greyed planned frameworks (ATT&CK, NIST AI RMF),
scout-registry status (proposal-only), planned/gated feeds, configured vs
served models, env-override warnings, and local store paths. The endpoint
never downloads framework data. `inference.restart_required_for_changes` is
always true.

### `GET /api/about`

About-page content from `web/about.yaml` (or `ATLAS_ABOUT`).

### `GET /api/omlx`

```json
{"reachable": true, "base_url": "http://127.0.0.1:8000/v1",
 "model": "DeepSeek-V4-Flash-0731-oQ8-mtp", "auto_retire": false}
```

### `GET /api/intake/sources`

Optional adjacent sources for Add Research. Currently reports local
Singularity Atlas Neo4j reachability. Missing driver, a down database, or a
non-loopback URI returns `available: false` with a reason — never a 500.
The password is not included.

```json
{"singularity_atlas": {
  "available": false,
  "reason": "unavailable (ServiceUnavailable)",
  "uri": "bolt://127.0.0.1:7689",
  "driver_installed": true,
  "loopback": true}}
```

### `GET /api/scenarios/recommendation`

Deterministic coverage recommendation over the **approved** corpus only.
Pending, `needs_update`, rejected, and draft intake records are ignored.

```json
{"recommended": 2, "recommended_count": 2, "based_on": "approved_corpus",
 "range": {"min": 1, "max": 3}, "cap": 10,
 "basis": {"approved_evidence": 12, "approved_risks": 8,
           "eligible_risks": 6, "active_scenarios": 4,
           "uncovered_risks": ["…"], "ignored_unapproved_evidence": 0,
           "ignored_unapproved_risks": 0},
 "rationale": ["…"]}
```

### `GET /api/jobs/{job_id}`

Background job status: `{id, phase, status, started_at, finished_at,
result, error}`.

### `GET /api/review/pending`

The review queue: every pending evidence event and emerging risk, each
with its deterministic validation report, its cached advisory
suggestion (if any, with a staleness flag), and its latest
deep-research memo (if any, with candidate review statuses resolved).
Pending channel proposals appear under `channel_proposals.pending`;
their suggestion is explanation-only (`explanation` + `weaknesses`,
never a recommendation) and their research memo may carry a
`draft_reason`. Pending MITRE ATLAS applicability proposals appear under
`mitre_atlas_proposals.pending` with pinned technique context and a
scenario-snapshot currency check. Read-only. See
[review-and-research.md](review-and-research.md).

```json
{"events": [{"kind": "event", "record_id": "EVT-…", "record": {"…"},
             "report": {"approvable": false, "checks": ["…"], "refs": [],
                        "blockers": ["…"]},
             "suggestion": {"recommendation": "reject", "confidence": 0.8,
                            "reasons": ["…"], "human_must_verify": ["…"],
                            "human_action": null},
             "suggestion_stale": false,
             "research": {"verdict": "enough", "coverage": ["…"],
                          "candidates": ["EVT-DR-…"],
                          "candidate_status": [{"event_id": "EVT-DR-…",
                                                "review_status": "pending"}]}}],
 "risks": ["…"],
 "merge_proposals": {"pending": ["…"], "acceptance": {"…"}},
 "channel_proposals": {"pending": ["…"]},
 "analogue_proposals": {"pending": ["…"]},
 "mitre_atlas_proposals": {"pending": ["…"]},
 "agreement": {"decided": 4, "agreed": 3},
 "generated_at": "…"}
```

### `GET /api/eval/reports`

The latest EVAL/judgment artifacts written by the Analyze → Validation trigger
cards, parsed for the Eval & governance reports panel:

```json
{"crash_test": {"path": "generated/redesign/phase1/r0/eval_report.json",
                "modified_at": "…", "report": {"…"}},
 "parallel_worlds": {"…"}, "ablations": {"…"}, "judgment": {"…"}}
```

Each entry is `null` when the artifact is missing or corrupt (the panel
renders "not run yet"). Informational only.

### `GET /api/costs/summary`

Aggregated from `generated/telemetry/*.jsonl` (404 when none):

```json
{
  "n_events": 244, "spend_usd": 0.0, "spend_shadow_usd": 0.0,
  "n_promoted": 20, "cost_per_promoted_usd": 0.0,
  "tokens": {"input": 520, "output": 189, "cache_read": 0, "cache_write": 0},
  "cache_hit_rate": 0.0, "egress_denies": 0,
  "by_phase": {"evaluation": {"cost_usd": 0.0, "n_events": 218, "tokens": 709, "input_tokens": 520, "output_tokens": 189}},
  "by_model": {"DeepSeek-V4-Flash-0731-oQ8-mtp": {"cost_usd": 0.0, "n_events": 2, "tokens": 709, "input_tokens": 520, "output_tokens": 189, "latency_avg_ms": 5384}},
  "by_node": {"score_det": {"…"}},
  "attributed": [{"scenario_id": "scn-…", "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}],
  "unattributed": {"input_tokens": 520, "output_tokens": 189, "cost_usd": 0.0},
  "recent": [{"ts": "…", "node": "ingest_convert", "tokens": 448, "cost_usd": 0.0, "latency_ms": 5246, "status": "ok"}]
}
```

Buckets carry the input/output token split so Analyze → Telemetry's
**what-if panel** can re-price them client-side: the user enters
hypothetical input/output $-per-million-token rates and the spend by
phase/node/model, the per-scenario attribution, and the totals
recompute live (amber what-if figures alongside the metered ones —
nothing is written back to the server; rates persist in the browser and
can be shared as `?pin=…&pout=…` URL parameters). Rows with no metered
tokens — deterministic nodes like `score_det`/`compound_join` — show
"—" instead of a $0.00 what-if. Scenario rows price LLM work attributed
via `scenario_id` (Phase 3 generation + judge calls); cycle-level LLM
tokens stay in the unattributed row.

### `GET /api/tokens/usage?period=7d|7w`

Situation-room chart series. `7d` = daily buckets (last 7 calendar
days), `7w` = ISO-week buckets (last 7 weeks):

```json
{"period": "7d",
 "buckets": [{"label": "08-02", "input": 520, "output": 189}],
 "totals": {"input": 520, "output": 189}}
```

## Mutation APIs

### `POST /api/settings/frameworks/mitre-atlas/import`

Import one operator-supplied local MITRE ATLAS v6 YAML. Exactly one of
`local_path` (absolute `.yaml`/`.yml` file) or `content_b64` + `filename`
is required. `source_url` is recorded and never fetched. Optional
`expected_sha256` is verified before parse. `activate` defaults to true.
Same importer as `atlas-framework mitre-import`. Body cap matches artifact
ingest. Response: `201 {"ok": true, "import": {"…"}, "status": {"…"}}`.

### `POST /api/settings/frameworks/mitre-atlas/activate`

Activate an already imported content version. Older releases stay readable.

```json
{"content_version": "2026.07"}
```

### `POST /api/settings/models`

Write generation / judge / final-judge ids into `contingency-atlas.yaml`
(or `ATLAS_CONFIG`). Comments are preserved. The running process is not
live-mutated; restart the dashboard. Environment overrides (`OMLX_MODEL`,
…) return 409.

```json
{"model": "DeepSeek-V4-Flash-0731-MXFP4-MLX",
 "model_judge": "DeepSeek-V4-Flash-0731-MXFP4-MLX",
 "model_final_judge": "",
 "use_responses_api": false}
```

`use_responses_api` is OMLX-only. When true, the next dashboard start sends
generation through `POST /v1/responses` instead of `/v1/chat/completions`.
`GET /api/settings` reports `inference.responses_api.supported` from a GET
probe of that route (405/401 means present; 404 means absent).

### `POST /api/frameworks/mitre-atlas/proposals`

Creates one manual, hypothesized pending mapping against an explicit imported
release or the active release. It cannot approve a mapping.

```json
{"scenario_id": "SCN-…", "technique_id": "AML.T0051",
 "content_version": "2026.07",
 "rationale": "The scenario contains an untrusted instruction path."}
```

The scenario, release, and technique must resolve. Rationale is required and
bounded; an identical live identity returns the existing pending proposal.
The standard loopback peer, Host, Origin, and JSON-body limits apply. Response:
`201 {"ok": true, "proposal_id": "map-…", "proposal": {"…"}}`.

### `POST /api/run`

Starts a background phase job (one at a time; 409 while busy).

```json
{"phase": "phase1|phase2|phase3|phase4|phase5|phase6|compound_join|compound_graduate|review_analyze|review_research|full",
 "evidence_texts": "…", "source_urls": "…", "source_text": "…",
 "risk_source_urls": "…", "num_scenarios": 1}
```

Phase-specific requirements are validated server-side (e.g. phase 1
needs ≥1 evidence text and one canonical URL per item). Response:
`202 {"job_id": "web-…"}` or `4xx {"error": "…"}`.

Two phases are dashboard-side jobs rather than pipeline nodes:

- `review_analyze` — advisory pre-analysis of the pending review queue
  (optionally one record via `kind` + `record_id`, `force` to bypass
  the suggestion cache; `kind` is `event`, `risk`, or `channel` —
  channel proposals get an explanation with weaknesses, never a
  recommendation). Writes only the suggestion cache.
- `review_research` — bounded deep research for exactly one pending
  record (`kind` + `record_id` required; `seed_domains` optional,
  whitespace/comma separated). Writes a memo plus `pending` candidate
  evidence — never review state. Channel runs are gated on a fresh
  advisory explanation, file no candidates, and add an advisory
  `draft_reason` to the memo.

### `POST /api/review/decide`

One human review decision through the audited flow (same functions as
`scripts/review_records.py`).

```json
{"kind": "event|risk|merge|channel|analogue|mitre_atlas", "record_id": "…", "action": "approve|reject",
 "confirm_public_source": true, "confirm_rights": true,
 "note": "…", "reason": "…", "reviewer": "…"}
```

For events and risks, approval requires both attestations and rejection
requires a reason. The proposal kinds (`merge`, `channel`, `analogue`,
`mitre_atlas`) take a single named reviewer plus a written reason instead —
no attestations; a channel approval flips the proposal row only (it feeds the
compound join's shadow pass — the graph itself is never mutated). MITRE ATLAS
approval revalidates the scenario revision and pinned technique inside the
decision transaction, changes only the decision fields, and never mutates
scenarios, catalog rows, or scores.
Responses: `200 {"ok": true, "record": {"…"}}`,
`400 {"error": "…"}` (validation/attestation), `409` when the register
is locked.

### `POST /api/review/adopt-refs`

Attach **approved** evidence event ids to a **pending** risk's
`evidence_refs`, under the register lock, provenance-stamped and
idempotent. Any id that is missing or not fully approved rejects the
whole request with a 400 naming it.

```json
{"record_id": "…", "event_ids": ["EVT-DR-…", "EVT-…"]}
```

Response: `200 {"ok": true, "added": ["…"], "record": {"…"}}`.

### `POST /api/review/detach-refs`

Detach refs from a **pending** risk's `evidence_refs` — the mirror of
adopt-refs and the only sanctioned way to remove refs (e.g. URLs a
monitoring cycle unioned in from the broader source packet). Any ref
value may be detached. Under the register lock, provenance-stamped
("Evidence refs detached by human review: …"), idempotent (absent refs
are skipped), and recomputes the derived `evidence_strength`.

```json
{"record_id": "…", "refs": ["https://stale.example/…", "EVT-…"]}
```

Response: `200 {"ok": true, "removed": ["…"], "record": {"…"}}`.

### `POST /api/ingest/convert`

Convert an artifact into a **draft** (writes nothing). Cap: 8 MB raw.

```json
{"filename": "briefing.pdf", "content_b64": "…", "mode": "evidence|intelligence"}
```

or `{"text": "…", "mode": "evidence"}` for pasted text. Response:

```json
{
  "filename": "briefing.pdf", "converter": "markitdown",
  "warnings": [], "markdown_preview": "…",
  "source_text": "block one\n\nblock two",
  "source_urls": ["https://…/one", "https://…/two"],
  "unattributed": ["text the artifact gave no URL for"],
  "orphan_urls": ["https://…/unclaimed"],
  "chunks_processed": 1, "chunks_failed": 0, "truncated": false,
  "sources": [
    {"publisher": "CISA", "reference": "advisory …",
     "url": null, "origin": "unresolved", "confidence": "high", "note": "…"},
    {"publisher": "", "reference": "", "url": "https://…",
     "origin": "literal", "confidence": "high", "note": "link in artifact"}
  ],
  "rubric": {"verdict": "pass", "missed_sources": [],
             "attribution_fidelity": {"verdict": "pass"},
             "body_fidelity": {"verdict": "pass"},
             "load_bearing_facts": {"who": "present", "…"},
             "url_validity": {"checked": 1, "invalid": [], "verdict": "pass"}}
}
```

`source_text` and `source_urls` are the two fields the Phase 1 modal pre-fills,
and they are produced from **one** list in one order: blocks are separated by a
blank line, so `len(source_text.split("\n\n")) == len(source_urls)`. Phase 1
rejects any run where those counts disagree, so nothing else may fill them.
Two records may legitimately share a URL — one article often reports several
events — and `phase1_ingest` pairs positionally without requiring uniqueness.

Text that carries no URL goes to `unattributed`, and links no record claimed go
to `orphan_urls`. Neither pre-fills the form: an unsourced record cannot be
ingested, and padding either lane to make the counts agree would attach
provenance to a claim that does not have it. A record's URL is kept only if it
is among the URLs harvested from that same chunk.

Offline (no OMLX): `sources` contains the literal lane only, `source_text` is a
raw preview, `source_urls` is empty and the harvested links are returned as
`orphan_urls` — with nothing to attribute them to, pairing is left to the
reviewer. A warning explains the degradation.

The dashboard's primary **Add Research** path does not use this converter.
It posts mixed material to `/api/intake/prepare` instead.

### `POST /api/intake/prepare`

Read-only preparation of one free-form research packet. Writes no evidence,
risk, review, scenario, or run record. Requires a reachable LLM backend
(503 otherwise). Cap: 200,000 characters.

```json
{"research_material": "mixed notes and https://public.example/notice",
 "include_singularity_atlas": false}
```

`include_singularity_atlas` is optional. When true, the server may append a
bounded pull of **public-feed** stories from the local Singularity Atlas
graph (Loop archive editions are excluded). The pull is fail-soft: if Neo4j
is unreachable the pasted material is still prepared. Paste may be empty
only when this flag is true and eligible stories were selected. The draft
includes a `singularity_atlas` decision table (selected / skipped with
rationale). The same table is appended to
`singularity_intake_jobs.jsonl`.

Response `201` is a transient draft: `draft_id`, `summary` counts for
`ready` / `needs_attention` / `already_known`, per-item QA, source-shape
badges, duplicate findings, and input-unit accounting. The draft expires
in-process after one hour. URL badges report literal public HTTP(S) shape,
not reachability or rights.

### `POST /api/intake/submit`

Queue selected **ready** draft items as `pending` evidence and risk records.
Held, exact-duplicate, and unknown ids are rejected. A risk is queued only
when its linked evidence is selected in the same submission or already
exists in the evidence store. Partial success is reported per item; it is
not presented as complete success.

```json
{"draft_id": "draft-…", "selected_ids": ["INTAKE-…"]}
```

Response `201`: `queued`, `submitted` counts, `queued_items`,
`skipped_duplicates`, `held`, and `review_url`. `409` if the draft expired
or the selection is no longer available.

## Command-line interfaces

| Command | Purpose |
|---------|---------|
| `atlas-score r0-generate [--out DIR] [--seed S] [--quarters N] [--atoms N] [--channels N]` | Write the default Bernoulli synthetic R0 dataset; use the Python `R0Config` API for alternate count models |
| `atlas-score pairs --risks F --events F [--out DIR]` | Batch-score all pairs; ScoreRealizations + telemetry + run report |
| `atlas-eval demo [--out DIR]` | One-command R0 reproduction (generate + score + Task B/C) |
| `atlas-eval run --risks F --events F [--truth F] [--strict]` | Task B/C on given data |
| `atlas-eval multiseed [--out DIR] [--seeds N]` | Repeat Task B/C across the pre-registered synthetic seed set |
| `atlas-eval bcal --risks F --events F --truth F` | Task B-cal compound-level calibration against generative truth |
| `atlas-eval loss --risks F --events F --truth F` | Task L severity recovery and holdout likelihood evaluation |
| `atlas-eval calibrate --risks F --events F` | Task A calibration report |
| `atlas-eval r1 [--out DIR]` | Task B on the R1 public series (or documented insufficiency) |
| `atlas-eval ablations --risks F --events F` | Pre-registered ablation matrix |
| `atlas-eval ext-demo --risks F --events F [--with-llm-null]` | External feature screen on planted series |
| `atlas-eval synthetic-cell-demo [--out DIR]` | Fictional event generation → scoring-cell aggregation → egress check → Task B/C |
| `atlas-eval ledger [--record] [--resolve] [--report]` | Record, resolve, or report hash-chained judgment claims |
| `atlas-framework mitre-import --input F --source-url U [--expected-sha256 H] [--activate] [--db DB]` | Validate and transactionally import one operator-supplied local ATLAS v6 YAML; record operator-asserted provenance, verify an optional exact-byte digest, and optionally activate the sealed snapshot |
| `atlas-framework status [--db DB]` | Show installed releases, provenance, sealed/active state, and object counts |
| `atlas-framework activate VERSION [--db DB]` | Activate a sealed imported content version without deleting older releases |
| `python -m contingency_atlas.compound_join [--force]` | Monthly join (due-gated; force overrides) |
| `python -m contingency_atlas.web [--host H] [--port N]` | Dashboard (loopback hosts only) |
