# Testing & Quality

Suite verified 2026-08-12: **737 collected — 734 pass, 3 skip offline**
(pytest, `tests/`; the skips are optional real-OMLX probes). Runtime was
114.29 s on the verification host. Everything is offline except explicitly
live-verified local OMLX calls.

## 1. Layout by concern

| Area | Files |
|------|-------|
| Legacy pipeline phases | `test_phase1_2.py`, `test_phase3_4.py`, `test_phase5.py`, `test_phase6.py`, `test_pipeline.py` |
| Provenance & review | `test_provenance.py`, `test_review.py`, `test_review_boundaries.py`, `test_scouting.py`, `test_demo_release.py` |
| Scoring engine | `test_scoring_core.py`, `test_scoring_scorer.py`, `test_scoring_synthetic.py`, `test_scoring_eval.py`, `test_scoring_hardening.py` |
| Evaluation harness | `test_scoring_eval.py`, `test_scoring_ablations.py` |
| External features | `test_scoring_ext.py` |
| Synthetic scoring cells + artifact ingestion | `test_scoring_synthetic_cells.py`, `test_ingest_artifacts.py` |
| Telemetry & egress | `test_scoring_telemetry.py`, `test_scoring_egress.py`, `test_llm_telemetry.py` |
| Compound flow | `test_compound_join.py`, `test_compound_graduate.py` |
| Web | `test_web_security.py`, `test_web_foresight.py`, `test_web_costs.py`, `test_web_tokens.py`, `test_web_compounds.py`, `test_web_compound_join.py` |
| Unified research intake | `test_intake.py`, `test_web_intake_flow.py`, `test_singularity_source.py` — free-form graph, provenance clamp, QA/repair budget, duplicates, draft submit, approved-only recommendation, optional fail-soft Singularity Atlas pull |
| Review card & advisory assist | `test_web_review.py` — validation-report/validator agreement property, attestation enforcement, register-lock 409, mutation guard, suggestion cache + staleness, agreement audit |
| Deep research | `test_review_research.py` — DDG parser (redirect unwrap, non-public filtering), capped fetch (private-redirect block), scripted loop (pending-only candidates, invented-URL block, `no_evidence_found` path, dedup), adopt-refs (approved-only, idempotent) |
| Category/scouting/foresight | `test_category_cleanup.py`, `test_core.py`, `test_foresight_enrichment.py` |
| MITRE ATLAS catalog | `test_mitre_atlas_import.py` — bounded strict YAML, exact-byte digest, atomic normalized import, sealing, activation, immutable historical releases |
| MITRE ATLAS governance | `test_mitre_atlas_mappings.py`, `test_guard_seams.py` — pending-only intake, ontology tripwire, database review transitions, pinned provenance, monotonic staleness, non-scoring context |
| MITRE ATLAS dashboard | `test_mitre_atlas_web.py` — status/search/proposal APIs, Review decisions, current/stale approved display, escaping, optional-catalog behavior |
| Settings | `test_web_settings.py` — catalog status, local-file/base64 import, origin gate, activation, YAML model write with comment preservation, env-override 409 |

Shared fixtures (`conftest.py`): `tmp_workspace` (synthetic dbs + a
synthetic risk), `settings`, `mock_omlox_client` (no network).

## 2. What the suite enforces (invariants)

- **Reproducibility**: same seed → identical events, realizations,
  promoted sets, and R0 datasets (separate RNG streams for counts).
- **Holdout discipline**: fits ignore holdout periods; labels come only
  from the test window; a shuffle-label null must not beat base rate.
- **Egress**: local-detail content denied in keys/values/nesting; audit chain
  tamper-evident.
- **Telemetry**: one event per LLM attempt; errors logged per attempt;
  run report sums within $0.001.
- **No silent fabrication**: sparse data records `insufficient_data`
  instead of scores; unsupported artifact types warn instead of
  guessing; unstable ξ downgrades EVT claims.
- **Structure over frozen content**: repo-artifact tests never hardcode
  counts from runtime files.
- **Reports mirror validators**: the non-raising validation reports
  must agree with the raising validators on approvability across a
  record matrix (drift guard for the traffic lights).
- **Proposals only**: machine assistance can never produce an
  `approved` record; deep-research candidates always land `pending`,
  and adopt-refs refuses anything not fully approved.
- **No invented URLs**: the research fetcher asserts every URL came
  from the search step.
- **Pinned external context**: a mapping resolves only through its reviewed
  ATLAS release; activating a newer release cannot drift old context or source
  provenance.
- **No silent mapping reactivation**: scenario revisions increase across
  meaning-bearing edits, restoration, deletion, and recreation, so an old
  approval cannot become current without a new review.
- **Hostile framework input fails closed**: byte, alias, node, and nesting
  bounds apply before normalization; duplicate keys, recursive aliases,
  invalid identifiers, dangling relationships, and digest conflicts roll back.

## 3. Known flake

`tests/test_web_security.py::test_run_rejects_oversized_body_before_dispatch`
is timing-sensitive under full-suite load (observed failing
intermittently on 2026-08-01; passes standalone and on rerun). If it
becomes frequent, wrap the assertion in a short retry or serialize the
web tests.

## 4. Quality gates in CI (wired — `.github/workflows/quality.yml`)

On every push and PR, per Python 3.12 and 3.13:

1. `python -m compileall` on package + scripts.
2. `ruff check --select F821,F811,F402,E9,F841` — the crash-class lint gate
   (undefined names, redefinitions, shadowed imports, syntax); `ruff`
   ships in the `dev` extra.
3. `python -m pytest` (full suite green).
4. `python scripts/verify_all.py` (offline integration checks).
5. `python scripts/public_release_gate.py --history` (clean-room
   release boundary, worktree + reachable history).

A separate job builds the wheel + sdist (`py.typed` ships;
`scripts/karpathy_*` is MANIFEST-excluded). Still manual/suggested:
`atlas-eval demo` GO on the pre-registered R0 seed, and the ablation
matrix.
