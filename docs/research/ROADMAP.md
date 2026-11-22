# Atlas Workbench Product Roadmap

## Document control

| Field | Value |
|---|---|
| Document owner | Atlas Workbench maintainer |
| Status | Active |
| Version | 1.1 |
| Effective date | 2026-08-12 |
| Last reviewed | 2026-08-12 |
| Next review | At the next MITRE ATLAS upgrade or 2026-09-12, whichever comes first |
| Review cadence | At each product milestone and at least monthly while delivery is active |
| Change authority | Project maintainer |
| Supersedes | None; this is the product-level roadmap |
| Controlled copy | The version committed at the repository root |

## Purpose and status language

This is the controlling product roadmap for Atlas Workbench. I use it to
record what the product already does, what I have committed to deliver next,
and the evidence required before I call a milestone complete. Detailed design
packs and experimental decision logs remain authoritative for their narrower
subjects, but they do not replace this product-level view.

Status values have these meanings:

- **Complete** — implementation and proportionate verification are recorded.
- **In progress** — implementation is authorized and under active delivery.
- **Planned** — scoped, but not yet started.
- **Gated** — no delivery commitment until the named dependency is satisfied.
- **Retired** — deliberately removed or superseded; retained for history.

## Current product state

As of 2026-08-12, Atlas Workbench is a local-first banking
operational-risk foresight and scenario-analysis system with:

- a provenance-gated, human-reviewed evidence and emerging-risk workflow;
- an eight-phase pipeline covering intake, monitoring, scenario generation,
  prioritization, pattern detection, maintenance, compound joining, and
  compound graduation;
- deterministic compound scoring, score-realization history, falsification
  harnesses, and explicit separation from legacy urgency scores;
- proposal queues and review decisions that preserve reviewer identity,
  rationale, and audit history;
- a loopback-only dashboard with scenario, radar, compound, review,
  evaluation, and cost views;
- an optional, pinned, human-reviewed, display-only MITRE ATLAS overlay with
  accountable mapping history; and
- project-native banking-risk, external-driver, and signal-maturity
  vocabularies.

The product does not claim calibrated forecasting, autonomous incident
attribution, regulatory compliance, or affiliation with cited standards
bodies. Live research stores remain local runtime artifacts and are excluded
from public releases.

## Completed workstream: minimum MITRE ATLAS overlay

**Workstream status:** Complete

**Decision record:** [ADR-016](../decisions/016-mitre-atlas-reviewed-overlay.md)

**Controlled delivery plan:**
[MITRE ATLAS Minimum Integration — Implementation Plan](plans/MITRE_ATLAS_MINIMUM_IMPLEMENTATION_PLAN.md)

I added MITRE ATLAS as an optional, versioned, human-reviewed,
display-only overlay on scenarios. It is not a replacement for the
Atlas Workbench Risk Taxonomy. An approved mapping means only that a
reviewer judged an ATLAS technique useful for analyzing the scenario; it is
not an assertion that an adversary used the technique or that a related ATLAS
case study is evidence for the scenario.

| Milestone | Status | Deliverable | Acceptance evidence |
|---|---|---|---|
| M0 — Decision and scope | Complete | Saved implementation plan, architectural boundary, licensing posture, and ADR | Plan and ADR identify the overlay as optional, pinned, reviewed, non-scoring, and non-attributive |
| M1 — Catalog foundation | Complete | Additive framework tables plus a local-file MITRE ATLAS v6 importer and status/activation CLI | Synthetic and official v6 releases import atomically; exact source hash, content/format versions, and sealed state are stored; malformed, amplified, or conflicting inputs fail closed |
| M2 — Governed mappings | Complete | Manual scenario-to-technique proposals, reviewer decisions, scenario hashing, monotonic revisions, and stale-state detection | No proposal self-applies; database transitions require reviewer and reason; edits, restoration, and recreation keep prior approvals stale; scoring output is unchanged |
| M3 — API and dashboard | Complete | Bounded technique search, proposal endpoint, Review integration, and an approved-mapping card in the Atlas Brief | Loopback/Origin controls hold; only approved mappings display; current/stale state is explicit and stale mappings are not treated as current; upstream text is escaped; case studies carry a non-evidence warning |
| M4 — Documentation and release gate | Complete | Operator workflow, taxonomy distinction, ERD, security controls, third-party notices, data-license boundary, and regression verification | The documented official import succeeds; the full suite, CI-equivalent checks, package build, and clean-clone history gate pass |

### Minimum-release acceptance gate

I will not mark this workstream complete until all of the following are true:

- [x] An operator can import one explicitly selected official ATLAS v6 YAML
  file without application-startup network access.
