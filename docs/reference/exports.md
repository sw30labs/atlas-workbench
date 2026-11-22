# Research exports

Exports create a read-only snapshot without running a model, changing a score,
or making a review decision. The snapshot preserves canonical identifiers,
scenario origin, lifecycle and review status, evidence provenance, references,
score components and available review history.

## Formats

| Format | Intended use | Contents |
| --- | --- | --- |
| JSON | Canonical interchange and archival comparison | Manifest and complete related tables, retaining exact text and nested values |
| CSV ZIP | Tabular imports and spreadsheet inspection | One table per CSV, normalized relationship rows and a manifest with file checksums |
| XLSX | Analyst review | Typed cells, separate related tables, column filters, frozen headers and a Read me sheet |
| PDF | Committee and workshop discussion | Selected scenario briefs, signals, compound candidates, evidence bibliography and review context |
| DOCX | Editable committee and workshop documents | Native Word paragraphs with selected briefs, readable organization records, provenance and review warnings |
| PPTX | Editable presentation and discussion packs | Native slide text with the same selected research and review context, continued across slides as needed |

PDF, DOCX and PPTX present the selected research. Use JSON for a complete
machine-readable snapshot, including full review and scoring details. A
snapshot is not a certification that hypotheses are observed facts or that
supporting evidence remains suitable for a different audience.

## Command line

```bash
atlas-export --format pdf --scenario-id SCENARIO_ID --output review.pdf
atlas-export --format docx --scenario-id SCENARIO_ID --output review.docx
atlas-export --format pptx --scenario-id SCENARIO_ID --output review.pptx
atlas-export --format xlsx --status active --output active-scenarios.xlsx
atlas-export --format csv --risk-id RISK_ID --output signals.zip
atlas-export --format json --output research-snapshot.json
```

Repeat an identifier option to select several records. `--compound-id` and
`--evidence-id` work the same way. With no selections or filters, all catalog
records are included, retaining every review status. `--status` and `--origin`
restrict scenario roots; they do not hide the review state of linked evidence.
An existing output file is never overwritten.

Selected scenarios include their references and signpost review history;
selected compounds include constituent risks, supporting evidence and score
realizations. Evidence referenced by historical assessments is retained even
when it is not cited by the latest scenario projection. Unresolved or ambiguous
references remain explicit in the relationship table and manifest warnings.
Superseded compounds are omitted from the current-compound table; historical
score realizations are retained in a full snapshot.

## Application API

```python
from workbench_atlas.exports import build_snapshot, export_data, render_snapshot

snapshot = build_snapshot(settings, scenario_ids=["SCENARIO_ID"])
preview = snapshot["manifest"]
artifact = render_snapshot(snapshot, format="pdf")
# artifact.filename, artifact.media_type, artifact.content
# artifact.snapshot_id, artifact.record_counts
```

`export_data(settings, format="json", **selection)` combines both steps.
`scenario_ids`, `risk_ids`, `evidence_ids` and `compound_ids` accept lists or
`None`. All selectors `None` means the whole catalog. Once an identifier list
is supplied, only those roots and their dependencies are included. An explicit
empty list or unknown identifier is an error, never a fallback to all records.
`ExportError` messages are suitable for the operator; underlying database or
filesystem errors are not exposed.

`include_context=True` (CLI `--include-context`) is a separate opt-in. It requires
the organization-context workspace to be enabled and an explicit non-empty
`scenario_ids` selection. Only the context module's scope-limited related records
are included, under `organization_` table names. Full-catalog selections cannot
implicitly disclose the whole organization. These artifacts are labeled
`includes_organization_context`; the PDF, DOCX and PPTX display that classification
prominently. A scoped private evidence assessment also includes its public target
evidence, the connecting relationship and available review history, even when
that evidence is counterevidence absent from the scenario's current citations.
Unselected private assessments remain outside the snapshot.
Organization context is read in its own snapshot window.

The dashboard also offers a frozen committee pack for a selected organization
baseline. Its private opt-in is explicit, and its review summary, changes and
notes use the same renderers. Nested before/after values are rendered as labeled
fields rather than opaque JSON. Subsequent workspace edits do not alter a
previously previewed pack.

The manifest includes schema and application versions, start and finish times,
selection, record counts, taxonomy and algorithm versions, absent optional
tables, relationship warnings, canonical-table SHA-256 hashes and a stable
content-derived snapshot identifier. CSV ZIP also includes hashes of each file.
These checksums detect changes; they are not signatures or identity attestations.

## Boundaries and format safety

- Required scenario, evidence and risk stores must exist and be readable. A
  missing required table, malformed existing optional table or corrupt source
  stops export. The exporter never creates or migrates stores.
- Each SQLite store is held in a read transaction. The risk register is checked
  for replacement while records are read. The two databases and JSON register
  are not one atomic transaction; the manifest states this limitation.
- Optional tables are enumerated explicitly. The exporter does not serialize
  arbitrary tables, model settings, credentials, local paths or pipeline logs.
- Legacy risks without stored IDs receive the application's deterministic ID
  in the export only, with `export_identifier_origin=derived_from_legacy_title`.
  Missing or duplicate identities fail instead of silently merging records.
- Exported public references retain review, rights and provenance metadata.
  Proposed regulatory instruments retain `instrument_status`. Synthetic records
  and model-assisted review history retain their labels.
- Framework mapping records include `export_mapping_state`, calculated from
  the stored scenario hash and revision. Stale approved mappings and missing
  pinned-release provenance are explicitly warned about in the manifest.
- SQLite review decisions are collected from the scenario and evidence stores
  and remain scoped to selected objects. Embedded risk `review_history` is
  retained. Full exports include orphaned decision history for audit; selected
  exports do not bring unrelated decisions into a pack.
- CSV quotes all fields and prefixes formula-like text with an apostrophe.
  This deliberately changes those CSV cell values; JSON preserves exact text.
  Spreadsheet re-saving can alter escaping. Use XLSX for human spreadsheet
  review and JSON for faithful machine interchange.
- XLSX writes untrusted strings as text, never formulas or automatic hyperlinks.
  Fields exceeding Excel's text limit or containing forbidden XML controls fail
  with guidance to use JSON instead of silently truncating data.
- PDF escapes markup, permits only structurally public HTTP(S) source links,
  never fetches remote images or pages, and wraps content across numbered pages.
  Characters unsupported by the portable PDF base font are written as Unicode
  escapes. JSON, XLSX and Office documents retain original Unicode text, subject
  to each format's control-character constraints.
- DOCX and PPTX contain editable text, with no generated imagery, macros, remote
  assets or external relationships. Invalid XML code points are displayed as
  explicit Unicode escapes. Both use Arial and do not embed fonts; installed
  fonts and the opening application can affect glyph availability and wrapping.
  The synthetic QA documents were rendered with the bundled LibreOffice Word
  renderer and slide renderer, and their layouts inspected. The Word renderer
  lacked CJK glyphs available in the slide renderer; exact Unicode remains in
  the Office files. Rendering in Microsoft Word and PowerPoint was not checked.
- DOCX is limited to 20,000 paragraphs and PPTX to 200 slides. Long slide body
  content continues on additional slides. Long slide headings may be shortened;
  the complete record title remains in the body. These limits fail with an
  operator-facing error instead of silently discarding the selected records.
- Exports are bounded at 100,000 rows per table and 32 MB for the risk register.

The public-edition data boundary in `DATA_PROVENANCE.md` still applies to any
export proposed for publication or repository inclusion. Exporting is not a
license or permission to redistribute source material.
