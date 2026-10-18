# Enterprise workbench

The dashboard connects the research catalog to an organization's own services,
dependencies, controls and decisions. Organization declarations live in a separate,
opt-in store. They are not public evidence and do not enter research-model requests.

## A practical first review

1. Open **Your organization** and activate the workspace. Enter the operator name
   recorded with changes. Activation updates the local non-secret configuration;
   an explicit deployment setting that disables organization context takes priority.
2. Add one critical service, its owner and its impact tolerance. Record the source
   or basis for that declaration. Map immediate service or supplier dependencies.
3. Declare the failure mechanisms the review will cover. Open a scenario and use
   **Map to a service** to record why it could affect that service. A proposed
   mapping remains distinct from a confirmed exposure.
4. Examine controls and assumptions. Validation needs supporting evidence; assign
   an owner and a due date to the follow-up actions. Closing an action requires
   closure evidence. Completed exercises need a date, injects, outcomes and evidence.
5. Record an assessment for each relevant service and mechanism. Save a baseline
   before the next review so changes to the organization assessment can be compared.

The coverage denominator is the organization's declared active services multiplied
by its declared active failure mechanisms. Cells remain **unassessed** until an
assessment is recorded. A confirmed exposure alone does not change assessment
coverage. **Tested** requires relevant, completed exercise evidence. When the
supporting exposure or exercise ceases to qualify, the current coverage view
downgrades the cell and flags the need for reassessment.

This measures the coverage of a declared assessment. It does not establish that all
possible risks have been identified, that controls are effective everywhere, or that
an enterprise has met a regulatory obligation. Scenario urgency remains a research
prioritization measure; public-evidence co-activation estimates do not become an
organization-specific incident probability.

## Working with the portfolio

The portfolio offers a table alongside visual exploration. Scenario comparison
supports two or three hypotheses, showing the mechanism, assumptions, evidence and
what would weaken each hypothesis. Saved views retain portfolio filters in the
current browser. Reading mode and motion preferences are also browser-local.

From a scenario brief, operators can export the scenario, map it to a service,
assign an action or plan an exercise. Organization forms show typed fields and
links to existing records. Updates and archives submit the version originally
opened; a conflicting update returns HTTP 409 rather than overwriting another
reviewer's work. Archiving retains history and may invalidate linked assessments.

## Taking material to a meeting or another platform

Use **Export brief** on a scenario, export the current filtered portfolio, or choose
the entire public research catalog. Preview the scope before downloading:

| Format | Intended use |
| --- | --- |
| PDF | Meeting brief or portfolio pack with evidence and provenance |
| Excel | Readable workbook with related tables and typed text cells |
| CSV ZIP | Separate normalized tables, relationships and a checksum manifest |
| JSON | Complete canonical structured snapshot with source and review history |

The browser receives an opaque token for an in-memory, frozen snapshot. Downloads
render that snapshot even if a source store changes afterward. A token expires
after 15 minutes or can be evicted when the bounded cache fills. The preview and
download are POST operations, and downloads are served with `Cache-Control: no-store`.
Choosing another output format does not rebuild or broaden the selection.

Organization context is excluded by default. Including it requires both an explicit
selection of scenarios and the **Include linked organization context** option.
Only the linked scope is included; an empty selection is rejected. Such packages
carry an organization-context classification. Inspect that scope before sharing a
file outside the workspace. See [exports.md](exports.md) for the canonical tables,
selection rules, checksums, spreadsheet protections and PDF font limitation.

## Bringing existing organization records

**Import records** accepts CSV or JSON up to 2 MB and 500 records. Choose the record
type, declare a meaningful source and map source columns to Atlas fields. Preview
each normalized candidate, inspect validation errors, select the intended records
and submit with the same operator identity used for the preview.

Previews do not write records. A submission validates relationships and versions
and commits its selected records atomically. Tokens are bound to the operator and
workspace, expire after 30 minutes, and cannot replay an already submitted row.
Imports create organization records; imported approval or server-managed history
fields cannot approve public evidence. This workflow does not import a canonical
public research export back into the public stores. See [imports.md](imports.md)
for mapping, stable identifiers, revision handling and supported file shapes.

## History, health and recovery

Organization changes preserve the actor, before/after values and record revision
in an append-only decision history. Baselines preserve a hashed snapshot for
comparison. A baseline is an assessment checkpoint, not a replacement for backup.

The dashboard distinguishes missing, unreadable and available research stores so
an unavailable store is not presented as a reassuring zero. The settings workbench
shows workspace health and persisted run state. Interrupted or failed runs require
a deliberate retry; queued work can be cancelled before execution starts. Backup
and restore remain operator procedures, with checksum verification and restoration
into a new directory before any live store is switched.

By default the dashboard binds to loopback and checks the request origin for
mutations. Operator names in this local mode are self-declared attribution, not
authenticated identities. Shared deployment requires the separate authenticated
proxy configuration documented in [shared-deployment.md](shared-deployment.md).
The project does not configure an identity provider on an operator's behalf.

## HTTP contract and verification

The interface uses `/api/exports/preview` and `/api/exports/download`,
`/api/context` and the `save`, `archive`, `baseline`, `compare`, `enable` routes,
plus `/api/imports/preview` and `/api/imports/submit`. Most request envelopes are
limited to 256,000 bytes. Import preview permits the larger artifact envelope,
then independently enforces the import module's 2 MB content limit.

`tests/test_web_enterprise.py` runs the actual dashboard handler against temporary
fictional stores. It checks frozen downloads in every format, selection closure,
private-context opt-in, origin and request limits, operator binding, atomic selected
imports, revision conflicts, baseline changes, coverage downgrades, configuration
activation, damaged-store reporting and locally served assets. Node syntax checks
the external workbench JavaScript when Node is installed. Browser layout and
keyboard behavior require separate visual verification; an HTTP test does not
establish that a rendered workflow is usable.
