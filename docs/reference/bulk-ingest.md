# Bulk ingest pods

Status: implemented on `feat/acme-bulk-ingest` (2026-10-18). Design:
[`docs/research/plans/ACME_BULK_INGEST.md`](../research/plans/ACME_BULK_INGEST.md).

An operator points Atlas at a folder of organization documents. A pod
pipeline inventories the folder, reads every file, routes it to contracts,
extracts candidate records with provenance, links and deduplicates them across
files, raises batch-level issues and stages one reviewable batch. **Nothing is
imported, approved, attested or published by the pods.** The existing human
gates stay where they are:

- organization context goes through `imports.preview_import` /
  `imports.submit_import` with a named reviewer;
- external public events go through the draft-first research intake and land as
  `pending`;
- `internal_loss_events`, `org_scenarios` and `taxonomy_crosswalk` were agreed
  on 2026-10-18. They are now organization-context kinds in
  `context.RECORD_MODELS` and go through the same import gate. Crosswalk rows
  are always staged `status: "proposed"` with an empty reviewer and no
  `reviewed_on`. Pods never approve a mapping.

Code: `workbench_atlas/bulk_ingest/`. Tests: `tests/test_bulk_ingest_*.py`
(offline, synthetic fixtures built in `tmp_path`). Scorer:
`scripts/score_bulk_ingest.py`.

## Pipeline

| Pod | Module | Input → output | Lane |
|---|---|---|---|
| Inventory | `inventory.py` | folder → manifest: sha256, size, magic-byte format, duplicates, a reason for every file not accepted | deterministic |
| Reader | `readers.py` | file → `Document` (tables with header rows and locators, text blocks with locators, metadata) | deterministic |
| Triage | `triage.py`, `sections.py` | `Document` → routes with score, reasons and confidence; flags | deterministic; optional LLM tiebreak |
| Extractor | `extractors.py`, `sections.py`, `llm_lane.py` | routed document → `Candidate` records with provenance | tabular and pattern lanes deterministic; narrative LLM lane optional |
| Linker | `linker.py` | candidates → resolved service, vendor and mechanism links; stable ids; cross-file merge; contract validation; taxonomy crosswalk | deterministic |
| Critic | `critic.py` | batch → sorted issues (error, warning, info) | deterministic |
| Stager | `stager.py` | batch → `batch.json` plus handoff payloads | deterministic |

`orchestrator.run_batch` runs read, triage and extract per file in a bounded
`ThreadPoolExecutor` (`max_workers`, 1–16). Linking, criticism and staging run
once per batch. One failing file becomes an `*_failed` issue and the batch
continues. With `use_llm=False` the output is deterministic: the same folder
gives the same batch id, candidate ids and payload bytes.

### Inventory

- Never follows symlinks. A link that resolves outside the folder is
  `quarantined` ("symlink escapes the ingest folder"); an internal one is
  `skipped` ("symlink not followed"). Files open with `O_NOFOLLOW`.
- Skips hidden entries, `.DS_Store`, `Thumbs.db`, `__MACOSX`, `._*` and Office
  lock files (`~$*`, listed as `ignore`).
- Hard-excludes any file named `answer_key.json`. It is listed, never opened or
  hashed.
- Default limits (`inventory.Limits`): depth 8, 2000 files, 25 MB per file,
  1 GB per batch. Over-limit files are listed with the reason.
- Format comes from magic bytes. Zip containers are opened to tell
  xlsx/docx/pptx apart, so content wins over the extension. OLE containers with
  an OOXML extension are quarantined as encrypted. An OOXML or PDF extension
  whose bytes do not match is quarantined as corrupt.
- Byte-identical files: the first in walk order is read; the others are
  `duplicate` with `duplicate_of`.
- Readers re-hash the file and refuse it if it changed after inventory.

### Readers

| Format | Reader | Notes |
|---|---|---|
| csv/tsv | stdlib `csv` | Delimiter sniffed (`, ; tab |`). Encoding tries utf-8-sig/BOM, utf-8, cp1252, then latin-1. |
| xlsx | `openpyxl` (`read_only`, `data_only`) | Formulas are read as cached values. Merged ranges come from the sheet XML and fill their cells. Hidden sheets are read and reported. |
| docx | `python-docx` | Paragraphs in body order with heading levels, plus tables. |
| pptx | `python-pptx` | Slide text, grouped shapes, tables and notes. The slide title is kept as table context. |
| pdf | `pypdf` | Text per page. Encrypted files are quarantined. Pages with no paragraph breaks become one block per line. Scanned PDFs have no text and are flagged unreadable. Tables are recovered from layout-mode text (`readers.layout_tables`): column starts come from a header line, wrapped cells are joined (`Complete`/`d`, `2026-05-2`/`0`, `EX-26-0`/`1`), and sparse lines before the first row continue the header (`Register P99` / `(GBP m)`). Prose and page footers end a table. |
| md/txt | text | Paragraph blocks with line locators and `#` headings. Pipe tables become tables. |
| json | stdlib `json` | Arrays of objects become tables, with nested objects flattened to dotted columns (`citation.url`). NaN/Infinity are rejected. |
| eml | stdlib `email` | Subject, date and sender/recipient **domains only**. Text parts are read; attachments are listed by name, type and size, never opened. |
| html | stdlib `html.parser` | Text without script, style or template content. Tables become tables. |

