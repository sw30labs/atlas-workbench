# Security & Confidentiality

## 1. Data classification

| Class | Examples | Placement / compute |
|-------|----------|---------------------|
| `public` | public evidence, public registers, project taxonomies, imported public framework data | any |
| `aggregate` | synthetic scoring-cell counts, severity quantiles, segment codes | local by default; may cross the egress gateway under allow-list |
| `local_detail` | synthetic event rows and red-team prose | local only; **never** to external APIs |

## 2. The egress gateway (`scoring/egress.py`)

The only approved path from local processing to external compute.

1. **Allow-list schemas** per payload type (`scenario_structure`,
   `scoring_codes`, `public_refs`, `telemetry_rollup`, `judge_prompt`,
   `scoring_features`, plus dedicated request schemas for channel audit,
   analogue elicitation, and refutation memos). Unknown schemas, missing
   required keys, and unknown keys deny.
2. **Deny scans**: forbidden free-text keys (narrative, description,
   incident, legal, person, employee, customer, notes…), email-like
   tokens, account-like digit runs (8–17 digits), overlong prose
   (> 240 chars, URLs exempt). Scans recurse through nested
   dicts/lists.
3. **Audit log**: append-only JSONL, each entry hash-chained to the
   previous (`prev_hash`/`entry_hash`); `verify_audit_chain()`
   recomputes retained entries and detects broken links or modified content.
   Each append holds a process-safe file lock while verifying the existing
   chain, reading its current tail, appending, and flushing to durable storage.
   Separate dashboard/CLI processes and concurrent threads therefore share
   one chain. Malformed or truncated entries stop further audited egress;
   verification returns false. Remote LLM preflights use
   `generated/egress_audit.jsonl` by default. Detecting deletion of a complete
   suffix requires an independently retained last-entry digest; the local log
   alone is not a signed or externally anchored audit store.
4. **Behavior on deny**: abstain (no retry, no route-around); a
   `denied_egress` telemetry event is emitted.

Red-team posture (tested): synthetic local-detail strings in keys, values, and
nested lists are all denied; the chain detects a flipped decision.

## 3. Synthetic scoring-cell boundary

- `synthetic_events.py` generates fictional rows in memory; no public reader
  accepts an operator-supplied event file.
- `synthetic_aggregation.py` validates the generated rows, strips planted
  prose, and suppresses groups below `min_group_size`.
- Groups use neutral `scoring_cell_code` and `segment_code` fields.
- `aggregate` `scoring_features` are allow-listed; `local_detail` synthetic event rows have
  no egress schema and are denied.
- `atlas-eval synthetic-cell-demo` writes only clearly named synthetic demo
  artifacts under `generated/scoring/synthetic-cell-demo/`.

## 4. Network posture

- Dashboard binds loopback only (`--host` validated); APIs additionally
  require loopback Host header; mutations require loopback Origin.
- Inference is local: OMLX (Mac MLX) or the DGX vLLM server, selected by
  `llm_provider`. Both are expected on loopback — a DGX reached over an
  SSH tunnel qualifies. `OMLXClient` refuses to construct against a
  non-loopback endpoint unless `ATLAS_ALLOW_REMOTE_LLM=1` is set, and logs
  a WARNING naming the host when it is. This matters because `DGX_BASE_URL`
  and `VLLM_BASE_URL` may be inherited: without the check, a remote
  value would turn every prompt — register titles, evidence prose, scenario
  drafts — into off-host egress. Channel audit, analogue elicitation, and
  refutation memo calls additionally validate the exact structural request
  body through `EgressGateway` before transport; a deny or gateway failure
  abstains without retrying. Loopback inference bypasses that remote-only
  preflight, and ambient proxy settings are ignored for loopback endpoints
  (`trust_env=False`).
- Artifact uploads are base64 JSON with an 8 MB cap; the conversion
  endpoint writes nothing.
