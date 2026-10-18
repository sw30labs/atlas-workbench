# ADR-006: Review-first artifact ingestion with rubric completeness check

- **Status:** accepted
- **Date:** 2026-08-01

## Context

Phase 1 input required hand-pasted text and hand-typed canonical URLs —
the main UX friction point. Users' real artifacts are PDFs, Office docs,
screenshots, and meeting transcripts. LLM extraction of sources risks
hallucinated URLs, which would poison the provenance model.

## Decision

Upload → convert (MarkItDown optional extra; honest degradation) →
bounded chunk-map extraction (per-chunk strict JSON, tenacity repair,
deterministic merge) → **rubric completeness check** → editable prefill.
Nothing writes to any store until the human clicks RUN; the existing
Phase 1 provenance gate is unchanged. Sources are lane-labeled:
`literal` (URL in the artifact), `llm` (LLM-resolved, amber), and
`unresolved` (needs a URL or drop). Completeness is judged by a rubric
(missed-sources enumeration, attribution fidelity with quoted spans,
body fidelity, load-bearing facts, deterministic URL validity) — **not**
by an LLM confidence number (explicitly rejected by the owner).

Of those, **load-bearing facts is reported but does not gate the verdict.**
The schema records each fact as `present|missing` with no way to express "the
source never contained this", so a `missing` cannot be attributed to the
extraction. Gating on it would fail every artifact whose source simply lacks a
scale figure, and a verdict that fires on evidence it cannot attribute stops
being read. The other three criteria each detect an extraction *error* — a
dropped source, invented attribution, invented facts — and do gate. The missing
items are surfaced beside the verdict so the reviewer sees them.

## Consequences

- Extraction is a draft by construction; the write path stays exactly
  the audited one.
- Hallucinated URLs cannot turn green: every LLM-sourced URL passes
  `is_public_http_url` *and* user review; unresolved references are
  visibly unresolved.
- Chunk maps are bounded (caps, overlap, one repair pass, deterministic
  merge) — cost and behavior stay predictable.

## Alternatives considered

- **One-shot LLM extraction of the whole document** — rejected for long
  artifacts (context limits, silent omissions); the chunk map exists for
  completeness.
- **Coverage percentage as the completeness metric** — rejected by
  owner: weak proxy; rubric with forced evidence instead.
- **Auto-ingest without review** — rejected: violates the provenance
  boundary that the rest of the system depends on.