Header detection scores the first 15 rows by header-synonym hits. It
recognises a two-row header (a merged or gapped group row over sub-headers,
e.g. "Recovery objectives / RTO"). It skips title banners and long notices.
Rows above the header are kept as a *preamble*, which feeds the document
context (see below).

Locators: `Sheet!A12` (and per-field cells such as `Sheet!E12`), `row 12`,
`table 2 row 5`, `slide 3 table 1 row 2`, `page 4 ¶2` / `page 4 line 9`,
`paragraph 17`, `line 40`, `$.items[3]`, `body 1 line 5`.

### Triage

Routes: `services`, `dependencies`, `mechanisms`, `controls`, `assumptions`,
`actions`, `exercises`, `observations`, `impact_assessments`,
`evidence_events`, `internal_loss_events`, `org_scenarios`,
`taxonomy_crosswalk`, `ignore`, `quarantine`.

- **Tables.** Each header is mapped one-to-one to fields with the synonym
  tables in `synonyms.py`: 3 points for an exact label, 2 for an exact
  group + label, 1 for a contained phrase of at least 4 characters (3 when it
  has a digit, such as `p99`). A contained phrase does not count when the
  header adds an attribute word (`rto`, `date`, `id`, `ref`, `owner`,
  `status`, `type` and similar), so "Dependency RTO" is not the dependency
  name. It also does not count when a decimal follows ("P99.9" is not
  "P99"). Labels
  longer than 60 characters are treated as content. A route qualifies only
  when every anchor group in `ANCHORS` is hit. For example, `services` needs a
  name and one of tier, tolerance, RTO or RPO, and `internal_loss_events` needs
  an amount and a reference or date. Field weights, value cues (Basel ET
  codes, URLs, RTO durations) and filename or title cues add to the score.
  The best route wins. If the runner-up is within 2 points, the route is marked
  `low` confidence; the LLM, when enabled, may pick only among those
  candidates.