- Deep research (`research.py`) queries DuckDuckGo's HTML endpoint and
  fetches a capped number of public pages per run (≤ 6 pages). Search bodies
  are capped at 1 MB, ordinary page bodies at 400 KB, and PDFs at 8 MB.
  `research_transport.py` streams successful responses and rejects oversized
  bodies before parsing; redirect/error bodies are closed without reading their
  contents. It requests
  identity encoding and rejects compressed responses before decompression.
  The complete network exchange runs in a killable child process under one
  deadline, covering DNS, TLS, HTTP headers, bodies and up to three redirects.
  Every hop receives structural URL validation, excluded-host checks and DNS
  validation. If any DNS answer is non-public, the request is denied. The
  connection uses the checked address directly, preserving the original Host
  header and TLS certificate/SNI hostname without a second DNS lookup. Ambient
  proxies are ignored. The address validation, pinned socket and process
  cleanup helpers are shared with optional public-source scouting.
  PDFs are parsed locally with pypdf (≤ 20 pages, text-only); extracted text
  remains untrusted data. The model can only select URLs returned by search,
  never invent additional fetch targets. Programmatically injected httpx
  transports are an offline test seam; dashboard/API input cannot select one.

## 5. Secrets

- `OMLX_API_KEY` only via environment; never in config files.
- No credentials in telemetry (counts and metadata only; prompts are
  never logged).

## 6. Imported-framework boundary

The optional MITRE ATLAS catalog is classified `public`, but public does not mean
trusted. The integration treats both its YAML structure and every upstream
text field as hostile input:

- the application never downloads ATLAS content and never contacts MITRE at
  startup; an operator must provide an explicit local file and an
  operator-asserted source URL, which is recorded but never fetched;
- the importer reads one descriptor with a strict byte cap, bounds YAML aliases,
  graph nodes, and nesting, rejects recursive aliases and duplicate keys, then
  validates the v6 shape, object types, identifiers, maturity/platform values,
  and relationship endpoints in one transaction;
- the SHA-256 of the exact input bytes is recorded and may be verified against
  an independently supplied `--expected-sha256` before parsing; a different
  payload claiming an existing content version is rejected;
- successful imports are sealed before activation, so later SQL cannot append
  objects or relationships to the snapshot identified by that digest;
- object descriptions and related case-study text are stored only as catalog
  data, escaped at the browser boundary, and never interpolated into an HTML
  template or executable script;
- imported descriptions are never placed in an LLM prompt. The minimum
  mapping flow is manual, and framework content remains outside every scoring
  and generation path; and
- catalog search and proposal inputs are length-bounded. Proposal mutation
  uses the dashboard's existing loopback Host and Origin checks.

An imported release can be deactivated but is retained because approved
mappings are pinned to it. This limits silent supply-chain drift: activating a
new release changes the catalog offered for new proposals, not the bytes or
relationships used to explain an old decision.

## 7. LLM-specific risks and controls

| Risk | Control |
|------|---------|
| LLM invents URLs/facts | lane-labeled extraction + deterministic URL gate + human review (ADR-006) |
| LLM writes into score path | forbidden by ADR-001; score fields inserted from stored realizations only |
| Prompt exfiltration via logs | telemetry logs metadata only (ADR-007); no prompt storage |
| Model version drift in judge | QA gate threshold + recorded judge model per call in telemetry |
| Automation bias at the review boundary | advisory-only rendering (no pre-selected decision, attestations never pre-ticked) + suggestion-vs-decision agreement audit (ADR-013) |
| Prompt injection via fetched web pages / PDF text | research prompts treat all fetched content as untrusted data; fetched content cannot add fetchable URLs (search results are the only URL source); outputs are proposals, not decisions |
| Prompt injection via imported framework descriptions | ATLAS text is display-only, browser-escaped, and excluded from all LLM prompts and scoring paths |
| Evidence laundering ("the agent will find *something*") | coverage judge may return `no_evidence_found` as a first-class verdict; every candidate is `pending` and needs attested human approval; `origin: deep_research` provenance keeps machine-found evidence auditable forever |
| Model attests what only a human can | prompts forbid it; `human_must_verify` always lists public-access and rights checks; the attestation checkboxes exist only in the human's browser |
