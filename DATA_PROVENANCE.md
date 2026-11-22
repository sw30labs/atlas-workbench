# Data Provenance Policy

Effective: 28 July 2026

Provenance is a release requirement, not optional metadata. A reviewer must be
able to determine what a record says, where its factual basis came from, which
parts are researcher inference, and whether the material can be shared.

This policy applies to research events, emerging-risk signals, scenarios,
fixtures, exports, screenshots, and any other data-bearing artifact proposed
for inclusion in the repository.

## Public-edition boundary

The public edition is built from:

1. independently researched, publicly accessible sources with record-level
   citations; and
2. explicitly labeled synthetic fixtures created for testing or demonstration.

It excludes:

- employer, client, customer, counterparty, or employee material;
- confidential, personal, restricted, or non-public information;
- subscription or member-only content;
- copied third-party summaries or substantial source text;
- credentials, private endpoints, hostnames, usernames, filesystem paths, or
  infrastructure details;
- records whose origin or reuse status cannot be established; and
- generated claims presented as observed facts.

The repository contains no ORX member, subscription, loss-event, news,
scenario, taxonomy, identifier, schema, or other proprietary content. ORX
material must not be submitted to this project.

## Allowed provenance classes

Every record must have exactly one provenance class.

| Class | Meaning | Release requirement |
|---|---|---|
| `public_primary` | An official public record from a regulator, court, government body, company, standards body, or other direct issuer | Full citation, independent summary, rights/terms check, human review |
| `public_secondary` | Public reporting or analysis by a publisher that is not the direct issuer | Full citation, independent summary, primary-source search noted, rights/terms check, human review |
| `synthetic` | A deliberately fictional record made for testing or demonstration | Prominent synthetic label, no real-party allegation, generation method recorded |

`model_generated` is not an evidence class. Model output may help draft a
scenario or summary, but the underlying evidence must still be public and
cited or the record must be labeled synthetic.

A scout `source_proposal` is also not an evidence class or an approved record.
It is a bounded lead containing a registry entry, retrieval metadata, and a
public URL for a researcher to inspect. Scout output must complete the normal
acquire, record, summarize, classify, and review process before it can become
event evidence or support an emerging risk.

## Required metadata

Each public-source record must carry, at minimum:

| Field | Requirement |
|---|---|
| `event_id` | Project-native stable event identifier |
| `record_type` | Event, signal, scenario evidence, or another documented type |
| `provenance_class` | `public_primary` or `public_secondary` |
| `source_publisher` | Organization responsible for the source |
| `source_title` | Exact source-page or document title |
| `source_url` | Canonical public URL, not a search-result URL |
| `source_date` | Required source publication date in `YYYY-MM-DD` form |
| `retrieved_at` | UTC date or timestamp of access |
| `source_type` | Regulator release, filing, court record, advisory, news report, and so on |
| `instrument_status` | For `regulatory_release`: `proposed`, `enacted`, `enforcement`, or `vacated`. Required before approval. Empty on other source types. |
| `source_locator` | Page, section, paragraph, release number, or docket when useful |
| `independently_authored_summary` | Original, factual, non-substitutive summary |
| `research_inference` | Any interpretation, explicitly separated from sourced fact |
| `rights_review` | Result of access, reuse, and redistribution check |
| `content_fingerprint` | Hash or equivalent integrity marker where practical |
| `review_status` | `pending`, `approved`, `rejected`, or `needs_update` |
| `reviewed_at` | UTC review date or timestamp |
| `taxonomy_version` | Version of the independent project taxonomy used |

Synthetic records must instead include:

| Field | Requirement |
|---|---|
| `record_id` | Project-native stable identifier |
| `provenance_class` | `synthetic` |
| `synthetic_notice` | Clear statement that the record is fictional |
| `generator` | Human, script, model, or mixed method |
| `generator_version` | Script, template, or model version where applicable |
| `seed` | Reproduction seed when applicable |
| `created_at` | UTC creation date or timestamp |
| `review_status` | Confirmation that it does not allege a real-party event |

The bundled demonstration corpus records shared generation and review metadata
in [`examples/fixture_manifest.json`](examples/fixture_manifest.json), with
per-record fictional notices retained in each fixture file.

Additional metadata is encouraged. Required fields may be represented in a
normalized related table rather than duplicated, provided the relationship is
unambiguous and preserved in exports.

## Research workflow

### 1. Acquire

- Prefer primary public records.
- Confirm that the source is genuinely public and accessible without an
  employer account, paid subscription, member login, or bypass.
- Read and respect source terms, robots rules, copyright notices, and database
  rights.
- Do not retain more source text than is necessary for verification.
- Treat optional scout output as unverified leads. A successful HTTP response,
  parsed feed item, or registry inclusion does not establish accuracy, rights,
  relevance, independence, or approval.

### 2. Record

- Capture the canonical URL and bibliographic metadata when the source is
  reviewed.
- Store a fingerprint or archival locator when lawful and practical.
- Use a project-native identifier. Do not import another provider’s proprietary
  identifier or numbering convention.

### 3. Summarize

- Write an independent factual summary.
- Use short quotations only when necessary, clearly marked, and within
  applicable quotation and source-license limits.
- Do not copy headlines, abstracts, tables, taxonomies, or database records in
  bulk.
- Separate reported facts from researcher interpretation.

### 4. Classify

