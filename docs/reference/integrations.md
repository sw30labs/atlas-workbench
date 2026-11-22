# Offline enterprise handoffs

Atlas prepares reviewed staging packages for ServiceNow Import Sets or a generic
REST consumer. This is a local staging adapter, not a configured native connector:
it never connects to a destination, stores credentials, executes a transform, or
claims that records reached another system.

## Operator workflow

1. Select explicit scenarios. Organization context remains excluded unless it is
   explicitly selected. Choose `servicenow` or `generic_rest`, a stable source-system
   identifier and a destination staging-table name.
2. Prepare and review the staged records. Preparation persists a frozen package in
   the private local outbox. Unlike the ordinary export preview, it survives a
   dashboard restart. Download its ZIP after reviewing the content and mapping.
3. The destination administrator configures the staging table, field mapping and
   transform rules. Execute the external import through that organization's tools.
4. Review each destination row's actual result. Record an **accepted** acknowledgment
   with its destination ID, or a **rejected** acknowledgment with its reason.
5. Deliberately select unresolved or rejected rows for a retry packet. Accepted rows
   cannot be retried from that package. Investigate uncertain remote outcomes before
   executing a retry: absence of a local acknowledgment does not prove that the
   destination did not receive the earlier row.

Acknowledgments are attributed operator reports. Atlas does not verify them with
the destination. They affect the outbox only; they never approve public evidence,
change an organization assessment or overwrite a source record.

## ServiceNow setup responsibilities

ServiceNow's Import Set API uses a staging table and its configured transform maps;
the response includes transform results for destination records. The default
`u_contingency_atlas_import` is a suggested customer-created staging table, not a
table Atlas creates or assumes exists. [ServiceNow Import Set API](https://www.servicenow.com/docs/r/api-reference/rest-apis/c_ImportSetAPI.html)

Configure coalescing for the stable Atlas source identity. ServiceNow documents
that a matching coalesce value updates an existing record and that importing
without coalesce fields always inserts records. The package's source-system and
external-ID columns provide inputs for that configuration; Atlas cannot enforce
the destination transform. [ServiceNow: Coalescing](https://developer.servicenow.com/dev.do?_escaped_fragment_=%2Flearn%2Flearning-plans%2Fxanadu%2Fservicenow_application_developer%2Fapp_store_learnv2_importingdata_xanadu_coalescing)

Also configure the transform to recognize an already applied `revision_hash` and
handle older revisions deliberately. A successful transport response alone is
insufficient evidence that every row transformed successfully. GRC target tables,
required fields, workflow states and permissions are customer-specific.

The generic adapter emits the same mapped `{"records": [...]}` envelope without
ServiceNow's suggested `u_` column prefix. It deliberately defines no destination
URL, HTTP method, authorization header or automatic POST behavior.

## Stable identities and revisions

Each staged row has these canonical fields:

| Field | Meaning |
| --- | --- |
| `source_system` | Stable namespace for this Atlas deployment |
| `external_id` | `source_system:record_type:record_id`; preserved across retries |
| `record_type` / `record_id` | Original stable Atlas kind and identity |
| `revision_hash` | SHA-256 of the canonical source record, including recorded revision fields |
| `display_name` | Human-readable source label |
| `classification` / `source_status` | Recorded provenance and workflow state; never destination approval |
| `record_json` | Exact complete source record as a JSON string |

Set a distinct, stable `source_system` for each Atlas deployment before external
execution. Changing it later changes the external identity. Editing a source
record produces a new revision hash while retaining its external ID. Repeating
the same preview with the same operator, selection, mapping and source revisions
returns the existing package rather than creating a duplicate. An explicit retry
creates a child package that retains the original frozen revisions.

The staging rows include selected scenarios, linked evidence events and explicitly
included `organization_*` records. They are not a replacement for the full research
export: score realization and proposal tables are not destination rows. Use the
canonical research export when complete research history is required.

## Mapping contract

`mapping` accepts only:

```json
{
  "source_system": "example-atlas-production",
  "staging_table": "u_contingency_atlas_import",
  "fields": {
    "source_system": "u_source_system",
    "external_id": "u_external_id",
    "record_type": "u_record_type",
    "revision_hash": "u_revision_hash",
    "record_json": "u_record_json"
  }
}
```

Omit `fields` to include all canonical columns with the adapter's defaults. Field
mapping is canonical field → destination staging column. The five fields above
are mandatory. Destination names must be unique plain identifiers; URLs, unknown
source fields, target `sys_id` and workflow approval/state columns are rejected.
No mapping changes public-store approval semantics.

## ZIP contents

- `records.json`: exact mapped text in a `records` array.
- `records.csv`: mapped staging columns; formula-like values have a protective
  apostrophe. Prefer JSON if exact text must be retained without interpreting that
  CSV protection.
- `canonical-records.json`: complete original data for exactly the staged rows.
- `source-snapshot-manifest.json`: source snapshot timing, selection, provenance
  warnings and checksums, without additional source record bodies.
- `acknowledgments-template.json`: package ID, external IDs and revision hashes;
  statuses start blank and must be filled from reviewed destination results.
- `manifest.json`: mapping, classification, source snapshot ID, record hash and
  SHA-256 checksums for every other file.
- `README.txt`: execution and reconciliation instructions.

Retry downloads include only the explicitly selected retry rows, including in
their canonical data and acknowledgment template. Source-manifest counts describe
the original research snapshot; `manifest.record_count` describes the actual packet.

## Persistence and API

The outbox is `integration-outbox.db`, beside the configured organization-context
database. It is a distinct SQLite store, created with mode 0600. It may contain
private source bodies and must be backed up and retained according to organization
policy. It is excluded from public research stores and exports. Reads do not create
or migrate absent stores. Unrecognized schemas or payload-integrity failures stop
the operation. The outbox is limited to 300 packages, with 2,000 rows and 20 MB per
package; it never silently evicts audit history.

Python entry points in `contingency_atlas.integrations`:

```python
preview_integration(settings, *, actor, selection, adapter="servicenow", mapping=None)
download_integration(settings, *, package_id, actor)
acknowledge_integration(settings, *, package_id, acknowledgments, actor)
retry_integration(settings, *, package_id, selected_ids, actor)
list_integrations(settings)
```

Selection accepts nonempty `scenario_ids` and optional strict boolean
`include_context`. Preview returns `package_id`, manifest, records and per-row
states; download returns an `ExportArtifact` ZIP. Listing returns bounded package
metadata and row outcomes without staged record bodies. Download, acknowledgment
and retry require the creating operator identity. Shared-mode authorization is
enforced by the HTTP boundary described in [shared-deployment.md](shared-deployment.md).

An acknowledgment has `external_id`, exact `revision_hash`, `status` (`accepted` or
`rejected`), and `target_id` or `message` as appropriate. Each submitted set is
validated atomically. Unknown identities, stale hashes, duplicate rows or attempts
to rewrite an acknowledged outcome fail. Identical repeated acknowledgments are
idempotent. The receipt log is append-only. A retry does not silently refresh source
content: prepare a new preview to send a newer source revision.

The corresponding dashboard routes are `/api/integrations` plus POST
`/api/integrations/preview`, `/download`, `/ack` and `/retry`. The adapter performs
no network I/O. Tests cover source isolation, scope, identity, mapping, exact JSON,
CSV protection, frozen ZIPs, durable reconciliation, explicit retry, corrupt stores
and HTTP owner/origin boundaries using fictional temporary data only.