- **Document context.** Key/value pairs from cover-sheet preambles, two-column
  tables and `Key: value` lines become `DocumentResult.context`. A BIA cover
  ("Business Impact Analysis | Retail Payments Hub") then supplies the service
  for a dependency or impact table that has no service column. A PIR header
  table supplies the incident date for findings. An impact-over-time table
  takes `observed_on` from the analysis's own assessment or approval date
  ("Approved on"). It takes `tolerance_reference` and an explicitly stated
  `basis` from a single-cell note row in the table ("Basis: assumed (…).
  Impact tolerance: 4 hours."). Every value used this way is recorded in
  `meta.derived_fields` and its cell in provenance.
- **Narrative.** Line patterns (`Action AF-1: … Owner: … Due: …`),
  post-incident cues, exercise and assumption mentions, and filename cues.
  Heading-sectioned documents whose sections carry `Key: value` lines (for
  example impact-tolerance statements) route as `target: "sections"` when at
  least two sections satisfy a contract's anchors.
- **Nothing matches:** `ignore` with flag `out_of_scope`, or `quarantine` when
  the content is personal data or unreadable.

Flags: `pii`, `injection`, `duplicate`, `superseded`, `unreadable`,
`out_of_scope`, `conflict` (set by the critic).

- **PII.** A column is personal when its header names personal data (name,
  email, phone, DOB, NI number, salary and similar) or when 30% or more of its
  values look like emails, phone numbers or national identifiers. Personal
  columns are excluded from extraction. A table that holds personal data and
  matches no contract quarantines the document. Contact strings in text are
  redacted before extraction or any model call; three or more raise the flag.
  Owner and role columns are accountability declarations, not PII.
- **Injection.** Patterns cover instruction text aimed at a model or reviewer:
  "ignore previous instructions", "you are an AI", "mark all … as validated",
  "auto-approve", "note to the AI", "bypass review", chat-template tokens and
  similar. The block or cell is flagged with an excerpt and removed from
  extraction and from the LLM payload. The rest of the document is still
  extracted. A flagged instruction never changes a value.
- **Superseded.** The same family name with a lower version (`_v3.1` vs
  `_v3.2`, or a `Version: x.y` line) is flagged `superseded by …`, routed
  `ignore` (the reason lists its content routes) and not extracted. Duplicates
  are handled the same way.

### Extraction

- **Tabular, deterministic.** Rows are assembled per contract
  (`extractors.assemble`), with these coercions:
  - Dates: ISO, `dd/mm/yyyy`, `dd.mm.yyyy`, `12 Mar 2025`, `March 12, 2025`,
    compact forms, and a date inside longer text. Day/month order comes from
    the column, then the row, then the table. Then comes a categorical column
    that splits a mixed-convention extract into groups (for example a source
    system: one writes 31/12, the other 12/31). That needs every group to be
    internally consistent, at least two groups to disagree, and no two
    qualifying columns to contradict each other on a row
    (`extractors.group_date_orders`). A date that is still ambiguous, or has no
    year ("w/c 19/05"), is kept raw, so validation rejects it visibly. The
    engine never guesses.
  - A planned exercise's date is kept as `meta.scheduled_on`, not
    `completed_on`.
  - `1-in-N` frequencies accept hyphens (`1-in-10`). `annual_frequency` is a
    derived field, so a frequency disagreement is reported once, on
    `frequency_text`.
  - Money: symbols and ISO codes, k/m/bn, accounting negatives, EU decimal
    commas. Currency and scale hints come from the header ("(GBP m)") and from
    `Units`/`Ccy` columns. Multiple recovery columns are summed (a derived
    field).
  - Tier and criticality: `Tier 1`, `T1`, gold → `critical`, and so on.
  - RTO and RPO: `4h` and `240 mins` → `RTO 4 hours (240 minutes)`.
  - Status vocabularies, including the leading segment ("Closed - near miss"
    → closed). Qualified results such as "Effective with exceptions" stay raw
    for the reviewer.
  - Lists separated by `;`, `|` or newlines; service mentions also split on
    commas.
  - `1-in-N` frequencies → `annual_frequency`.
  - BIA dependency tables: `Type` decides between supplier
    (`external_provider`) and internal service (`to_service_id`), and
    `Direction: Downstream` reverses the edge. An unknown type leaves the
    destination empty, which fails validation visibly.
  - Wide hierarchical taxonomies (`L1 Code | L1 Name | L2 Code …`) become one
    node per level with parent codes.
  - Single-cell note rows are skipped and counted in an info issue.
- **Narrative, deterministic.** Action lines, PIR key/value headers
  (observations), exercise reports (title, date held, inject and observation
  tables) and heading sections. Also:
  - Scenario decisions in minutes. A `Scenario 22 - Title` heading section
    stating "P90 raised from £30m to £34m", "P99 unchanged at £95m" or
    "Frequency 1-in-30" is staged with the decided value and a span. A ref
    found in the text is used only when its final number is the heading's
    scenario number; action ids are ignored.
  - `R-05 closure evidence: …` lines attach closure evidence to action R-05
    of the same document.
- **Narrative, LLM** (`llm_lane.py`, only with `use_llm=True`). Excluded
  blocks are removed and the text is chunked at about 6000 characters, with
  at most 12 chunks. The document is sent as quoted JSON data with a
  data-not-instructions system prompt. `chat_json` gets two attempts per
  chunk. Every field needs a quote. The quote must occur verbatim in the
  chunk (whitespace, quotes and dashes normalized) and must state the value:
  the value appears in the quote, or the parsed date or amount is equal. A
  field that fails is dropped with a `span_unverified` issue. An LLM-derived
  record is never `ready`; it is at best `needs_review`. Model errors become
  `llm_error` issues.

Provenance on every candidate:
`{file, locator, pod, method: deterministic|llm, spans: {field: quote}, cells: {field: locator}}`.
Merged records keep every provenance entry.

### Linking and validation

- **Services.** The largest service table seeds canonical names. Mentions
  resolve through these strategies in order: exact; exact without
  parentheticals; acronym (`CAP` → Card Authorisation Platform); declared
  parenthetical aliases (`Retail Payments Hub (RPH)`; geography such as
  `(UK)` is a qualifier, not an alias); normalized tokens (plural forms and
  abbreviations such as mgmt, ops, gw); in-order prefix (`Card Auth`); token
  subset (single generic tokens are refused); token Jaccard of at least 0.6.
  The next strategies run only when all of those fail, so they never change an
  earlier resolution:
  - *abbreviation*: each mention word maps in order onto the name, segment by
    `&`/`and`/`/` segment, as an equal token, a prefix either way (at least 3
    letters), or upper-case initials of that many tokens. 'FP Gateway' and
    'TF Docs' must cover a one-segment name completely. 'Fraud & TM' must
    cover each segment's start and at least one whole segment.
  - *segment acronym*: 'HVP' for 'CHAPS & High-Value Payments', at least 3
    letters.

  Mentions that still fail are retried as variants:
  - without a leading organization word. That word is learned from the batch,
    when most services' legal entities start with it ('ACME Mobile').
  - split at camelCase ('CollMan').
  - as `/` alternatives that must agree ('CLM/KYC').

  The first strategy with any hits decides. Exactly one hit resolves. Several
  hits make an `ambiguous_link` issue. No hit makes an `orphan_link` issue.
  Rows in one table are never merged with each other unless their names are
  identical.
- **Reference-keyed aliases.** Any table with exactly one exact service-ref
  header (`IBS Number`, `Service ID`, …) and one exact service-name header
  (`Service`) yields (ref, name) pairs, for example a processes sheet. The
  name becomes an alias only when the ref already identifies exactly one
  service. A name that already belongs to another service is raised as an
  issue instead. The resolution strategy is `reference`.
- **Vendors.** Legal suffixes, geography and entity words are stripped
  (`Ltd`, `plc`, `EMEA SARL`, `UK`, …). Dotted abbreviations collapse
  (`S.W.I.F.T.`, `L.P.`), `.com` is dropped, and trailing descriptors
  (`Bank`, `Solutions`, `Technologies`, `Business`, `LP`, `SC`) are dropped
  while a distinctive word remains. Groups merge on in-order prefix, first
  word, or acronym (`BT` / `British Telecommunications`), only when
  unambiguous. Two names that one register lists under different supplier ids
  are never merged (`Microsoft 365` and `Microsoft Ireland Operations` are
  different suppliers there). The most frequent spelling becomes canonical.
  Pure world-knowledge renames (Refinitiv → LSEG, Azure → Microsoft) are left
  to the LLM lane or the reviewer.
- **Scenarios.** Ref equivalence: 'SCN-22' ~ 'ACME-SCN-2025-022'. The shorter
  ref's letter groups appear in the longer one, its final number equals the
  longer one's final number, and its other numbers appear in it. A record with
  a global ref merges with the unique equivalent-ref record of another file.
  The abbreviated ref is kept as an alias, not a conflict, and several
  equivalents make an `ambiguous_reference` issue. Scenario mentions on
  losses, actions, assumptions, observations and exercises (a `Scenario`
  column) resolve to organization-scenario ids (`scenario_ids`) by ref,
  equivalent ref or exact title.
- **Ids.** `bulk-<kind>-<sha256(identity)[:12]>`. Identity is the
  organization reference when it is global (at least 5 characters with letters
  and digits; short refs such as `A1` are scoped to their file), otherwise a
  kind-specific key (name, title, `from|to|category`, URL, code). Re-runs
  produce the same ids.
- **Merge.** Records with the same identity across files merge with every
  provenance entry kept. These differences are *not* conflicts: name or title
  differences after a reference match (kept as aliases), derived fields,
  presentation rounding (£1.2m vs 1,234,567) and amounts in different
  currencies (a `currency_mismatch` warning). Quantities are compared on
  value. Tolerances are parsed into durations (`linker.duration_profile`):
  "RTO 4 hours; RPO 1 hour" vs "4 hours" compares the bare tolerance with the
  RTO, and free text that is not a plain duration falls back to text
  comparison. Frequencies compare on the annual rate, so '1-in-10' equals
  '1 in 10 years'. Any other differing value is a
  `conflict` error. The first source's value is kept until a reviewer decides,
  and the record becomes `needs_review`.
- **Validation.** Organization-context records, including the three agreed
  kinds, go through `context.validate_record` (the `RECORD_MODELS` pydantic
  models and business rules). Evidence needs a headline, a public URL, an ISO date and a summary. Errors are
  attached to the candidate (status `invalid`), never dropped. Nothing is
  fabricated to pass a rule. A control that reports "Effective" without a date
  or evidence stays invalid with the model's error. An instruction in a file
  cannot upgrade it.
- **Taxonomy crosswalk.** An organization node maps to an Atlas category by
  exact name (`exact`) or a `CATEGORY_RULES` keyword (`narrower` for level 2
  and below, `related` for level 1), with a rationale. `reviewer` is always
  empty. An unmatched node is left unmapped with a warning.

Candidate status: `ready` (valid, no conflict, unresolved link or LLM field),
`needs_review` or `invalid`.

## Contracts

Organization context, via the import gate (`contract_status: "existing"`):
`services`, `dependencies`, `mechanisms`, `controls`, `assumptions`,
`actions`, `exercises`, `observations`, `impact_assessments`.

Agreed on 2026-10-18 (`contract_status: "agreed"`). These are now
organization-context kinds in `context.RECORD_MODELS` (`OrgScenario`,
`InternalLossEvent`, `TaxonomyCrosswalk`) and go through the same handoff and
gate. `proposed_contracts.py` was removed.

| Contract | What bulk ingest fills |
|---|---|
| `internal_loss_events` | org_ref, title, basel_event_type, org_event_type_code, business_line, legal_entity, incident_ref, the three dates, gross_loss, recoveries, net_loss, potential_impact (near misses carry no booked loss), currency (original), status, near_miss, boundary, description, root_cause, service_ids, scenario_ids (from a scenario column). The reporting-currency block is never filled: no FX rate is invented. |
| `org_scenarios` | org_ref, title, narrative, owner, org_taxonomy_code, basel_event_type, frequency_text, annual_frequency, severity_p50/p90/p99 + severity_currency, last_workshop, status, service_ids. atlas_scenario_ids stays empty: crosswalk only, never merged. |
| `taxonomy_crosswalk` | org_code, org_label, level, parent_code, atlas_category, mapping_type, rationale; always `status: proposed`, `reviewer: ""`, no `reviewed_on` |

Organization-scenario ids are referenceable like Atlas scenario ids
(`scenario_ids` on exercises, actions, assumptions, observations and internal
losses), and the linker fills them where the source names a scenario.

Evidence: `evidence_events` are external public loss events. The citation is
kept. They enter the review queue as `pending`.

## Outputs

The default output directory is `<workspace>/generated/bulk_ingest/<batch_id>/`
(`generated/` is git-ignored). The directory is created 0700 and files are
written atomically with mode 0600. The output directory may not be inside the
ingest folder and may not contain the public stores.

- `batch.json` (`schema_version: 1`, `kind: atlas_bulk_ingest_batch`): batch
  id, status, the operator who prepared it, folder, settings, counts,
  manifest, per-document routes, flags and context, candidates, link
  registries (`links.services.entities/resolutions`, `links.vendors.mapping`),
  issues and the handoff index.
- `handoff/org_context_NNN.json`: exactly `{"records": [{"kind", "data"}]}`
  for `imports.preview_import(format="json")`. It contains valid records only,
  in `context.import_records` dependency order (services, mechanisms,
  org_scenarios, taxonomy_crosswalk, dependencies, …, internal_loss_events),
  split under 500 records and the 2 MB budget minus 64 KB.
- `handoff/evidence_research_material_NNN.txt`: blank-line-separated units,
  at most 100 units and 190k characters per file, each with headline, entity,
  date, summary and `Source: <url>`. `handoff/evidence_drafts.json` holds the
  same records shaped as pending evidence events (`review_status: pending`,
  `rights_review: pending`) for reference. There is no model-free path into
  the queue: `intake.prepare_intake` holds every candidate without the semantic
  judge. To queue them, run
  `web.intake_flow.prepare_research_material(material, settings=…, client=OMLXClient(settings))`
  and then `submit_draft_to_review(draft_id, selected_ids, settings=…)`.
- `handoff.proposed` is kept in the shape for older readers and is always
  `[]`. No `proposed_*.json` files are written any more.

## Submitting (the human gate)

```bash
python -m workbench_atlas.bulk_ingest submit <batch.json> --actor "Named reviewer" --select all
python -m workbench_atlas.bulk_ingest submit <batch.json> --actor "Named reviewer" --select bulk-svc-…,bulk-dep-…
```

`--actor` and `--select` are required. The plan (payload, kind, id, label,
warnings, skipped count) is printed before anything is written. A tty prompt
asks for `yes`; a non-interactive session needs `--yes`, otherwise nothing is
written.

- `all` means every `ready` organization-context record, closed under
  references: a ready record that points at an unselected batch record (for
  example a `needs_review` service) is skipped with the reason.
- Explicit ids must exist, be organization-context records and be valid.
  Otherwise the whole selection is rejected and nothing is written. A reference
  to an unselected batch record is a warning; the import gate then rejects that
  payload unless the target already exists.
- Each payload is previewed with the actor and the source
  `Atlas bulk ingest batch <id> (prepared by …)`. Only that payload's selected
  rows are confirmed. Each payload is atomic. The sequence is not: a failure
  reports how many earlier payloads were written. Before anything is written,
  selected ids that already exist in the workspace stop the submission
  (`already_exists`, nothing written).

Python: `plan_submission(batch, selection)` and
`submit_batch(settings, batch_path, selection=…, actor=…)`. Failures raise
`SubmitError` (a `ValueError`) with a typed `.code` from
`SUBMIT_ERROR_CODES`, plus `.written` (payloads already written) and `.ids`:

| Code | Meaning | Dashboard status |
|---|---|---|
| `not_a_batch`, `invalid_selection`, `actor_required`, `nothing_to_submit`, `preview_failed`, `import_rejected` | the request cannot be honoured as asked | 400 |
| `already_exists` | a selected id is already in the workspace; nothing written | 409 |
| `revision_required` | the workspace changed under the write (`ContextConflict`) | 409 |
| `stale_plan` | (adapter) the reviewed plan digest no longer matches | 409 |

`web.bulk_ingest_web.http_status` maps the codes, and the response carries
`{"error", "code"}`. Status is never inferred from message text.

## CLI

```bash
python -m workbench_atlas.bulk_ingest <folder> [--out DIR] [--llm] [--json] [--actor NAME] [--synthetic] [--workers N]
```

The command prints files by route and flag, records by contract and status,
and issues by severity. `--synthetic` stamps `classification: synthetic` (use
it for fictional packs). `--llm` enables the local OMLX lane; check
reachability first, because each failed call becomes an issue rather than an
abort.

Scoring against a pack answer key (only the scorer reads it):

```bash
python scripts/score_bulk_ingest.py <out>/batch.json examples/acme-bank/answer_key.json [--json]
```

The scorer reports route precision and recall per file, flag accuracy,
`expected_counts` deltas, alias accuracy for services, vendors and scenarios,
and conflicts and injections detected. A conflict is detected *leniently* when
the batch flags any disagreement between at least two of the listed files,
and *strictly* when every value the batch reports also corresponds to a key
value (same amount or duration, or contained text). The vendor score is also
given over the aliases that occur as a supplier name in the batch, because
aliases that appear only in prose cannot be resolved by name normalization.
With `--llm` batches it reports LLM records, fields kept and dropped
(span-verification drop rate), errors and malformed answers. The scorer
imports the engine's `name_key`/`vendor_key`, so a normalization change also
changes how the key is read. Compare runs scored with the same code.

## Integration (UI and job runner)

- Job entry point:
  `orchestrator.run_bulk_ingest_job(params, settings, progress=None, client=None, *, cancel=None) -> dict`.
  `params` is `{folder, actor, use_llm, classification}`; `cancel` is a
  `threading.Event` passed to `run_batch`. The dashboard runner uses it.
  `bulk_ingest.batches_dir(settings)` is the single source of the default
  staging directory (`<workspace>/generated/bulk_ingest`). It returns
  `BatchResult.summary()`:
  `{batch_id, status: completed|completed_with_issues|cancelled, out_dir, batch_path, counts, handoff}`.
  `completed_with_issues` means error-level *issues* exist (invalid records,
  conflicts). It is not a failed job.
- Runner phase: `bulk_ingest` (see Dashboard below). Check
  `omlx_reachable(settings)` before `use_llm=True`.
- Progress events are dicts with `stage` (inventory, read, triage, extract,
  link, critic, stage, batch), `status`, and per file `file`, `done` and
  `total`. Cancellation uses a `threading.Event`; a cancelled batch writes
  nothing.
- Review screen: read `batch.json`. Show `documents[].routes/flags/context`,
  `candidates[]` (status, errors, warnings, `meta.conflicts`,
  `meta.unresolved_links`, `meta.derived_fields`, provenance with spans and
  cells) and `issues[]`. Submit through `plan_submission` / `submit_batch`
  with the authenticated actor.

## Evaluation on the ACME Bank pack

Measured 2026-10-18 on `examples/acme-bank/pack` with
`scripts/score_bulk_ingest.py` against `answer_key.json`. The engine never
reads the key, and every change is generic: no pack names are in the code.

```bash
python -m workbench_atlas.bulk_ingest examples/acme-bank/pack --out "$OUT" --synthetic --actor "demo operator"
python scripts/score_bulk_ingest.py "$OUT/batch.json" examples/acme-bank/answer_key.json
```

### Deterministic lane (no LLM)

"Before" is the batch from the start of the day, scored twice: with the
original scorer, and with the current scorer and normalization (like for
like).

| Metric | Before (original scorer) | Before (current scorer) | After |
|---|---|---|---|
| Route precision / recall | 0.944 / 0.694 | 0.944 / 0.694 | 0.975 / 0.796 |
| Flag exact-match accuracy | 0.786 | 0.786 | 0.821 |
| Record-count total abs delta | 153 | 153 | 106 |
| Conflicts detected (lenient / strict) | 5 / 8 | 5 / 8 · 5 / 8 | 6 / 8 · 6 / 8 (8 raised, all real) |
| Injections detected | 1 / 1 | 1 / 1 | 1 / 1 |
| Alias accuracy: services | 0.602 | 0.602 | 0.795 |
| Alias accuracy: vendors | 0.490 | 0.583 | 0.594 |
| Vendors, aliases present as a supplier name (60 of 96) | – | 0.933 | 0.950 |
| Alias accuracy: scenarios | 1.0 | 1.0 | 1.0 |
| Ready org-context records imported through the real gate (scratch workspace) | 153 | – | 816 of 852 ready (36 dropped by reference closure) |
| Wall time | 1.2 s | – | 2.6 s (layout-mode PDF pass) |

What moved the numbers:
- PDF layout tables recover the self-assessment (mechanisms, exercises,
  vulnerabilities, assumptions, remediation), the audit's management actions
  and the ICAAP scenario table.
- Reference-keyed aliases from the IBS processes sheet, plus the abbreviation,
  segment-acronym, org-prefix, camelCase and slash strategies.
- The minutes scenario-decision pattern catches SCN-22 P90 (£30m register vs
  £34m minutes).
- Grouped date-order inference: internal-loss invalids fell from 38 to 11.
- Impact-assessment context (5/5 ready, was 5/5 invalid).
- The fixes behind the invalid counts, below.

Vendor gains are small because most missing vendor aliases ('Tessellate',
'Corvane CX', 'Quillfern', 'AWS') occur only in prose or loss descriptions,
never as a supplier name. The vendor-key change alone moves the old batch from
0.49 to 0.58 under the current scorer, since the scorer shares the
normalization.

**Invalid counts, classified** (after the changes):

| Kind | Invalid | Honest validation result | Was an extraction bug (fixed) |
|---|---|---|---|
| controls | 14 of 64 (was 17; needs_review 9, was 47) | 7 unresolved service names (world knowledge: 'Fraud Engine', 'Credit Cards Ops', 'XB Payments' …); 4 "Effective with exceptions" kept raw for the reviewer; 3 "Effective" with no evidence ref | All 47 needs_review came from unresolved failure-mode refs (FM-0x), now resolved to the self-assessment's mechanisms |
| impact_assessments | 0 of 5 (was 5) | – | `observed_on` and `tolerance_reference` were stated in the BIA (cover date, note row) but not read |
| exercises | 4 of 9 (was 1 of 1) | 3 completed exercises without injects/outcomes/evidence in the source; the tabletop report names no scenario (`scenario_ids` required) | Planned exercises had their scheduled date stored as `completed_on` ("completion in the future") |
| assumptions | 3 of 6 (was 2 of 2) | 2 workshop assumptions have no owner; A-02 status "Under review (INC-0107)" is not open/validated/invalidated | – |
| observations | 6 of 13 (new) | The self-assessment's vulnerabilities table has no per-row date (`observed_on` required) | – |
| actions | 6 of 34 | "Closed" without closure evidence | R-05's stated closure evidence line is now attached |
| dependencies | 2 of 127 (was 20) | ServiceNow row lists no supported service; 'Fraud Engine' target unresolved | "Dependency RTO" was mapped to the dependency name; alias misses |
| internal_loss_events | 11 of 498 (was 38) | 'w/c 19/05' dates without a year | Mixed UK/US day-month order across source systems |

### LLM lane

**Not run on 2026-10-18.** OMLX was reachable at `127.0.0.1:8000`.
`OMLXClient.list_models()` answered with the default key (no `OMLX_API_KEY`
in the environment; the YAML deliberately holds no key). But
`/api/status` reported `models_loaded: 0`. The id configured that day
was not among the server's models, and the server default was
`DeepSeek-V4.1-Flash-1017-MLX-Q9` (~296 GB, not loaded). The configured
id is now `DeepSeek-V4.1-Flash-1017-oQ4e-mtp`. A chat call would have made OMLX load a model, which
the operator had ruled out (one giant model at a time on this Mac). No LLM
numbers are recorded here. To measure once a model is loaded:

```bash
curl -s http://127.0.0.1:8000/api/status | python -m json.tool | grep -A3 loaded_models
OMLX_MODEL=<the loaded model id> python -m workbench_atlas.bulk_ingest examples/acme-bank/pack --llm --out "$OUT_LLM" --synthetic --actor "demo operator"
python scripts/score_bulk_ingest.py "$OUT_LLM/batch.json" examples/acme-bank/answer_key.json   # prints the LLM line
```

The LLM lane's guarantees are tested with fake clients, including one that
obeys and echoes an injection. Its fields are dropped as `span_unverified`,
nothing is validated, completed, closed or approved, and "all" never selects
an LLM-derived record.

## Known gaps

- No OCR (scanned PDFs and images), no legacy `.xls`/`.doc`/`.msg`, no
  archives.
- Narrative extraction without the LLM is limited to the deterministic
  patterns above. Still unrecalled on the ACME pack: the PIR prose (impact,
  internal loss, assumption A-02 "invalidated"), the audit's control results
  (KC-TPM-004 Effective → Ineffective, which is said with a pronoun), the
  annual report's observations and the vendor email's dependencies. The two
  remaining missed conflicts live there.
- The LLM lane runs only for routes triage assigned. A narrative document
  routed `ignore` (the vendor email) is not offered to the model.
- The LLM lane is tested with fake clients only. No real-model run is
  recorded yet (see Evaluation).
- Dependency type inference uses keywords (supplier, vendor, third, external,
  provider, outsourc; internal, service, application, system, platform).
- Service and vendor renames that need world knowledge (OLB = Online Banking,
  Azure = Microsoft, Refinitiv = LSEG) stay unresolved for the reviewer.
- The processes sheet's process ids and names are used for aliases only; they
  are not yet joined into `services.process_ids`.
- Submission across several payloads is not atomic as a whole.

## Dashboard

The **Bulk import** screen (`#bulk` in the dashboard rail) drives the same
pipeline as a job. Code: `workbench_atlas/web/bulk_ingest_web.py`, the
`bulk_ingest` phase in `web/runner.py`, routes in `web/server.py`, the
screen in `web/static/index.html` (between the `BULK IMPORT` markers). Tests:
`tests/test_web_bulk_ingest.py`.

- **Folders.** Only strict subfolders of `Settings.bulk_ingest_roots` are
  accepted. The default is `<workspace>/inbox` and the checkout's `examples/`.
  Override with YAML `bulk_ingest: {roots: [...]}` or
  `ATLAS_BULK_INGEST_ROOTS` (`os.pathsep`-separated); an empty list disables
  the screen. Paths with `..`, any symlink component, a public or private
  store inside, the staged-batch output, or an `answer_key.json` anywhere
  inside are refused. The dropdown lists subfolders up to two levels deep that
  hold files (`examples/acme-bank/pack` is listed, `examples/acme-bank` is
  not). The folder is validated when it is queued and again when the job runs.
- **Job.** `POST /api/run` with `{"phase": "bulk_ingest", folder, use_llm,
  classification, actor}`. "Use local LLM" is enabled only when `/api/omlx`
  reports the model reachable, and the job refuses `use_llm` if it is not.
  `GET /api/jobs/<id>` adds `progress` (`stage`, `file`, `done`, `total`)
  while the job runs. `POST /api/jobs/cancel` stops a running bulk ingest
  cooperatively; the job then completes with `result.status ==
  "cancelled"` and nothing is staged. Bulk ingest jobs are not pipeline runs.
- **Review.** `GET /api/bulk-ingest/folders` (folders plus recent batches) and
  `GET /api/bulk-ingest/batch?id=bulk-<12 hex>` (only `batch.json` under
  `<workspace>/generated/bulk_ingest/`, without the link registries, plus the
  `"all"` plan). The screen shows files by route and flag, injection and PII
  excerpts escaped, quarantined and ignored files with reasons, records per
  contract with ready / needs_review / invalid counts, and a filterable record
  table (source and locator, method, quoted spans, validation errors,
  conflicts with both values and files). Since the 2026-10-18 schema decision
  no record is `proposed`; scenarios, losses and crosswalk rows are selectable
  like other organization records. Evidence events are labelled as going to
  research review as pending; crosswalk rows are labelled as needing reviewer
  sign-off.
- **Commit.** Only ids in a handoff `org_context` payload are selectable (all
  ready, per contract, or individually). `POST /api/bulk-ingest/plan`
  `{batch_id, selection}` returns `plan_submission` output plus a
  `plan_digest` over the batch file, the plan, and the SHA-256 of each handoff
  payload that plan would import. Submit re-reads those payloads only as
  regular files inside the batch directory (no absolute paths, `..`, or
  symlinks) and returns 409 when the bytes no longer match the digest. `POST
  /api/bulk-ingest/submit` `{batch_id, selection, plan_digest, actor}` calls
  `submit_batch` only when the digest still matches (409 otherwise). The
  actor follows the existing endpoints: the typed operator name in local mode,
  the verified proxy subject in proxy mode. WRITE is a two-click in-page
  confirmation; any change to the selection, the name or the plan disarms it.
  Analysts and admins may use these endpoints in proxy mode.