- [x] Re-importing identical bytes is idempotent, while a different payload
  claiming an existing content version is rejected.
- [x] Older imported releases and mappings remain readable after activation
  of a newer release.
- [x] A manual technique mapping traverses pending → approved or rejected
  through the existing human review boundary.
- [x] Each approved mapping retains the ATLAS content version, source hash,
  scenario hash, rationale, reviewer, reason, and decision time.
- [x] A changed scenario makes an older approval stale rather than silently
  current.
- [x] Only approved mappings appear as scenario context; current/stale state
  is explicit, and stale mappings are not treated as current coverage.
- [x] ATLAS tactics, mitigations, maturity, platforms, and related case
  studies are derived from the mapping's pinned release.
- [x] The interface states that applicability is hypothesized and that
  associated case studies are examples, not scenario evidence.
- [x] Imported framework content never changes likelihood, impact, urgency,
  compound generation, graduation, or any other score.
- [x] Imported descriptions are treated as untrusted display data and are
  never inserted into model prompts.
- [x] License, copyright, trademark, attribution, and non-endorsement notices
  are retained.
- [x] The existing test suite, new framework tests, and public-release gate
  pass.

### Verification evidence — 2026-08-12

- Full suite: 737 collected, 734 passed, 3 optional live-OMLX probes skipped.
- Offline integration verification, source compilation, scout-registry
  validation, crash-class/F841 lint, worktree release gate, and isolated wheel
  plus source-distribution build passed.
- Official `2026.07` v6 smoke import verified the pinned SHA-256, 16 tactics,
  178 techniques, 37 mitigations, 68 case studies, 1,196 relationships,
  activation, and sealed append rejection.
- A single-branch clean clone passed the worktree and reachable-history release
  gate; only the feature branch and its remote-tracking ref were present.

### Explicitly outside the minimum

The first release excludes automatic downloads and scheduled refresh,
MITRE ATT&CK integration, NIST AI RMF crosswalks, automatic or LLM-generated
mappings, incident attribution, a Navigator clone, coverage scoring, control
effectiveness scoring, and scenario generation from the ATLAS catalog.

## Gated and future product work

| Workstream | Status | Gate or next decision |
|---|---|---|
| Scoring calibration claims | Gated | Evidence that levels generalize beyond synthetic regimes; current ranking evidence does not establish calibrated probabilities |
| Multi-user/server storage | Planned | Re-plan only after a demonstrated collaboration need; local SQLite remains the product default |
| Automatic source and framework refresh | Planned | Threat model, pinning policy, rollback design, and operator approval UX |
| Broader framework crosswalks | Planned | Evidence that the MITRE ATLAS minimum is used and maintainable before adding ATT&CK or NIST mappings |
| Multiverse search loop and console | Retired | Reintroduce only with new evidence that batch scoring cannot meet a concrete product need |

## Related plans and decision records

- [Redesign v1 roadmap](redesignv1/roadmap.md) is the historical
  experiment and decision log for the scoring program. It records completed,
  failed, gated, and removed work; it is not a current delivery promise.
- [Redesign v1 evaluation plan](redesignv1/evaluation-plan.md) controls
  the scoring program's pre-registered tests and kill criteria.
- [Model inventory](redesignv1/model-inventory.yaml) records the current
  research-tier model restrictions and evidence links.
- [Technical ADR index](../decisions/README.md) controls enduring
  architectural decisions.
- [Public release checklist](../release/PUBLIC_RELEASE_CHECKLIST.md) remains the release
  boundary for fixtures, provenance, licensing, secrets, and history.

## Governance

- I update milestone status only when its acceptance evidence exists in the
  repository or in a named, reviewable runtime artifact.
- I append material scope, status, or acceptance changes to the history below;
  I do not silently rewrite the rationale for a past decision.
- I give this roadmap a new document version when I change commitments,
  acceptance gates, ownership, or governance. Editorial corrections may keep
  the current version but still update `Last reviewed`.
- I record enduring architecture choices in ADRs and link them here rather
  than embedding competing decisions in this file.
- I keep upstream-framework versions separate from this roadmap's document
  version and from the project-native taxonomy versions.

## Change history

| Date | Version | Author | Change |
|---|---:|---|---|
| 2026-08-12 | 1.1 | Project maintainer | I marked M4 and the minimum MITRE ATLAS workstream complete after the documented official import, full regressions, package build, and clean-clone reachable-history gate passed. |
| 2026-08-12 | 1.0 | Project maintainer | I created the controlled product roadmap, recorded M1–M3 as verified, and retained M4 as in progress pending the clean-clone release gate. |
