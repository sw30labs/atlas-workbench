# ADR-017: Unified draft-first research intake and approved-only scenario planning

- **Status:** accepted
- **Date:** 2026-08-30
- **Tracks:** [Issue #10](https://github.com/sw30labs/atlas-workbench/issues/10)

## Context

The original Research Cycle form exposed four parallel inputs: evidence text,
evidence URLs, risk-intelligence text, and risk URLs. Phase 1 correctly refused
to ingest a batch unless every evidence block had one positionally aligned
public-source URL, but the form made the operator construct that positional
data structure by hand. It also asked the operator to decide in advance which
material was evidence and which material was a risk signal.

Those are different governed objects, but they are not mutually exclusive. An
evidence event is a source-grounded observation. An emerging risk is a research
hypothesis that must link to evidence before approval. The same source passage
may therefore yield an evidence draft and a linked risk-hypothesis draft.
Requiring four prepared fields pushed classification, pairing, and bookkeeping
onto the user without making the provenance boundary safer.

[ADR-006](006-review-first-ingestion.md) introduced a non-mutating artifact
converter and completeness rubric, but its result still pre-filled the legacy
parallel fields one mode at a time. The dashboard also asked for a scenario
count before new intake had crossed Review. That suggested a relationship that
must not exist: pending material cannot influence generation, scoring, pattern
analysis, or a recommendation about how many scenarios to generate.

The first implementation slice must also respect the existing evidence
identity rule. `atlas:sourceUrl` is inverse-functional in the ontology and the
evidence store treats a repeated canonical source URL as the same evidence
identity. Whether one document should instead contain several child claims or
events is a separate schema and ontology decision; unified intake must not
silently change it.

## Decision

### 1. One input surface prepares a draft bundle

The primary dashboard intake is one free-form **Add Research** field. It may
contain mixed prose, notes, and literal public HTTP(S) URLs in any order. The
operator does not construct parallel arrays or label the input as evidence or
risk intelligence before preparation.

Preparation is a read-only graph:

```text
free-form material
  -> bounded units + literal URL harvest
  -> evidence and linked-risk draft extraction
  -> deterministic record/provenance checks
  -> semantic QA
       revise -> bounded repair -> semantic QA
       pass | hold
  -> exact + near-duplicate preflight
  -> ready / needs attention / already known preview
```

The graph writes no evidence, risk, review, scenario, or run record. It returns
a transient draft bundle with an input digest, candidate identifiers, source
links, input-unit accounting, QA history, duplicate findings, and explicit
holds. Every input unit must be represented by at least one candidate, marked
irrelevant with a reason, or held as unaccounted. An empty or fully irrelevant
input may legitimately produce zero candidates.

Model output is untrusted. Deterministic code clamps it to the existing record
shapes and fixes every candidate's intended review state to `pending`. A model
cannot approve a record, attest source access or reuse rights, invent a
different input unit, or introduce a fetch target.

### 2. Literal URL admission is not a reachability or truth claim

URLs are harvested deterministically from the operator's input. A model may
select only from that allow-list. Each selected URL must pass
`is_public_http_url`, which rejects malformed URLs, credentials, loopback and
private/reserved IP literals, single-label/private-suffix hosts, and non-HTTP
schemes. Canonicalization is applied only after that shape check.

This check establishes **public URL shape**, not reachability. Preparation does
not resolve DNS or fetch arbitrary pasted URLs in this slice. It does not claim
that a URL exists, is canonical, is publicly accessible to the reviewer,
supports the attached statement, or carries acceptable reuse rights. Those
claims remain visible human checks in Review.

Any future availability probe must be a separate advisory signal with pinned
public DNS resolution, redirect-by-redirect revalidation, response-size and
content-type caps, a killable deadline, and no ambient credentials or cookies.
Even a successful response may never substitute for claim-attribution review
or the public-source and rights attestations.

### 3. QA is conditional, bounded, and allowed to abstain

Deterministic checks run before semantic QA. They enforce admitted URLs,
accounted input units, required record shape, one canonical URL for an evidence
candidate, evidence links for a risk candidate, and `pending` review state.
A deterministic failure goes directly to `hold`; another model turn cannot
repair missing provenance.

The semantic judge returns one of `pass`, `revise`, or `hold` with issues.
`revise` follows a conditional edge to a repair step and then back to the
judge. The repair step may revise wording or classification only within the
candidate's original input units and literal-URL allow-list. A judge or repair
failure, unavailable model, invalid result, exhausted budget, or non-repairable
provenance problem ends in `hold`, never an optimistic pass.

Input, unit, candidate, token, and iteration caps bound the work. The initial
policy accepts at most 200,000 input characters, 100 input units, 100
candidates, and five QA iterations per candidate (three by default). Quality
is expressed through evidence-forced checks and issue history rather than an
opaque confidence percentage. The graph succeeds when it accounts honestly
for its input, including zero results and unresolved holds; it is not required
to manufacture an ingestible record.

### 4. Duplicate identity remains conservative

Duplicate preflight compares the prepared bundle with the governed corpus and
with earlier candidates in the same bundle.

- An exact canonical evidence URL, normalized evidence identity, or exact risk
  identity is shown as **already known** and held from submission. In this
  first slice, at most one new evidence event per canonical source URL may be
  queued, preserving the current store and ontology identity contract.
- Deterministic token similarity may identify a **possible duplicate**. This is
  advisory. It stays visible with the similarity reasons and may narrow a
  later scenario-planning range, but it never merges, suppresses, approves, or
  rejects a distinct governed record.
- A semantic risk merge continues through the pending merge-proposal and human
  decision path in ADR-014. Unified intake does not create a second merge
  authority.

If the project later needs several independently governed events from one
source document, it must first choose between a source-document/child-claim
model and a revised evidence identity rule, migrate existing data, update the
ontology, and ratify that change separately.

### 5. Submission is selective and preserves Review

The preview groups candidates as **ready**, **needs attention**, and **already
known**, with per-item source, QA, linkage, and duplicate details. Ready
candidates are independently selectable; deselected candidates and held items
remain transient and are not written. An exact duplicate is not selectable as
a new record. A near-duplicate warning does not become an automatic identity
decision.

Submission revalidates the selected candidates against current state. A risk
hypothesis is submit-eligible only if its linked evidence is selected in the
same submission or already exists in the governed evidence store. Independent
candidate groups may be submitted partially, but the response must report a
per-item queued, held, or already-known outcome; a partial result must not be
presented as complete success. A dependency failure may not leave a risk that
claims a missing evidence link.

Every admitted evidence event and risk hypothesis enters as `pending` with its
model-assisted provenance. The existing Review workflow remains the sole path
to `approved`: a human checks the source and claim, explicitly attests public
access and reuse rights, and approves or rejects the record. Pending or
`needs_update` material remains outside every analytical consumer.

### 6. Scenario planning is deterministic, approved-only, and optional

Scenario planning is separate from intake preparation and submission. Its
recommendation is computed without an LLM from:

- evidence whose review state is `approved` or `approved_synthetic`;
- active approved risks for which **every** evidence reference resolves to
  approved evidence; and
- active scenarios and their explicit or normalized core-risk coverage.

Prepared drafts, newly queued pending records, `needs_update` records, rejected
records, faded/merged risks, and inactive scenarios do not contribute. The
recommendation therefore does not change merely because the operator pasted or
queued material. It becomes eligible only after Review, and planning is then
recomputed from the approved corpus.

Let `U` be the number of approved, evidence-backed risk atoms not represented
by an active scenario. The initial explainable policy is:

```text
lower bound     = ceil(U / 3)
recommended     = ceil(U / 2)
upper bound     = U
all values      = capped to the operator/run maximum, never above 10
```

Conservative risk and scenario duplicate candidates can narrow the exploratory
upper bound, but they do not silently collapse identities or lower the central
recommendation. The result reports covered and uncovered risk IDs and
categories, unresolved risk exclusions, duplicate candidates, corpus counts,
and plain-language rationale. Zero is a first-class recommendation when no
eligible gap exists or the configured cap is zero.

The operator may accept or override the recommendation within `0..10` and must
explicitly trigger generation. A recommendation, including a non-zero one,
never starts a scenario run by itself.

## Consequences

- Operators can paste material in the form they have, while the application
  takes responsibility for proposing structure and exposing its reasoning.
- Evidence observations and risk hypotheses retain different meanings and can
  be linked without forcing a false either/or classification.
- Literal URL clamping and deterministic validation prevent model-invented
  provenance from becoming a green check.
- Holds, ignored units, exact duplicates, partial submissions, and zero
  recommendations are visible outcomes rather than silent failures.
- Review, attestation, and approved-only analysis boundaries remain unchanged.
- Preparation may take longer than a simple form submission because extraction
  and semantic QA are model-backed, but the loop is bounded and never mutates
  while it reasons.
- The one-event-per-canonical-source rule remains a known representational
  constraint. Unified intake improves the operator workflow without pretending
  to solve that ontology question.
- Coverage matching is deliberately conservative and may understate or
  overstate a semantic relationship. The rationale and manual override make
  that uncertainty inspectable without putting an LLM in the count policy.
- An optional local Singularity Atlas Neo4j pull may append public-feed
  stories to the same draft field. It is not a second ingest authority: it
  is fail-soft, feed-only, relevance-filtered, de-duplicated by story id/URL,
  and still lands in Review as pending. Archive/Loop editions stay out.

## Alternatives considered

- **Keep the four parallel fields as the primary UI** — rejected because they
  expose an internal transport shape and make the operator perform fragile
  positional pairing. They remain a lower-level programmatic compatibility
  surface.
- **Classify each input unit as either evidence or risk** — rejected because a
  source-grounded observation may also support one or more linked hypotheses.
- **Fetch every pasted URL before preparation** — rejected because URL
  reachability is not provenance and an arbitrary fetcher adds SSRF, redirect,
  content-bomb, credential, and prompt-injection risk.
- **Let the QA loop search for or invent missing citations** — rejected because
  repair cannot create provenance that was absent from the supplied material.
- **Run an unbounded overarching Ralph loop until it finds something** —
  rejected because it turns abstention into apparent failure and creates
  unpredictable cost and latency.
- **Auto-submit or auto-approve passing candidates** — rejected because a model
  rubric cannot make the human source, rights, and factual attestations.
- **Auto-merge semantic duplicates** — rejected because similarity is not
  identity and an incorrect merge corrupts the risk/dependence model.
- **Have an agent choose and immediately generate a scenario count from the
  draft bundle** — rejected because pending material may not influence
  generation and model-authored counts are neither stable nor necessary.
