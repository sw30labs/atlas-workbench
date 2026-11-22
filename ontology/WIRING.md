# Ontology Guard Wiring

The ontology guard sits in front of every machine-produced write. It validates
staged records against the domain ontology and SHACL shapes before an
application store changes. `FLAG` findings are admitted and logged; `REJECT`
findings leave the previous state intact.

## Bundle contents

| Path | Role |
|---|---|
| `domain.ttl` | Project classes, properties, identity rules, and category links |
| `shapes.ttl` | Constraints C-01 through C-10 |
| `guard.py` | Two-gate validator and record projection models |
| `export_existing.py` | Regenerates the ignored local snapshot from operator stores |
| `fixtures/` | Deliberately fictional positive and negative records for every constraint |

`existing.ttl` and `out/` are runtime outputs. They are ignored, are not
release inputs, and must never be used as fixture sources.

## Write seams

1. `phases/phase2_risks.py:save_emerging_risks` validates a complete register
   replacement before the atomic write.
2. `compound_join.py:run_compound_join` validates compounds and their run
   record before database or artifact writes.
3. `phases/phase1_ingest.py:ingest_text` validates an evidence event before
   `add_evidence_event` stores it.
4. `db.py:upsert_scenario` validates every scenario writer at the storage
   boundary.
5. `proposals.py:create_channel_proposals` validates proposal-only records;
   validation can never approve them.
6. `frameworks/mappings.py:create_mapping_proposal` validates a pending public
   framework mapping before insertion.

The adapter is `workbench_atlas/guard_hook.py`. For an upsert, it removes the
existing triples for the same record identity from the in-memory snapshot,
then validates the proposed replacement. This avoids a stale value blocking
its own repair while preserving fail-closed storage behavior.

## Identity and classifications

Register records carry a stable `risk_id`. Readers prefer that persisted value
and mint a slug only for a legacy record that lacks it. A duplicate slug is a
review flag, never proof that two records are identical.

Evidence producers may emit `event_driver_class` and
`primary_cause_class` only from the closed project vocabularies. An absent or
out-of-vocabulary classification becomes `null`; keyword inference must not
turn prose into an unsupported fact. `risk_category` remains a required
project category because downstream scoring and review use it.

## Operational rules

- `ATLAS_GUARD=off` is an explicit operator kill switch. Tests set it when a
  unit is not exercising the ontology seam.
- A rejected batch may be regenerated twice. After that, an operator reviews
  the repair report; producers never retry without a bound.
- Human approval remains in the review layer. An ontology verdict is not a
  review decision.
- Upgrade existing stores with
  `.venv/bin/python scripts/migrate_runtime_stores.py`, then rebuild a local
  snapshot with `.venv/bin/python ontology/export_existing.py`. The exporter
  remains read-only.
- Fixtures must remain synthetic. Never copy a row, identifier, count,
  headline, URL, or adjudication from an operator store into this bundle.
