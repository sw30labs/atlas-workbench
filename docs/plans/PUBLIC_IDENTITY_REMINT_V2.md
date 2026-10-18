# Public Identity Remint V2

## Status and authority

Status: implemented locally on 2026-08-29; release-administration steps remain
open. This plan supersedes the earlier partial independence assessment and its
decisions to freeze legacy evidence and synthetic-import identifiers. The
private source history was not rewritten or published. The release workflow
creates a separately verified, one-commit public lineage.

## Target boundary

The tracked tree, dashboard, prompts, ontology, examples, wheel, source
distribution, and public Git lineage must contain only project-native or
public-standard contracts. Runtime stores and operator research remain local.
Facts, identifiers, counts, labels, or judgments derived from those stores
must not be copied into source comments, fixtures, tests, or documentation.

## Canonical contracts

| Legacy concept | Canonical public concept |
|---|---|
| evidence table and helpers named for one source medium | `evidence_events` and `*_evidence_event*` |
| evidence classification columns | `event_driver_class` / `primary_cause_class` |
| extraction input text | `source_text` |
| ontology source-medium class | `EvidenceEvent` / `evidence_event` |
| identifier-prefix origin inference | explicit `origin` field |
| institution-shaped scoring cell field | `scoring_cell_code` |
| numbered internal-data regimes | descriptive public, aggregate, and local-detail classes |

Canonical writers, prompts, APIs, fixtures, and UI code emit only the new
contracts. Compatibility is permitted only in a bounded, tested migration
reader and expires after the migration window.

## Work packages

1. Replace tracked runtime-derived witnesses with deliberately fictional
   fixtures and generic invariant explanations.
2. Migrate the evidence schema transactionally across SQLite, Python, API,
   dashboard, ontology, prompts, examples, tests, and documentation.
3. Remove the arbitrary private-extract entry point and future internal-data
   pilot from the public edition. Retain only a synthetic scoring exercise
   under descriptive names.
4. Replace prefix-sensitive behavior with explicit, persisted provenance.
5. Enforce excluded-source denial at discovery, fetch, redirects, storage,
   cache use, and registry validation.
6. Discover all shipped prompt contracts automatically, scan them for residue,
   and require a version for every contract.
7. Scan normalized paths, content, schema identifiers, archive members, Git
   metadata, and reviewed binary text/metadata. Release from a tracked-only
   staging directory.
8. Regenerate public assets and documentation after contract changes, then
   build and inspect both distribution formats.

## Migration invariants

- Migrations run in one transaction and fail closed on ambiguous dual schemas.
- Record identifiers, review decisions, immutable histories, and provenance
  are preserved.
- Pre/post row counts and canonical record fingerprints match.
- No compatibility alias appears in a prompt, writer, API response, or UI.
- No local runtime store is edited without an explicit operator action.

## Completion gates

- Full pytest, crash-class Ruff, compile, and offline verification pass.
- Worktree, package, binary, and clean-public-history identity scans pass.
- Denied hosts cannot reach a memo, evidence candidate, or cache.
- A clean clone built from the export reproduces the test and package results.
- All locally executable checklist controls are recorded against that clean
  clone. Release tags, external-link availability, repository visibility, and
  hosting security settings remain explicit operator or administrator actions.

## Completion record

- Source verification: 813 tests passed and 3 optional probes skipped; Ruff,
  compile, scouting-registry validation, and offline integration checks passed.
- Distribution verification: wheel and sdist build successfully; the release
  gate scans archive members, binds reviewed binaries to SHA-256 digests, and
  checks installed ontology files byte-for-byte against tracked sources.
- Installed-wheel verification: a fresh environment resolves the packaged
  ontology bundle and obtains an `ADMIT` verdict for a minimal synthetic risk.
- Public-lineage verification: the export contains one clean root commit, no
  remotes, and passes the worktree, artifact, reachable-history, and dedicated
  secret scans. The source repository's index, refs, history, and remotes remain
  unchanged.
