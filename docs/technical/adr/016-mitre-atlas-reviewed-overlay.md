# ADR-016: MITRE ATLAS is a versioned, reviewed, non-scoring overlay

- **Status:** accepted
- **Date:** 2026-08-12

## Context

Contingency Atlas has a compact project-native banking operational-risk
taxonomy, an external-driver overlay, and a governed scenario workflow. It
recognizes AI and model risk in broad operational terms, but it does not carry
the adversary-behavior vocabulary, technique identifiers, mitigations, or case
study relationships maintained by MITRE ATLAS.

That creates two needs which must not be conflated. A scenario needs a stable
banking-risk classification to explain the operational consequence, while an
AI-security analysis may also need to describe how an adversary could act.
Replacing the native taxonomy with MITRE ATLAS would discard the first need;
mixing ATLAS fields into scenario scores would turn a qualitative mapping into
unsupported quantitative evidence.

The two products also share the word “Atlas.” Without an explicit boundary,
users could mistake the independently authored Contingency Atlas taxonomy for
MITRE content, infer compatibility that has not been reviewed, or treat an
ATLAS case study as evidence that a hypothetical banking scenario occurred.

## Decision

I will integrate MITRE ATLAS as an optional external-framework overlay with
the following invariants:

1. The official v6 YAML is imported only from an operator-supplied local file.
   Each import records the ATLAS content version, format version,
   operator-asserted source URL, exact-byte SHA-256, and import time. An
   optional expected digest verifies the bytes before parsing. Application
   startup performs no ATLAS network request. A successful release is sealed
   before activation so later SQL cannot append objects or relationships to
   the snapshot identified by that digest.
2. Framework releases, objects, and relationships are stored separately from
   scenarios. Older releases are retained, and each mapping remains pinned to
   the release against which it was reviewed.
3. Scenario-to-technique applicability begins as a manual pending proposal.
   Approval or rejection uses the existing human review boundary and requires
   reviewer identity and a written reason. A proposal cannot self-apply.
4. The mapping records both a hash and a monotonic revision of the scenario's
   meaning-bearing fields. A later change or delete/recreate makes the approval
   stale and requires a new review; restoring identical text does not silently
   reactivate it, and no transition rewrites history.
5. Only approved mappings may display technique context and relationships to
   tactics, mitigations, platforms, maturity labels, and case studies. Current
   or stale state is explicit; stale approvals remain visible as audit context
   but are excluded from current coverage until re-reviewed. Applicability is
   labeled hypothesized rather than observed or attributed. Related ATLAS case
   studies are examples associated with a technique, not evidence for the
   Contingency Atlas scenario.
6. Imported ATLAS content and mapping state never enter likelihood, impact,
   urgency, compound generation, graduation, or any scoring path. Imported
   descriptions are untrusted display data, are escaped in the browser, and
   are not inserted into LLM prompts.
7. `atlas-1.0` remains the stable identifier for the **Contingency Atlas Risk
   Taxonomy**. External coordinates use `mitre-atlas/<content-version>`; ATLAS
   technique maturity is never conflated with Contingency Atlas signal
   maturity.
8. The product preserves upstream copyright and Apache-2.0 terms, identifies
   MITRE ATLAS™ only to describe interoperability, and states that MITRE has
   not affiliated with, sponsored, approved, or endorsed this project.

The controlled delivery and acceptance detail is in the
[MITRE ATLAS minimum implementation plan](../../plans/MITRE_ATLAS_MINIMUM_IMPLEMENTATION_PLAN.md).

## Consequences

- AI-specific adversary behavior can be communicated in a shared external
  vocabulary without weakening the banking-risk model.
- A reviewer can trace a displayed mapping to the exact upstream bytes and
  the exact scenario state considered at decision time.
- A content-restored or deleted-and-recreated scenario remains stale against
  an older approval because currency requires both content hash and monotonic
  revision to match.
- Importing a new ATLAS release does not silently change historical analysis;
  upgrades may expose mappings that need deliberate re-review.
- The integration adds catalog, relationship, proposal, and decision state to
  the local SQLite store and adds an operator-run import step.
- The overlay can be absent without degrading the existing pipeline or
  dashboard. No imported release means no ATLAS context, not an application
  error.
- The UI must carry more caveat language than a conventional taxonomy tag,
  because technique relevance, adversary attribution, and case-study evidence
  are materially different claims.

## Alternatives considered

- **Replace the native taxonomy with MITRE ATLAS** — rejected because ATLAS
  describes adversary behavior, not the banking operational consequence the
  project taxonomy is designed to classify.
- **Add technique IDs directly to scenario rows** — rejected because this
  loses release provenance, normalized relationships, decision history, and
  stale-state detection.
- **Automatically map scenarios with keywords or an LLM** — rejected for the
  minimum because plausible text similarity is not accountable applicability
  judgment. Automation can be reconsidered later only as proposal assistance.
- **Download the latest ATLAS release at startup** — rejected because a moving
  dependency would make runs non-reproducible and add an unnecessary network
  and supply-chain path.
- **Feed approved mappings into scores** — rejected because ATLAS does not
  provide calibrated banking likelihood or severity evidence.
- **Copy related case studies into scenario evidence** — rejected because an
  upstream example linked to a technique does not substantiate a separate
  scenario.
