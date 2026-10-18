# Enterprise discovery and experience delivery

Tracking issue: [#13](https://github.com/sw30labs/contingency-atlas/issues/13).

I am extending Contingency Atlas into a governed operational-risk discovery
workbench. The user journey is: establish the service context, investigate a
signal, review the evidence, examine an exposure, assign a decision or test,
and share a traceable brief.

## Delivery checkpoints

- [x] Correct scoring identities, approval linkage, loss semantics, and current
  compound membership; distinguish ranking evidence from calibration.
- [x] Bound research transport and preserve a valid concurrent audit chain.
- [x] Record accountable, revision-aware decisions and visible data health.
- [x] Add canonical PDF/CSV/XLSX/JSON export and reviewed structured import.
- [x] Introduce a separately governed organization context: services,
  dependencies, controls, assumptions, exposures, actions and exercises.
- [x] Refine the GUI around meaningful visual exploration, readable priorities,
  contextual intake, comparison, saved views, decision briefs and exports.
- [x] Add persistent job state and restart recovery, configurable backup and
  verified restore, and controlled integration/access capabilities.
- [x] Add baseline comparisons, evidence-quality diagnostics and pilot
  evaluation instruments; retain explicit uncertainty and missing-data states.
- [x] Reconcile public release history through an isolated clean export, verify
  install/build/test/release workflows, and document operational ownership.

## Invariants

Public research and private organization context have separate storage and
export policies. Unreviewed material does not gain approval through import,
export, model output or a UI shortcut. A generated scenario remains a
hypothesis. Monetary outputs require measured monetary inputs. Historical
decisions and score realizations remain inspectable. Existing operator stores
are migrated additively, never reseeded during development.

I retain the expressive visual identity and exploratory charts while adding
clear reading and reduced-motion options. Every important visualization has
an accessible textual equivalent and leads to a useful next action.

## Verification

Each checkpoint receives behavioral tests and an independent review where
practical. Browser checks use isolated fictional fixtures. PDF and spreadsheet
exports receive real-format validation. Full tests, offline verification,
lint, package checks and the worktree release gate run before delivery.
History remediation uses an isolated release tree without rewriting the
working repository or weakening its release gate.

Human pilot results, independently established model calibration, customer
identity-provider configuration and live destination integration credentials
cannot be manufactured by implementation. I provide executable instruments
and configuration paths, and report these validation/deployment dependencies
explicitly.

## Organization fit

The September 2026 SW3.0 Labs organization presents work through research
questions, inspectable evidence, maturity and next milestones. I apply that
language to Atlas: a visual should communicate exposure, evidence and the next
decision. The source of inspiration is the public
[SW3.0 Labs profile](https://github.com/sw30labs), not imported organization data.

## Delivery verification — 8 September 2026

I completed 1,293 passing tests with 3 optional tests skipped, followed by 131
passing report/export/HTTP checks after the final report integrity corrections.
I also completed the CI lint gate, offline integration checks, wheel build and
isolated installation smoke checks.
I verified the GUI with temporary fictional records at desktop and narrow
widths, including real PDF downloads, private baseline reports, source
assessments, comparison and durable staged handoffs. Editable Word and
PowerPoint exports received format and visual checks.

The source worktree passes its public release gate. I also verified an isolated
source export with exactly one root commit and a passing history gate. This
does not clear the original repository's legacy history for public visibility.
I have not published that history or changed repository visibility.

[Review resolution and remaining deployment evidence](../technical/review-resolution.md)
records the implemented capabilities and their practical limits.
