# Private organization context

Organization context connects reviewed public scenarios with an operator's
declared services, dependencies, controls and follow-up work. It is an opt-in
local workspace. The module does not call an LLM, write public records or enter
the pipeline's configuration payload. It does not seed customer data or infer
facts about an organization.

## Enablement and storage

Explicit activation uses configuration. In a source checkout the dashboard
writes the gitignored overlay `workbench-atlas.local.yaml` and leaves the
tracked `workbench-atlas.yaml` unchanged. `ATLAS_CONFIG` and
`$ATLAS_HOME/config.yaml` are edited in place.

```yaml
organization_context:
  enabled: true
  db_path: organization-context.db
```

Precedence, highest first: explicit `Settings` arguments, environment
(`ATLAS_CONTEXT_ENABLED`, `ATLAS_CONTEXT_DB`), the local overlay, then the
active config file, then defaults. The overlay applies only when the active
file is the checkout `workbench-atlas.yaml`. `get_settings()` reads fresh
configuration, so an operator's activation takes effect without a restart.
Both settings are omitted from `Settings.to_safe_dict()`.

The context store must be distinct from scenario, evidence, risk and export
stores; identical paths and hard-link aliases are rejected. New database files
use owner-only permissions. Existing unrelated databases and unsupported schema
versions are rejected. Dashboard reads do not create or migrate a database.
Disabled workspaces return an empty disabled payload even if a store exists.

The SQLite schema has versioned current records, append-only decisions and
immutable baselines. A separate database is a data-flow boundary, not tenant
authentication or encryption. Protect the host and backups appropriately. An
actor identifies the operator's assertion; this module does not authenticate
that identity. The server is responsible for its access-control boundary.

## Record contracts

All records contain `classification` (`organization` or `synthetic`), a required
`source`, optional `source_refs` and `notes`. Source text is an operator-provided
provenance statement, not a claim that a cited file or URL was independently
retrieved or verified. Business fields are validated with Pydantic and reject
unknown fields.

| Kind | Required business fields | Relevant optional fields |
|---|---|---|
| services | name, owner, impact_tolerance | criticality, legal_entity, region, process_ids, process_names, supplier_external_ids |
| mechanisms | name, description | — |
| dependencies | name, from_service_id, category | exactly one of to_service_id/external_provider; common_cause, supplier_external_id |
| controls | name, owner, service_ids | mechanism_ids, validation_status, validated_on, evidence_refs, stale_after_days, valid_until |
| assumptions | statement, owner, expires_on, test | service_ids, scenario_ids, status, evidence_refs |
| exposures | scenario_id, service_id, rationale | mechanism_ids, status: proposed/confirmed/dismissed |
| actions | title, owner, due_date | service/scenario/record links, status, closure_evidence, decision_type, rationale, decision_evidence, acceptance_expires_on |
| exercises | title, scenario_ids, service_ids | injects, outcomes, status, completed_on, evidence_refs |
| assessments | service_id, mechanism_id, assessed_on | status: unassessed/mapped/tested, exposure_ids, exercise_ids, evidence_refs |
| observations | title, observed_on, description, evidence_refs | service_ids, mechanism_ids, scenario_ids |
| impact_assessments | title, service_id, observed_on, time_since_onset_minutes, tolerance_reference, at least one impact quantity | scenario_id, downtime_minutes, backlog, customers, capacity_loss, basis, evidence_refs, units, service_revision |
| evidence_assessments | evidence_id, owner, claim, rationale, relation, evidence_refs | scenario_ids, directness, source_family, incident_family, independence_status, independently_verified, verified_on, evidence_revision |
| saved_views | name, view_kind | filters, columns |
| org_scenarios | title | org_ref, narrative, owner, org_taxonomy_code, basel_event_type, frequency_text, annual_frequency, severity_p50/p90/p99 with severity_currency, last_workshop, status, service_ids, atlas_scenario_ids |
| internal_loss_events | org_ref | title, basel_event_type, org_event_type_code, business_line, legal_entity, occurrence/discovery/accounting dates, currency, gross_loss, recoveries, net_loss, potential_impact, reporting block, status, near_miss, boundary, description, root_cause, incident_ref, service_ids, scenario_ids |
| taxonomy_crosswalk | org_code, org_label | level, parent_code, atlas_category, driver_class, mapping_type, rationale, status: proposed/approved/rejected, reviewer, reviewed_on |

