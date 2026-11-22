# Organization CSV and JSON imports

The import workflow adds explicit organizational declarations to the separate
private workspace. It never writes the public evidence store or emerging-risk
register, invokes a model, or converts incoming approval labels into reviewed
public evidence.

An operator declares the source, previews mapped records, selects valid rows and
confirms them. Required fields and business rules are checked in the preview.
Relationships, existing identifiers and current revisions are checked together
when the selection is committed. A failed selection writes no records.

## Supported inputs

- UTF-8 CSV with a header and comma, semicolon or tab delimiters. Quote values
  containing delimiters or newlines. Column headers must be unique and nonempty.
- JSON arrays of flat record objects, or a single record object, with a selected
  organization record kind.
- Mixed JSON arrays of `{kind, data, expected_revision?}` entries, or a
  `{records: [...]}` wrapper. This format can create a related service, control
  and exposure in one atomic selection.

Files are limited to 2 MB and 500 records. Whole research export snapshots are
not accepted. JSON duplicate keys and non-finite numeric constants are rejected.
Source text is data, including text that resembles formulas or instructions.

The supported kinds are `services`, `mechanisms`, `dependencies`, `controls`,
`assumptions`, `exposures`, `actions`, `exercises`, `assessments`, `observations`
and `saved_views`. Their schemas come from `context.RECORD_MODELS`; required
fields and typed business rules are the same as individual context edits.

Example CSV for fictional service declarations:

```csv
Name,Owner,Tolerance,References
Fictional payments service,Synthetic service owner,Exercise tolerance of four hours,Document A;Document B
```

Map the source columns to `name`, `owner`, `impact_tolerance` and `source_refs`.
When a mapping is supplied, unmapped source columns are ignored. With no mapping,
headers must already be canonical field names. Array fields accept JSON arrays
or semicolon-separated values. Blank optional CSV fields use the typed defaults;
required blank fields remain validation errors. The source declared by the
operator fills missing record-level `source`; supplied per-record sources remain
visible in the preview. `classification` defaults to `organization` and may be
explicitly `synthetic`.

Use explicit IDs when new records reference one another. Missing IDs are minted
once during preview and frozen with the candidate, so confirmation cannot change
the displayed identity. To update a record, provide its `id` and an explicit
`expected_revision` integer. Existing IDs without a matching revision are rejected
at confirmation. Server-owned `revision`, timestamps and archive fields are not
accepted as importable business fields.

## Application API

```python
from workbench_atlas.imports import preview_import, submit_import

preview = preview_import(
    settings,
    content=csv_text,
    format="csv",
    kind="services",
    actor="Named operator",
    source="Declared source of the organization inventory",
    field_mapping={
        "Name": "name",
        "Owner": "owner",
        "Tolerance": "impact_tolerance",
        "References": "source_refs",
    },
)

# Display each candidate, its row_number, errors and warnings before confirmation.
result = submit_import(
    settings,
    preview_id=preview["preview_id"],
    selected_ids=["row-1"],
    actor="Named operator",
)
```

Preview candidates include their stable `candidate_id`, zero-based `row_index`,
human `row_number`, `label`, normalized `record`, `errors` and `warnings`.
`counts` reports total, valid and invalid rows. A valid syntax preview does not
promise that a referenced record exists or that a revision will still be current.
Select only desired valid rows; required dependencies must already exist or be
included in that selection.

Previews stay in process memory for 30 minutes, with at most 20 open previews.
Raw uploads are not retained. Tokens are bound to both the named actor and the
resolved private-workspace path. Returned preview data is a copy: altering the
client response cannot change the frozen records submitted to the store. A
candidate cannot be submitted twice, and concurrent submits of one preview are
serialized. An application restart expires outstanding previews.

In shared deployments, the server must supply the authenticated actor rather
than trusting an arbitrary client-provided name. Context decisions record that
actor, and existing context permission and separation rules still apply.

Public evidence ingestion remains the separate acquire, normalize and review
workflow. Importing licensed or internal organization context here does not
authorize publishing it or moving it into public research stores.
