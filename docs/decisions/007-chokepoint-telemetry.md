# ADR-007: LLM telemetry at the client choke point

- **Status:** accepted
- **Date:** 2026-08-01

## Context

A coverage audit found 13 uninstrumented LLM call sites (legacy phases
1–3/6, the QA judge, Phase 8 drafting + judging, ingest
extraction/rubric). Patching each site individually was the wrong shape;
the spec's own rule is "no LLM call outside the instrumented client."

## Decision

`OMLXClient` takes an optional telemetry sink and node label and emits
**one event per call attempt** (tokens from the endpoint's `usage`
object when present — zeros when absent, latency, status, error class).
A shared production sink (`generated/telemetry/llm.jsonl`) is wired into
the pipeline client factory, the dashboard runner, and the ingest
endpoint. Emission can never break the caller, and the client is
un-instrumented by default so tests and offline paths stay quiet.

## Consequences

- 100% of LLM calls through the client are logged — the spec's coverage
  invariant holds by construction, not by convention.
- The Costs page can report real token counts, latencies, and (once
  priced) spend per phase/node/model.
- Per-attempt events make retries visible rather than averaged away.

## Alternatives considered

- **Per-call-site instrumentation** — rejected: 13 patches, guaranteed
  future leaks as new call sites appear.
- **HTTP proxy middleware** — heavier, duplicates parsing, and cannot
  attach node semantics as cleanly as the client itself.
