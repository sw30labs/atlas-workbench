# MITRE ATLAS Minimum Integration — Implementation Plan

## Document control

| Field | Value |
|---|---|
| Document owner | Contingency Atlas maintainer |
| Status | Implemented |
| Version | 1.1 |
| Effective date | 2026-08-12 |
| Last reviewed | 2026-08-12 |
| Next review | At the next MITRE ATLAS upgrade or 2026-09-12, whichever comes first |
| Change authority | Project maintainer |
| Supersedes | None |

## Decision

I will add MITRE ATLAS as a versioned, human-reviewed, display-only overlay on
Contingency Atlas scenarios. I will not replace the project-native banking
operational-risk taxonomy, alter scores, or claim that a hypothesized technique
was observed in an incident.

I consider the minimum integration complete when I can:

1. Import one pinned official MITRE ATLAS v6 YAML release from a local file.
2. Search imported techniques from a scenario.
3. Create a pending scenario-to-technique applicability proposal.
4. Approve or reject the proposal through the existing Review queue.
5. Display approved techniques, tactics, mitigations, platforms, maturity, and
   related case studies in the Atlas Brief.
6. Trace every mapping to its source release, source hash, scenario snapshot,
   rationale, reviewer, and decision.
7. Activate a newer ATLAS catalog without silently rewriting old mappings.

## Architectural boundary

```text
Contingency Atlas scenario
        |
        | reviewed applicability mapping
        v
MITRE ATLAS technique
        +-- achieves --> tactic
        +-- mitigated by --> mitigation
        +-- employed by --> case study
```

I will preserve these semantic boundaries:

- `atlas-1.0` continues to identify the Contingency Atlas banking-risk
  taxonomy.
- `mitre-atlas/<content-version>` identifies the external framework release.
- `signal_maturity` remains the maturity of a banking weak signal.
- `technique_maturity` remains MITRE's feasible/demonstrated/realized label.
- Approved mappings remain advisory context and never become scoring inputs.

## Upstream data policy

I will consume the official MITRE ATLAS v6 YAML distribution rather than
scraping the website or hand-maintaining a technique list.

- I will require an explicit local input file.
- I will pin and record the content version, format version,
  operator-asserted source URL, and SHA-256 of the exact input bytes. I will
  record but never fetch or authenticate that URL.
- I will support an independently supplied expected SHA-256 and reject a
  mismatch before parsing.
- I will never download framework data during application startup.
- I will not commit the full upstream dataset in the minimum release.
- I will retain the upstream copyright, license, trademark, and
  non-endorsement notices.
- I will reject a changed payload that reuses an already imported content
  version.

## Storage contracts

### Framework releases

I will add a `framework_releases` table with:

- `framework`
- `content_version`
- `format_version`
- `release_date`
- `source_url`
- `source_sha256`
- `imported_at`
- `is_active`
- `is_sealed`

The `(framework, content_version)` pair will be unique. Only one release per
framework may be active. Activating a release will retain all older releases.
Each successful import will be sealed before activation, and the database will
reject later object or relationship appends to that snapshot.

### Framework objects

I will add a `framework_objects` table with:

- framework and content-version coordinates
- stable upstream object ID
- object type
- name and description
- technique maturity
- platforms as canonical JSON
- deterministic object hash
- canonical upstream payload as JSON

The minimum importer will accept tactics, techniques, mitigations, and case
studies. Keeping the canonical payload lets me retain upstream fields without
forcing a schema migration for each upstream addition.

### Framework relationships

I will add a `framework_relationships` table with:

- framework and content-version coordinates
- deterministic relationship key
- source object ID
- relationship type
- target object ID
- canonical relationship metadata

The minimum will retain `sequences`, `achieves`, `specializes`, `mitigates`,
and `employs` relationships. The importer will reject dangling references.

### Mapping proposals

I will add a `framework_mapping_proposals` table with:

- proposal ID
- scenario ID, scenario content hash, and monotonic scenario revision
- framework and pinned content version
- technique ID
- applicability (`hypothesized` in the minimum)
- required rationale
- evidence references as canonical JSON
- origin (`manual` in the minimum)
- status, reviewer, reason, decision time, and creation time

I will keep claim fields append-only. Decision functions may update only the
decision fields. I will suppress duplicate pending proposals while preserving
the ability to re-propose after a decision.

### Scenario revision registry

I will add `scenario_revision` to scenario rows and a
`scenario_mapping_revisions` registry with scenario ID, monotonic revision,
and change time. Insert, meaning-bearing update, and delete triggers will
advance the registry. This makes an approval stale after restoration or
delete/recreate even when the scenario text and content hash later match.

## Importer and CLI

I will add a focused framework package and an `atlas-framework` CLI with:

```text
atlas-framework mitre-import --input PATH --source-url URL \
  [--expected-sha256 SHA256] [--activate]
atlas-framework status
atlas-framework activate VERSION
```

The importer will:

1. Enforce an input-size limit before parsing.
2. Parse with a bounded `yaml.SafeLoader` subclass that rejects duplicate keys,
   recursive aliases, and excessive alias, node, or nesting counts.
3. Require the MITRE ATLAS v6 top-level structure.
4. Keep content and format versions separate.
5. Validate MITRE identifiers (`AML.TA####`, `AML.T####`,
   `AML.T####.###`, `AML.M####`, and `AML.CS####`).
6. Validate object types, maturity values, and platforms without inventing
   replacements.