### Organization registers, losses and taxonomy

These three kinds hold what an organization already has before it adopts
Atlas, typically loaded by [bulk ingest](bulk-ingest.md).

- `org_scenarios` is the organization's own scenario register. Its ids can be
  used anywhere an Atlas scenario id is accepted (exposures, exercises,
  actions, assumptions, observations, losses, coverage and scoped exports).
  `atlas_scenario_ids` crosswalks an entry to Atlas scenarios. The records are
  never merged, and every crosswalk target must exist. Severity percentiles
  must be non-decreasing and need a currency. Archiving an entry makes it
  unreferenceable, and existing references are then reported as missing.
- `internal_loss_events` keeps amounts in their original currency. An
  optional reporting-currency amount is all-or-nothing:
  `reporting_currency`, `reporting_net_loss`, `fx_rate`, `fx_rate_date` and
  `fx_source` together. Totals across currencies are only meaningful when the
  conversion basis is declared. A near miss carries no booked loss; its
  estimate goes in `potential_impact`. Event dates cannot be in the future,
  and discovery cannot precede occurrence.
- `taxonomy_crosswalk` maps an organization taxonomy node to an Atlas risk
  category or event-driver class with a mapping type. A mapping starts as
  `proposed`. Approving or rejecting one needs a named reviewer and a review
  date. Only approved mappings should be relied on, and automated ingestion
  never approves them.

Returned records add stable `id`, `kind`, `schema_version`, integer `revision`,
`archived`, `created_at` and `updated_at`. Optional identifiers are generated on
creation; changing a title never changes identity. These server-owned envelope
fields can be round-tripped when editing, but cannot set revision, timestamps or
archive state. Saved views use `view_kind` to avoid colliding with envelope kind.

Context references must resolve to active records of the right kind. Scenario
references are checked against the scenario database, using a read-only
connection, and against active `org_scenarios` records. Scenario and context reads are separate transactions: public
scenario removal can happen after a save. The payload reports missing scenario
references and downgrades affected coverage instead of retaining a false badge.

### Explicit evidence challenge and source families

`evidence_assessments` records an owner's review of a particular claim and an
existing public evidence ID. `relation` is `corroborates`, `contradicts`,
`context_only` or `uncertain`. `directness` is `direct_observation`,
`secondary_report`, `inference` or `unknown`; it defaults to unknown.
Contradictory assessments coexist with supporting assessments instead of
silently replacing them. A source can support one claim and challenge another.

`source_family` and `incident_family` are optional operator-declared labels.
Matching labels group declarations; different labels do not demonstrate
independent sources or distinct incidents. `independence_status` is
`not_assessed`, `independent`, `shared_origin` or `uncertain` and defaults to
not assessed. The strict boolean `independently_verified` defaults to false.
Declaring independence requires explicit verification; a verified assessment
requires a non-future `verified_on`, an explicit independence status, named
owner, rationale and nonempty supporting `evidence_refs`. This records the
operator's assertion and basis. Atlas does not fetch those references or
independently certify the conclusion. Directness, evidence approval and source
independence are separate properties.

The required `evidence_id` must resolve in the read-only evidence store. Pending
evidence can be assessed without becoming approved. Each assessment pins an
`evidence_revision` hash at save time; a supplied stale hash is rejected.
Updates must supply the exact source revision the operator inspected; null
cannot silently adopt the newest source during re-review. A
later source edit leaves the historical assessment intact and marks its
current view as changed and needing reassessment. Missing and unreadable
sources are explicit. `context_payload` adds `effective_evidence_state`,
`current_evidence_review_status`, `effective_verification_status`, and
`current_evidence_revision` (derived, not stored in the assessment), plus
`evidence_assessment_diagnostics` with source/incident-family declaration
groups. These private declarations do not enter the public diagnostics,
generation pipeline or a default export, and they do not change scoring.

Optional `scenario_ids` makes the assessment relevant to an explicit scenario
scope. Private scoped exports include an assessment only when its nonempty
scenario scope is completely within the selected scenarios. An evidence-only
assessment or one also scoped to an unselected scenario is excluded. Exported
assessments retain their pinned revision and derived current-source state.

## APIs

