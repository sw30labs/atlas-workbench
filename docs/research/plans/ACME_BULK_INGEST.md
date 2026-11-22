# Bulk ingest pods and the ACME Bank information pack

Status: draft, 2026-10-18. Branch `feat/acme-bulk-ingest`.

## Problem

Onboarding an organization today means typing every service, dependency,
mechanism and scenario by hand, one form at a time. Real organizations do not
start from scratch. They already hold service catalogues, business impact
analyses, vendor registers, scenario registers, risk taxonomies, internal loss
data, incident post-mortems, audit findings and board reports, in many formats
and with their own vocabulary.

## Goal

An operator points Atlas at a folder. A pod of agents inventories it, decides
what each file is, extracts candidate records, maps them onto the agreed
contracts, links them to each other, and stages one reviewable batch. The human
gate stays. Nothing is approved automatically, and it takes a few decisions per
batch rather than one decision per field.

## Non-negotiables

- Staging only. Organization context still goes through
  `imports.preview_import` / `submit_import`. External evidence still lands as
  `pending` in the existing review queue. Pods never approve, attest or
  publish anything.
- File contents are data. A document that says "ignore your instructions" or
  "auto-approve" is flagged and shown to the reviewer. It is never obeyed.
- Every staged record carries provenance: source file, sheet or page or
  paragraph locator, the extracting pod, the method (`deterministic` or
  `llm`), and a quoted source span for each LLM-extracted field.
- Deterministic first. Tabular files are mapped by header synonyms and value
  rules. The LLM lane (local OMLX through `workbench_atlas.llm`) is optional.
  It handles narrative documents and unmapped columns, and the full flow runs
  offline without it, so the tests do too.
- No new runtime dependencies. Readers use what the base install already has:
  `openpyxl`, `python-docx`, `python-pptx`, `pypdf`, the stdlib `csv`,
  `json`, `email` and `html.parser` modules, plus MarkItDown when it is
  installed.
- Fail closed and say why. Unsupported, oversized, corrupt, encrypted or
  duplicate files are listed with a reason and never silently dropped.

## Pods

| Pod | Input → output | Lane |
|---|---|---|
| Inventory | folder → file manifest (sha256, size, format sniff, duplicates, limits) | deterministic |
| Reader | file → normalized `Document` (tables with sheet and row locators, text blocks with page or paragraph locators, metadata) | deterministic |
| Triage | `Document` → contract routes with scores and reasons; flags (PII, injection-like text, out-of-scope) | deterministic rules, then LLM tiebreak |
| Extractor (one per contract family) | routed `Document` → candidate records in contract shape plus a field-level provenance map | tabular: deterministic; narrative: LLM with quoted spans |
| Linker / normalizer | candidates → resolved cross-references (service names to ids, vendor names to providers), taxonomy crosswalk, dedup across files, contract validation via `context.RECORD_MODELS` | deterministic |
| Critic | staged batch → issues: span fidelity, conflicting values across files, orphan links, low-confidence routes | deterministic, plus optional LLM |
| Stager | batch → `batch.json` plus the handoff payloads (mixed `{kind,data}` import JSON, evidence intake units) | deterministic |

The pods run with bounded concurrency inside one batch job, use the
JobJournal, and can be cancelled and retried.

## Contract routes

Existing contracts (no schema change):

- `services`, `dependencies`, `mechanisms`, `controls`, `assumptions`,
  `actions`, `exercises`, `observations`, `impact_assessments` (organization
  context, via the import gate)
- `evidence_events` (external public loss events, as `pending` research
  intake with the citation kept)

Proposed contracts. **Gate: the schemas need agreement before they get
tables.** Until then pods stage these records as typed JSON marked
`contract_status: "proposed"`:

- `internal_loss_events`: the organization's own loss and near-miss records
  (Basel event type and business line, occurrence / discovery / accounting
  dates, gross loss, recoveries, net loss, currency, status, root cause,
  linked services)
- `org_scenarios`: the organization's existing scenario register (its
  reference, title, narrative, owner, its own taxonomy code, frequency and
  severity estimates, last workshop date, linked services), crosswalked to,
  never merged into, Atlas-generated scenarios
- `taxonomy_crosswalk`: organization taxonomy node → Atlas category or
  driver class, with the mapping type (`exact`, `broader`, `narrower`,
  `related`), the rationale and the reviewer

Also: `ignore` (out of scope, with a reason) and `quarantine` (unreadable,
or flagged for human attention).

## ACME Bank information pack

`examples/acme-bank/pack/` is what the operator ingests.
`examples/acme-bank/answer_key.json` is the ground truth used to score the
pods (route accuracy, record recall, link accuracy). Pods must never read the
answer key. Everything about ACME is synthetic and labelled as such. ACME is
a fictional bank (profile from the legacy repo's
`docs/business/acme-bank-pitch.md`: about 40 markets, a ServiceNow GRC
estate, around 30 board-visible scenarios refreshed annually).

Data reuse from the legacy `workbench-atlas` repo:

- External public loss events: approved `public_primary` and
  `public_secondary` records only, with their citations and independently
  authored summaries. No source text. **Not rebranded:** they are real
  events about real organizations, and relabelling them as ACME's would
  fabricate facts. They appear as ACME's "external loss watchlist" export.
- Scenarios, emerging risks and taxonomy: the legacy scenarios are rewritten
  into ACME's own scenario-register format and vocabulary. ACME is fictional,
  so this is synthetic organization data inspired by public-evidence-grounded
  scenarios.
- Internal losses, services, vendors, controls and reports are generated
  synthetic data, made consistent with each other (the same services,
  vendors and incidents appear across files, under the naming drift real
  estates have).

Realism, deliberately included: mixed date formats and currencies, merged
header rows, a scenario register with ACME-specific columns, renamed and
abbreviated service names across files, a duplicate file, a superseded
version, an irrelevant file, a corrupt file, a file with synthetic personal
data to flag, and a document containing injection-style text.
