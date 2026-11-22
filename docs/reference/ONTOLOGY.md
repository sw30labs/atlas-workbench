# Ontology Guard

Atlas Workbench combines probabilistic producers with a deterministic
admission boundary: **the pipeline proposes; the ontology permits**. Every
machine-produced write is checked before it reaches a governed store.

## Validation Model

The guard applies two complementary gates:

1. `ontology/guard.py` validates individual records with Pydantic. It rejects
   malformed identifiers, unknown enum values, invalid references, and bad
   dates at the door.
2. `ontology/shapes.ttl` applies SHACL rules to the candidate batch and an
   optional store projection. It detects dangling links, lifecycle conflicts,
   duplicate titles, and competing current runs.

`ontology/domain.ttl` supplies narrowly scoped inference. For example,
evidence events with the same canonical source URL are linked with
`owl:sameAs`; inference never bypasses SHACL admission.

## Governed Records

- `emerging_risk` records cite approved evidence and use persisted,
  project-native slug identifiers.
- `evidence_event` records describe public evidence. Their canonical URLs
  establish identity.
- `scenario` records may use only approved risks and evidence.
- `compound_scenario` records reference two or more registered risks and one
  `compound_join_run`.
- `channel_proposal` and `category_mapping_proposal` records remain inert
  until a reviewer decides them.

Verdicts are `ADMIT`, `AUTO_MERGE`, `FLAG`, `REJECT`, or
`REJECTED_AT_DOOR`. Warnings enter review; violations block the write and
leave the prior state intact.

## Pipeline Wiring

`workbench_atlas/guard_hook.py` adapts application rows to ontology records.
The write seams cover evidence ingestion, register saves, scenario upserts,
compound persistence, and machine-generated proposals. Human review actions
carry their own attestations; their resulting register writes still pass the
shared save boundary.

Set `ATLAS_GUARD=off` only for isolated tests that explicitly exercise code
outside the ontology boundary. Production and release verification must leave
the guard enabled.

## Run the Guard

Validate the self-contained synthetic fixture without reading runtime stores:

```bash
.venv/bin/python ontology/guard.py ontology/fixtures/candidates.json \
  --out /tmp/workbench-atlas-ontology
```

Regenerate a local store projection when required for operator diagnostics:

```bash
.venv/bin/python scripts/migrate_runtime_stores.py
.venv/bin/python ontology/export_existing.py
```

The explicit migration is transactional and idempotent; it skips missing
stores. Snapshot generation itself remains read-only.

`ontology/existing.ttl` and guard output directories are local runtime
artifacts. They are never release fixtures or sources for checked-in mappings.
For seam details and failure handling, see `ontology/WIRING.md`.

Wheel installs place the same tracked validator bundle under
`share/workbench-atlas/ontology` in the active Python environment.
`guard_hook.py` resolves that location automatically. If the guard is enabled
and any required script or TTL is missing, validation raises `GuardError`
instead of admitting the write.