```python
context_payload(settings)
validate_record(kind, data)
save_record(settings, kind, data, actor="operator-id", expected_revision=None)
archive_record(settings, kind, record_id, actor="operator-id", expected_revision=revision)
import_records(settings, entries, actor="operator-id")
create_baseline(settings, name, actor="operator-id", expected_revision=store_revision,
                scenario_ids=None)  # or a fixed list of watched scenario IDs
compare_baseline(settings, baseline_id)
export_context(settings, scenario_ids=["explicit-scenario-id"])
```

`validate_record` is pure syntactic normalization for form/import previews.
Relationships and current revisions are checked during submission. Import
entries have shape `{kind, data, expected_revision?}`; a batch accepts 1–500
entries, orders them by dependency kind, and either commits all entries and
decisions or none. Use explicit IDs when imported rows refer to one another.

New records omit `expected_revision` (or use zero). Updating or archiving a
record requires its exact current revision. Missing or stale revisions raise
`ContextConflict`; malformed data and references raise `ContextError` or a
Pydantic validation error, all subclasses of `ValueError`. There is no implicit
overwrite. Archiving a still-referenced record is rejected; first update or
archive its dependants.

Each mutation and its actor/before/after decision are committed in one SQLite
transaction. Decisions carry a linked hash sequence and cannot be updated or
deleted through normal SQLite writes. Baselines capture all current and
archived records with an integrity hash; subsequent comparison reports added,
changed and missing records. Archival appears as a change, preserving history.
Baseline `expected_revision` refers to the workspace decision sequence rather
than one record's revision. Creating a baseline never changes the records.

New baselines use snapshot format 2 and also capture a bounded public-research
projection. This is an observation of the local public stores, with no model,
network request, public-store write or schema migration. Organization record
schema version 1 and the context database schema remain unchanged. Existing
format 1 baselines retain their original bytes and integrity hash; their
research comparison reports `not_captured` instead of reconstructing history.

The default research scope follows all current scenarios, up to 2,000. It
reports newly added current scenarios at subsequent comparisons and retains
the baseline scenario IDs when they become inactive, so retirement appears as
a historical state change. Unrelated inactive scenarios are outside this
scope. Supplying `scenario_ids` creates a fixed watchlist of 1–2,000 existing
IDs, including explicitly selected historical scenarios. Linked evidence is
bounded at 20,000 records. Limits fail with an actionable error; capture never
silently truncates the selected portfolio.

The projection records scenario fields, citation and signpost references,
source identity/publication/retrieval/review metadata, content fingerprints,
and content hashes. Source summaries and research inferences are represented
by hashes. Every source link identifies its scenario, citation role and
current or historical scope. Signpost projections are explicitly labeled
reviewed, unreviewed or historical; a weakening signpost is not automatically
treated as confirmed counterevidence. This comparison does not evaluate
unpublished source content, independently count incidents or infer changes
that occurred between the two snapshots.

`compare_baseline(...)["research_changes"]` contains `scope`,
`baseline_as_of`, `current_as_of`, per-store `read_windows`, missing source IDs,
and separate `scenarios`/`evidence` differences. Each difference has `added`,
`changed`, `removed` and `counts`; changed rows include named `fields` with
their before/after values. Unchanged fields and read-time-only changes are
suppressed. The as-of values are observation cutoffs, not an event-time filter
or a reconstructed historical database. Public scenario, public evidence and
private context stores are read in separate windows, not one atomic snapshot.

An added source is newly linked or newly present in the selected scope. Its
publication date is separately labeled as preceding or following the baseline
cutoff, or unknown. Retrieval of an older article is not a new operational
incident. A removed source is absent from the current linked scope; this can
mean unlinking as well as record deletion. A readable store with missing
linked IDs reports those IDs. Missing/corrupt stores and malformed citation
lists produce an unavailable or not-captured comparison with null counts,
instead of fabricated removals or a reassuring zero. The overall comparison
is `partial` when only one store can be compared.

`context_payload` returns `records` and JSON `schemas` by kind, `coverage`,
`overdue`, recent `changes`, baseline metadata and `store_revision`. It does not
return private content through any default public export API.

## Coverage semantics

The denominator is the Cartesian product of **declared active services ×
declared active mechanisms**. No service or mechanism records means scope is
not declared and the fraction is null, not 100% or zero risk. Every declared
cell begins unassessed.