- Apply only the independently documented Atlas Workbench taxonomy or a
  public standard whose source and terms are recorded.
- Record the taxonomy version.
- Do not infer that resemblance to an external taxonomy grants permission to
  copy it.

### 5. Generate

- Pass only authorized source summaries or synthetic inputs to a model.
- Preserve the evidence identifiers used for each scenario.
- Treat generated claims as hypotheses until independently verified.
- Never cite a model output as the factual source for an event.

### 6. Review

A human reviewer must verify:

- citation accuracy and accessibility;
- fidelity of the summary to the source;
- separation of fact and inference;
- absence of restricted, personal, employer, client, and subscription data;
- correct synthetic labeling;
- neutral taxonomy and naming; and
- appropriateness for public release.

New public-source events and model-derived emerging risks remain `pending`.
Phase 3 scenario generation and Phase 5 evidence analysis exclude pending,
rejected, and `needs_update` records. The bundled synthetic fixtures use the
separate `approved_synthetic` status.

Phase 3 does not accept an ad hoc raw-signal packet. New source material must
first enter Phase 1 or Phase 2 and complete review before a later generation
run can use it.

The optional Singularity Atlas pull is an adjacent local graph, not a
Atlas Workbench store. It may contribute public-feed story text and URLs
into a draft-only intake prepare when the operator opts in. Loop archive /
Innermost Loop editions are excluded (all-rights-reserved). A missing Neo4j
driver or an unreachable database is a skip, not a write. Selected stories
still enter Review as `pending` with ordinary public-source and rights
attestations; the graph itself is never treated as an approved source.

The optional scout remains outside the eight-phase pipeline. It must not insert
into either runtime database, update the emerging-risk catalog, invoke a model,
or set `review_status`. Network retrieval requires explicit operator opt-in and
produces only proposal artifacts for subsequent independent research.

An emerging risk inherits record-level provenance only through fully approved
public evidence records. During risk approval, each `evidence_refs` value must
match an approved event ID or the exact source URL of an approved event; the
workflow then stores the canonical project event IDs on the risk. A bare public
URL is not sufficient for approval.

Use the review helper to inspect and make an explicit decision:

```bash
python scripts/review_records.py list
python scripts/review_records.py approve-event EVENT_ID \
  --confirm-public-source --confirm-rights
python scripts/review_records.py reject-event EVENT_ID --reason "Citation mismatch"
python scripts/review_records.py approve-risk "Risk issue" \
  --confirm-public-source --confirm-rights
python scripts/review_records.py reject-risk "Risk issue" --reason "Insufficient evidence"
python scripts/review_records.py list --kind signpost
python scripts/review_records.py approve-signpost SCENARIO_ID
python scripts/review_records.py reject-signpost SCENARIO_ID \
  --reason "Assessment overstates the cited evidence"
```

Approval confirmation flags record a reviewer’s attestation; they do not fetch,
license, verify, or legalize a source automatically. Review the source itself
before using them.

Signpost review accepts or rejects a model-assisted interpretation of already
approved evidence. It does not alter evidence approval or scenario lifecycle
state. Approval resolves every considered reference against the current
approved evidence database and verifies that the scenario projection still
matches the latest pending immutable `signpost_assessments` snapshot. Review
decisions update that history row and the projection marker atomically.

## Employer and institution safeguards

No contribution may derive from the author’s employer systems, communications,
reports, methodologies, client relationships, credentials, or non-public
knowledge. Replacing an institution’s name with a generic label does not
sanitize an internally derived claim.

Real institutions may be named only when a public-source record directly
supports the factual statement and naming is necessary for the research.
Hypothetical scenarios should use “a hypothetical major global bank” or an
obviously fictional institution. They must not imply that a real institution
experienced, predicted, or concealed the scenario.

## ORX safeguard

ORX may appear in acknowledgment and disclaimer documents as an external
industry resource. It must not appear as:

- a source provider for repository records;
- an imported event, scenario, taxonomy, field, identifier, or schema;
- a prompt target asking a model to recreate ORX classifications; or
- a label suggesting compatibility, equivalence, affiliation, or endorsement.

If the provenance of a record is uncertain, exclude it.

## Release gate

Before a data-bearing change is merged or a release is published:

1. Verify every non-synthetic record against its public source.
2. Validate required metadata and review status.
3. Confirm that runtime databases, sidecars, model outputs, and local exports
   are not tracked.
4. Scan tracked text and binary artifacts for employer terms, prohibited
   provider terms, credentials, emails, usernames, private hosts, local paths,
   and personal data.
5. Inspect images and PDFs with OCR or equivalent visual review.
6. Run tests and provenance validators.
7. Review the complete Git diff and, for a public repository, repository
   history.
8. Document any approved reference to a restricted scan term so future audits
   can distinguish intentional disclosures from leaks.

Mentions of ORX and Nassim Nicholas Taleb are expected only in approved
release documents such as the disclaimer and acknowledgments. Their presence
elsewhere requires review.

## Corrections and removals

Open a repository issue for an ordinary citation correction. If a report could
expose confidential, personal, credential, or security-sensitive information,
follow [SECURITY.md](SECURITY.md) and do not post the material publicly.

Maintainers may remove or quarantine a record when provenance, accuracy,
privacy, or reuse rights are disputed. Removal from the current tree does not
remove prior Git history; maintainers must assess whether history rewriting,
credential rotation, host remediation, or platform support is also necessary.
