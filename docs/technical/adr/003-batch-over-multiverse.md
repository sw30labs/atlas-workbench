# ADR-003: Batch compound scoring; multiverse loop removed

- **Status:** accepted
- **Date:** 2026-08-01

## Context

The redesign (Phase 2) built a CBGE multiverse search loop: channel-
blocked expansion, promotion gates, archive, checkpoints, branches and
merges in a SQLite multiverse store. It passed its own pre-registered
kill test on the synthetic R0 regime (52 candidates vs 105 batch, equal
P@20 0.9, slightly better AP).

## Decision

Remove the loop, multiverse store, Mermaid projection, and their
dependents (coherence judge, A/B harness, expert panel, workshop
exporter, `atlas-loop` CLI) from mainline. **Production compound scoring
is the batch path** (Phase 7: score all channel-blocked pairs once per
monthly run).

## Consequences

- ~2,500 lines and ~45 tests removed; the codebase matches what
  production actually runs.
- The batch-vs-loop result is preserved in git history and as
  **historical rows** (A2/A4/A6) in the ablation matrix — the loop is
  one `git revert` away if expensive judgment ever makes search
  economics matter.
- The decision overrides the loop's own "keep_loop" test result: the
  test passed on synthetic data at trivial scale (105 pairs), which the
  owner judged insufficient to justify the machinery. Recorded
  explicitly rather than silently.

## Alternatives considered

- **Keep the loop dormant** — rejected by owner decision after review:
  no phase called it, and its GUI added no value (console already
  dropped separately).
- **Auto-switch loop vs batch monthly** — offered, declined; batch
  everywhere is simpler and currently sufficient.
