# Enterprise review resolution

8 September 2026. This document maps the substantive findings and recommendations
from the local `ULTRAREVIEW-2026-09-08.md` to the resulting implementation and its
regression evidence. The original review was an ignored working artifact; its
historical line numbers describe the pre-change code.

My product objective is to identify operational exposures that existing
assessments may have missed, explain their relevance to declared business
services, and turn them into owned tests and decisions. I treat working software,
practitioner usefulness, calibrated forecasts and production readiness as
separate claims. This implementation supports an explicitly scoped pilot; it
does not establish the latter three claims by itself.

“Implemented” below means the behavior is present in source with the cited
regressions. It is not a claim that a customer deployment or external platform
has been exercised. The final integrated test, browser and release-gate results
belong in the release evidence; they should not be inferred from this inventory.

## Correctness and governance findings

| Review finding | Resolution | Source and regression evidence |
| --- | --- | --- |
| **T1 — score identity collisions** | Training-event content, monetary observations, within-period counts and scoring configuration participate in identity. A duplicate realization identity with a different payload fails the transaction. | [Scorer](../../workbench_atlas/scoring/scorer.py), [compound persistence](../../workbench_atlas/compound_join.py); [identity/configuration/history/collision regressions](../../tests/test_scoring_enterprise_integrity.py). |
| **T2 — fabricated monetary units** | Missing loss amounts remain null. Occurrence and measured severity counts are separate. Every constituent must meet the training-window severity minimum before monetary research output is emitted; a known holdout loss cannot qualify unknown training losses. The minimum is a research-fit threshold, not evidence of financial calibration. | [Incident model](../../workbench_atlas/scoring/models.py), [R1](../../workbench_atlas/scoring/r1.py), [scorer](../../workbench_atlas/scoring/scorer.py); [missingness, holdout and constituent-sufficiency regressions](../../tests/test_scoring_enterprise_integrity.py). |
| **T3 — approval transferred by title** | Approval and evidence linkage use stable risk identities; duplicate or ambiguous identities fail safely. A pending same-title record does not inherit another record's approval or evidence. | [R1](../../workbench_atlas/scoring/r1.py); `test_same_title_cannot_transfer_approval_or_evidence` in [scoring integrity tests](../../tests/test_scoring_enterprise_integrity.py). |
| **T4 — ineffective download cap** | Production research streams bounded responses, rejects oversized/compressed bodies, avoids consuming redirect bodies and enforces a killable overall deadline. Search and PDF paths have bounded transport too. | [Research transport](../../workbench_atlas/research_transport.py), [research callers](../../workbench_atlas/research.py); [cap, compression, redirect, PDF and blocked-worker regressions](../../tests/test_research_transport.py). |
| **T5 — public hostname resolving privately** | Research validates resolved public addresses, pins the connection and preserves the HTTP/TLS hostname. Redirect targets are resolved and checked again. | [Transport](../../workbench_atlas/research_transport.py); [private-DNS, pinned-connection and redirect regressions](../../tests/test_research_transport.py). No live private-network probe is claimed. |
| **T6 — lost concurrent review decision** | Evidence decisions validate and update under one transaction with their immutable decision event. Risk decisions use the locked, atomically replaced register. Review interfaces submit the inspected revision; stale decisions conflict. Compatibility callers must supply the revision when making a decision based on an earlier inspection. | [Review](../../workbench_atlas/review.py), [decision ledger](../../workbench_atlas/decisions.py), [HTTP boundary](../../workbench_atlas/web/server.py); [concurrent-winner, immutable-history and atomic-risk tests](../../tests/test_decisions.py). |
| **T7 — broken egress audit chain** | The verified-tail read and append are serialized with a process-safe file lock. Damaged history fails closed; the predecessor is not cached across gateway instances. | [Egress gateway](../../workbench_atlas/scoring/egress.py); [multiple-instance, threaded and damaged-chain regressions](../../tests/test_scoring_egress.py). |
| **T8 — duplicate current compound pairs** | Current membership is governed by stable pair identity. Recomputed portfolios supersede prior candidates while preserving realizations and graduation history. Graduation deduplicates the pair. | [Join](../../workbench_atlas/compound_join.py), [graduation](../../workbench_atlas/compound_graduate.py); [graph-growth, historical repair and graduation regressions](../../tests/test_scoring_enterprise_integrity.py). |
| **T9 — missing accountable identity** | Evidence/risk decisions retain actor, reason, time, attestations and before/after revisions. Private context changes have the same attributable, revision-aware pattern. Shared mode replaces submitted names with the signed authenticated subject. Local names remain self-declared. | [Decisions](../../workbench_atlas/decisions.py), [context](../../workbench_atlas/context.py), [access boundary](../../workbench_atlas/web/access.py); [decision tests](../../tests/test_decisions.py), [authenticated actor tests](../../tests/test_access_web.py). |
| **T10 — unavailable data presented as empty** | Store health distinguishes available, missing and unreadable data. The dashboard reports failures rather than silently retaining a reassuring display. Diagnostics and baseline comparisons suppress unverifiable counts. | [Data health](../../workbench_atlas/web/health.py), [dashboard](../../workbench_atlas/web/static/index.html), [context comparison](../../workbench_atlas/context.py); [damaged-store HTTP tests](../../tests/test_web_enterprise.py), [diagnostic tests](../../tests/test_diagnostics.py), [baseline unavailability tests](../../tests/test_context.py). |