- **Unassessed:** no explicit assessment, or its current supporting mapping is
  missing, dismissed or refers to a missing scenario.
- **Mapped:** an assessment links at least one confirmed scenario exposure for
  exactly that service and mechanism.
- **Tested:** the mapping also links a completed exercise covering that service
  and a mapped scenario, with dated injects, outcomes and evidence; the
  assessment itself records evidence. Completion must precede the assessment.

The matrix does not claim exhaustive scenario coverage. One exercise does not
prove all failures for a cell are controlled. "Tested" means documented testing
occurred; it does not mean the service is safe, a control passed, or residual
risk is zero. A declaration whose supporting status later changes is
automatically shown as needing reassessment. Controls and observations alone
do not silently promote a cell to tested.

Closing an action requires closure evidence. Validated/failed controls require
a date and evidence. Completed exercises require date, injects, outcomes and
evidence. Resolved assumptions require evidence. Open overdue actions and
expired assumptions appear in the overdue list; expired previously validated
assumptions still need review.

## Explicit private portability

`export_context` is a read-only seam for an explicitly requested private-context
export. It requires nonempty selected scenario IDs and an enabled, existing,
readable context store; missing/corrupt stores are errors, not empty success.
The public export module must call this seam only after explicit inclusion.

The export includes directly scoped records, relevant services and controls,
service/exposure-linked actions and assumptions, and referenced supporting
records. Dependency expansion stops at immediate outgoing links rather than
walking the organization graph. Shared records whose scenario scope includes
unselected scenarios are omitted, including their prose. Saved views,
baselines and the complete decision history are excluded. The result is a
grouped context object with organization/synthetic classification preserved.

This is structured scope filtering, not semantic redaction of arbitrary prose.
An operator should review an explicitly requested private export before sharing
it. Public and private stores are read in separate snapshot windows; the export
manifest should disclose that boundary rather than claim a cross-store atomic
snapshot.

## Scope, impact observations and review expiry

Service legal entity, region, process IDs/names and supplier external IDs are
optional operator declarations. External identifiers are not assumed to resolve
to a connected authoritative inventory. The dependency's
`supplier_external_id` can carry the corresponding provider-register identifier.
Adding these fields does not rewrite existing service records or infer scope.

`impact_assessments` records impact at an explicit time since disruption onset.
Every row states a single `basis`: `assumed` or `measured`. Split mixed bases
into separate rows rather than presenting assumed values as measurements.
Measured rows require evidence and an observation date that is not in the
future. Fields are:

- `time_since_onset_minutes` and optional `downtime_minutes`, in minutes;
- optional `backlog`, with a declared unit (default items);
- optional `customers`, a whole-number count;
- optional `capacity_loss`, a percentage between 0 and 100.

All numeric values must be finite and non-negative. At least one impact
quantity must be supplied. Missing quantities remain null; an explicit zero is
retained as zero. Units are recorded and validated. These are contextual impact
observations or assumptions, not calibrated forecasts or financial-loss fits.

Each impact record requires `tolerance_reference`, a source/policy/declaration
reference for the tolerance being considered. Its service revision is pinned
transactionally at save time. A supplied stale `service_revision` is rejected;
later tolerance or service-scope changes do not silently reinterpret the stored
observation. The system does not parse free-text tolerance into a numeric
pass/fail threshold or claim that a tolerance breach has been verified.

Controls may declare `valid_until` and/or `stale_after_days` from the validation
date. `context_payload` returns the derived `effective_validation_status`
(`untested`, `validated`, `failed` or `stale`) and `validation_stale_on`, plus a
`control_diagnostics` summary. The earlier applicable expiry is used. Historical
`validation_status`, evidence, revision and decision records remain unchanged.
An omitted validity rule is reported as undeclared; it is not indefinite
assurance. Stale control validation is separate from a coverage cell's record
that an exercise took place.

Actions distinguish `investigate`, `test`, `remediate` and `accept_risk` through
`decision_type`. Risk acceptance requires an owner, nonempty `rationale`,
`decision_evidence` and `acceptance_expires_on`. Its decision evidence can also
support closing the action. Acceptance expiry produces an `expired`
`effective_acceptance_status` and an overdue review item even if the workflow
action remains historically closed. No new acceptance or reversal decision is
made automatically. Expiry is a request for an accountable review, not a
silent extension of an earlier approval.