7. Normalize objects and relationships deterministically.
8. Reject dangling or malformed relationships.
9. Calculate source and object hashes.
10. Seal the imported snapshot against later object or relationship appends.
11. Perform import and optional activation in one SQLite transaction.
12. Return a machine-readable count and provenance summary.

## Mapping and review workflow

I will make the first mapping workflow manual and explicit.

1. A reviewer opens a scenario and searches the active technique catalog.
2. The reviewer selects a technique and writes a required rationale.
3. The application verifies the scenario, release, and technique.
4. The application records a pending proposal, a hash of the scenario's
   meaning-bearing fields, and its monotonic scenario revision.
5. The Review queue displays the proposal with technique context and derived
   tactics, mitigations, and case studies.
6. Approval or rejection requires reviewer identity and a written reason.
7. Only approved mappings appear in scenario output.

The scenario hash will cover title, description, core risks, transmission
mechanism, and affected capabilities. Currency will require both the hash and
monotonic revision to match. If any meaning-bearing field changes, or the row
is deleted and recreated, I will retain the approved mapping as history but
display it as stale and exclude it from current coverage until it is reviewed
again. Restoring identical text will not reactivate the old approval.

## API

I will add:

```text
GET  /api/frameworks/mitre-atlas/status
GET  /api/frameworks/mitre-atlas/techniques
POST /api/frameworks/mitre-atlas/proposals
```

Technique search will support bounded text, platform, maturity, and result
limit parameters. Proposal creation will reuse the dashboard's loopback and
Origin mutation protections. I will extend `/api/review/decide` with a
`mitre_atlas` review kind and enrich `/api/data` only with approved mappings.

## Dashboard

I will extend the existing scenario modal and Atlas Brief rather than add a new
top-level product area.

- The scenario modal will provide a small "Propose MITRE ATLAS mapping" flow.
- The Review queue will support MITRE ATLAS mapping proposals.
- The Atlas Brief will show a full-width MITRE ATLAS overlay card.
- Every card will state that applicability is hypothesized, not observed
  attribution.
- Related ATLAS case studies will be examples associated with a technique, not
  evidence for the scenario.
- Upstream content will always be escaped before rendering.

## Governance and ontology

I will extend the existing rule that machinery proposes and humans dispose.

- A mapping proposal cannot self-apply.
- A decided proposal must carry reviewer identity and reason.
- A technique must resolve in the proposal's pinned release.
- The proposal remains tied to its original scenario hash.
- Approval cannot mutate scenarios, scores, framework objects, or framework
  relationships.

I will record the architectural decision in an ADR and extend the ontology
adapter only as far as needed to preserve the existing proposal-state
tripwire. SQLite reference checks remain authoritative.

## Verification

### Import verification

- Valid synthetic v6 import.
- Idempotent migration and import.
- Same version with a different hash fails closed.
- Invalid IDs and object types fail closed.
- Dangling relationships fail closed.
- Oversized input fails before parsing.
- Partial failure rolls back completely.
- New activation preserves old releases.

### Mapping verification

- Scenario and technique must exist.
- Tactics and mitigations cannot be selected as techniques.
- Duplicate pending proposals are suppressed.
- A decision cannot be applied twice.
- Only decision columns change on approval or rejection.
- Mapping release coordinates never drift.
- Scenario edits make mappings stale.
- Approval never changes a scenario or scoring table.

### API and UI verification

- Search is bounded and correctly filtered.
- Missing catalog has a clear response.
- Remote mutation requests are rejected.
- Pending proposals enter Review.
- Reviewer and reason are required.
- Only approved mappings enter `/api/data`.
- Upstream descriptions are escaped.
- Existing dashboard behavior remains intact without a catalog.

### Regression invariants

- Existing urgency scores remain unchanged.
- Phase generation does not acquire MITRE fields.
- MITRE content does not enter LLM prompts.
- The pipeline operates normally without an imported framework.
- The complete existing test suite stays green.

## Delivery checkpoints

### Checkpoint 1 — controlled plan

I will commit this plan before implementation.

### Checkpoint 2 — catalog foundation

I will deliver migrations, importer, CLI, provenance reporting, attribution,
and focused importer tests.

### Checkpoint 3 — governed mappings

I will deliver proposal storage, review decisions, staleness, relationship
resolution, and governance tests.

### Checkpoint 4 — API and dashboard

I will deliver search, proposal, Review queue, approved scenario output, and
the Atlas Brief UI.

### Checkpoint 5 — controlled release

I will add the root product roadmap, ADR, taxonomy clarification, security and
licensing documentation, run focused and full verification, and audit the
final diff.

## Explicit exclusions

I will not include these in the minimum:

- automatic framework downloads or scheduled refreshes;
- MITRE ATT&CK integration;
- NIST AI RMF crosswalks;
- LLM-generated or keyword-generated mappings;
- automatic incident attribution;
- a Navigator or matrix clone;
- technique/control coverage scoring;
- control-effectiveness scoring;
- changes to likelihood, impact, urgency, or compound scoring;
- automatic scenario generation from each technique; or
- use of related ATLAS case studies as scenario evidence.

## Change history

| Version | Date | Author | Change |
|---|---|---|---|
| 1.1 | 2026-08-12 | Contingency Atlas maintainer | I recorded the implemented digest pin, sealed release snapshot, monotonic scenario revision, and restoration-safe staleness controls. |
| 1.0 | 2026-08-12 | Contingency Atlas maintainer | I approved the minimum MITRE ATLAS integration plan for implementation. |