An additional activation defect found during integration was corrected:
flow-style, quoted, indented or aliased YAML cannot be corrupted by enabling
private context. The complete intended mapping, including unknown future
fields, is verified before atomic replacement. Serialization, sync and replace
failures preserve the original configuration. See [activation](../../workbench_atlas/web/context_setup.py)
and [targeted tests](../../tests/test_context_setup.py).

## Model claims and coverage

| Recommendation | Resolution and boundary |
| --- | --- |
| Replace “proven compound” and operational co-materialization claims | Graduation describes a hypothesis informed by held-out public-signal co-activation. Operational likelihood is not filled from that signal. Mechanism claims still require substantive source review. [Graduation](../../workbench_atlas/compound_graduate.py), [prompt regression](../../tests/test_scoring_enterprise_integrity.py). |
| Rename reference-count “Evidence Strength” | Human-facing labels use **Citation breadth**; the legacy field remains compatible. Publisher/domain concentration and duplicate fingerprints/URLs are explicit review aids. They do not establish independent incidents. [Diagnostics](../../workbench_atlas/diagnostics.py), [pilot/quality contract](pilot-validation.md). |
| Represent directness, contradiction and source families | Private `evidence_assessments` retain claim, relation, directness, owner, rationale, evidence, declared source/incident families and explicit verification. Contradictory and corroborating assessments coexist. Changed public evidence invalidates the current verification display without rewriting history. [Context contract](organization-context.md), [regressions](../../tests/test_context.py). No source independence is inferred or fed automatically into model confidence. |
| Separate ranking, calibration and readiness | Evaluation emits distinct verdicts and identifies the legacy recommendation as `ranking_and_structure_only`. Calibration failures cannot be hidden by a ranking pass. [Evaluation](../../workbench_atlas/scoring/evaluate.py), [verdict regressions](../../tests/test_scoring_enterprise_integrity.py). |
| Separate recent monitoring from held-out model data | Scoped baselines compare observed scenario/source changes and retain source dates, review state, content hashes and read cutoffs. Historical additions and retired scenarios remain labeled. Holdout discipline is retained. [Context baselines](organization-context.md). |
| Do not equate register representation with enterprise completeness | The organization assessment uses declared services × failure mechanisms. Unassessed cells remain unknown; tested cells require relevant completed exercise evidence. A missing, withdrawn or changed supporting mapping can require reassessment. [Coverage semantics](organization-context.md), [state-transition tests](../../tests/test_context.py). |

The earlier nominal-90% interval failures remain historical results in
[evaluation-analysis.md](evaluation-analysis.md). These code changes do not
retroactively repair those measurements or establish enterprise loss forecasts.
A bounded cross-channel discovery experiment and its incremental value remain
empirical work; existing exploratory generation is not a substitute for that
evaluation.

## Enterprise workflow and portability

| Recommendation | Implemented delivery | Remaining interpretation or acceptance work |
| --- | --- | --- |
| Service/entity inventory and dependencies | A separate opt-in store contains services, owners, criticality, tolerances, legal entity, region, processes, supplier identifiers and explicit service/provider dependencies with common causes. | The operator must supply and validate the real inventory, including fourth parties and shared people/facilities. A declared map is not automatic dependency discovery. |
| Controls, assumptions, actions and acceptance | Linked controls retain validation evidence and expiry; current staleness is derived. Assumptions have owners, tests and expiry. Actions distinguish investigation, test, remediation and risk acceptance; acceptance requires rationale, owner, expiry and evidence. | Control effectiveness and completed remediation require actual reviewed outcomes. An entered declaration is not an independently verified control. |
| Impact and exercises | Typed impact observations record time since onset, downtime, backlog, customers, capacity loss, units, assumed/measured basis and a pinned service-tolerance revision. Exercises retain scope, injects, outcomes and evidence. | The operator defines tolerance meaning and records real exercise results. No automatic pass/fail is inferred from free-text tolerance statements. |
| Coverage and evidence challenge | Explicit service/mechanism assessments, source-family declarations and contradictory claim reviews expose unexamined or unsupported assumptions. | Coverage is bounded by the declared inventory and mechanism set. It is not completeness of all possible operational risks. |
| Watchlists and committee changes | Fixed scenario watchlists and full-current-catalog baselines provide field-level scenario/evidence differences. Private baseline reports and a scheduler-friendly digest suppress verified unchanged material. | Scheduling, distribution recipients and retention remain operator-controlled. Digest creation does not send a notification or confirm an incident. Baseline review packs explicitly cover the private baseline workspace. |
| Portable exports | One canonical read-only snapshot serves PDF, CSV, JSON, XLSX and editable DOCX/PPTX renderers, selected relationship closure, provenance, history, checksums and frozen previews. Private context requires explicit selection and opt-in. Scoped evidence challenges include their target public evidence and review history. | PDF and editable documents/slides are presentations; JSON is the canonical machine representation. The supplied layouts are not customer-approved templates. Final visual rendering checks remain part of release acceptance. |
| Structured imports | CSV/JSON source mapping, typed preview, stable IDs, selected submission, revision checks and atomic relationships operate entirely in the private context store. | This does not import an arbitrary public research snapshot or convert incoming status text into a public approval. Destination-specific migrations need an explicit mapping. |
| One enterprise handoff | ServiceNow Import Set and generic REST staging packages retain stable external IDs, frozen revisions, mapping, durable receipts and explicit retries. | This is an offline staging adapter. The destination administrator must configure and execute its transform/import, inspect row outcomes and report them back. No remote delivery or reconciliation has been independently verified. |

Implementation evidence: [context](../../workbench_atlas/context.py),
[exports](../../workbench_atlas/exports.py), [imports](../../workbench_atlas/imports.py),
[baseline reports](../../workbench_atlas/assessment_reports.py),
[handoffs](../../workbench_atlas/integrations.py). Behavioral coverage is in
[context tests](../../tests/test_context.py), [export tests](../../tests/test_exports.py),
[import tests](../../tests/test_imports.py), [assessment-report tests](../../tests/test_assessment_reports.py),
[integration tests](../../tests/test_integrations.py), and the real-handler
[enterprise HTTP tests](../../tests/test_web_enterprise.py).

## User experience

The workbench preserves the research/review boundary while adding direct intake
and export entry points, readable portfolio tables, scenario comparison, saved
browser views, reading/motion preferences, private service mapping, actions and
exercise controls. The scenario brief uses an explicit initial-assessment state
when no change summary exists. Specialized terminology is replaced with task
labels, and model availability is distinguished from the ability to read saved
research. See [enterprise-experience.md](enterprise-experience.md),
[workbench behavior](../../workbench_atlas/web/static/experience.js),
[workbench styles](../../workbench_atlas/web/static/experience.css), and
[brief/review rendering](../../workbench_atlas/web/static/index.html).

Review panels retain deterministic blockers, source references, attestations
and inspected revisions; the workbench exposes decision history. The HTTP
regressions exercise selected import/export, conflicts, failed data reads,
actor binding and activation. They do not prove that an inexperienced analyst
can complete the journey unaided. Browser checks covered desktop and narrow screens, keyboard/dialog behavior,
meaningful names, reading and motion controls, source assessment and actual
PDF/ZIP downloads using isolated fictional records. The narrow page had no
horizontal document overflow. No WCAG conformance result is claimed.

The current routed brief remains the scenario detail surface. A dedicated
standalone detail page is not necessary to use the new actions, but remains a
design choice to validate. Guided installation/conversion setup is partial:
readiness, model settings, documented installation and explicit private
activation exist; a fully packaged first-run installer is not supplied.

## Shared operation, recovery and scale

| Recommendation | Resolution | Deployment boundary |
| --- | --- | --- |
| Authenticated shared access | Signed, expiring, replay-protected requests; explicit viewer/analyst/reviewer/admin routes; export capability; authenticated actor and preview ownership. Backend remains loopback. [Access code](../../workbench_atlas/web/access.py), [unit tests](../../tests/test_access.py), [HTTP tests](../../tests/test_access_web.py). | The customer must supply and validate IdP login, TLS termination and the server-side signing adapter. This is one shared tenant, not multitenant isolation or an enforced four-eyes rule. [Deployment contract](shared-deployment.md). |
| Durable jobs and truthful outcomes | Persistent journal, bounded queue, serial execution, interrupted-work reconciliation, queued cancellation and explicit linked retry. Returned phase errors mean failure, retaining partial results. [Runner](../../workbench_atlas/web/runner.py), [operations](../../workbench_atlas/operations.py), [tests](../../tests/test_operations.py). | Running Python work is not safely preempted or rolled back; an uncertain prior result must be inspected before retry. |
| Whole-workspace recovery | Configured stores, SQLite committed WAL state, decisions, context, outbox and job history enter checked backup bundles. Restore verifies checksums and uses a new directory. [Recovery procedure](operations.md), [restore tests](../../tests/test_operations.py). | The operator schedules backups, protects independent copies, sets retention and performs a real deployment restore drill. No live store is switched automatically. |
| Coherent modules and bounded portfolio reads | Enterprise HTTP, private context, exports, imports, access, health and reporting are separate modules. A bounded read-only scenario-page API provides deterministic sorting, filters, limits and query deadlines. [Portfolio](../../workbench_atlas/web/portfolio.py), [tests](../../tests/test_portfolio.py). | The legacy aggregate remains for compatibility. The page API is not proof that every dashboard surface is incrementally loaded or that customer-scale latency and concurrency targets are met. |
| Procurement and exit | Deployment, data boundaries, backup/restore, import/export and integration responsibilities are documented. Canonical exports provide an exit path. | Customer support terms, patch commitments, storage encryption, retention policy and security/regulatory acceptance remain deployment decisions. No certification is claimed. |

## Evidence still required before broader claims

My acceptance gates remain explicit:

1. **Release integrity verified for the candidate tree.** The integrated
   local suite passed 1,293 tests with 3 optional skips. After the final report
   integrity corrections, 131 report/export/HTTP checks passed, including 22
   added interruption, corruption and publication cases. Lint, offline checks, wheel
   build/isolated installation and browser verification passed. I verified an
   isolated public export containing one root commit with a passing history
   gate. The original reachable-history gate still fails on legacy history;
   changing this repository's visibility would expose that history. I preserve
   the working repository and use the isolated export procedure before any
   eventual public distribution. No public publication or history rewrite has
   been performed.
2. **A customer-controlled deployment.** Validate actual IdP roles, TLS,
   identity-header handling, signing-secret protection, replay storage,
   permissions, private-data retention and restoration. Local synthetic tests
   cannot validate an external identity provider or production host policy.
3. **A destination handoff.** Agree the selected GRC tables and mappings,
   configure coalescing/revision behavior, execute the import and reconcile
   actual destination outcomes. Operator-entered acknowledgments are evidence
   of an attributed report, not an Atlas network verification.
4. **Practitioner value.** Run blinded, service-scoped comparisons with the
   existing register and a credible alternative workflow. Measure useful novel
   exposures, duplicates, actionability, time to a reviewed brief and reviewer
   agreement. Follow accepted findings to evidenced tests and decisions.
5. **Forecast and robustness evidence.** Use actual dated outcomes for Brier
   scores and appropriate labeled numerical outcomes for interval coverage.
   Preserve cutoff/holdout discipline, test syndication and publisher-removal
   sensitivity, and evaluate consequential cross-channel hypotheses. Neither
   the scenario count nor model self-ratings establish value or calibration.

The [pilot protocol](pilot-validation.md) and [measurement implementation](../../workbench_atlas/diagnostics.py)
provide a concrete instrument with validated input, explicit denominators and
`not_validated` for missing or unsuitable evidence. [Pilot regressions](../../tests/test_diagnostics.py)
verify those reporting boundaries. No customer pilot, live-model improvement,
retrospective lead-time advantage, operational calibration or regulatory
compliance result is claimed by completing this engineering work.

## Local verification record

The isolated fictional browser walkthrough exercised service creation, a
fixed-scope baseline, two-scenario comparison, PDF download, private committee
PDF, staged ServiceNow-format ZIP and durable re-download history, plus an
unverified source assessment. Browser checks found and corrected a debounced
filter-selection race and legacy evidence-ID picker mismatch; executable
JavaScript regressions cover both. No real destination received data.

Final CI exposed a concurrent replay-ledger initialization race: changing
SQLite journal mode before reserving a write could fail immediately under
contention. Initialization now shares the nonce transaction and preserves the
existing journal mode. All 59 focused access tests passed, including a held
writer, synchronized cold starts, separate processes and existing-WAL replay
retention. Replay rejection and failure-closed storage behavior remain intact.

Export render verification covered a three-page Word sample and nine-slide
PowerPoint sample. The bundled renderer lacks CJK fonts; the OOXML text remains
intact, and Microsoft Office rendering is not claimed tested. Browser-generated
reports, screenshots and runtime stores remain outside the public source tree.
