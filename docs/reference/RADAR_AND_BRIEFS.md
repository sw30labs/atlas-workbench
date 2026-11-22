# Radar, Signposts, and Atlas Briefs

Contingency Atlas includes an optional horizon-scanning layer around its
existing banking operational-risk workflow. The extension helps researchers
notice broad external change, describe how it could reach a banking capability,
and revisit a scenario when observable evidence changes.

It does not turn scenarios into predictions. Radar labels, maturity judgments,
evidence-strength labels, and signpost assessments remain research hypotheses
for human review.

## Design boundary

The radar is deliberately split into two parts:

1. **Scouting proposes public sources.** An opt-in command reads a bounded,
   project-maintained registry and produces proposal packets. It never inserts
   evidence, approves a record, or invokes the eight-phase pipeline.
2. **The governed pipeline evaluates approved research.** Existing provenance
   and human-review boundaries still determine which evidence and emerging
   risks may inform generation or analysis.

This preserves the core flow:

```text
Optional scout
    |
    v
pending source proposals
    |
    v
human selection and independent research
    |
    v
existing provenance-gated eight-phase workflow
```

## External-driver overlay

The Contingency Atlas Risk Taxonomy answers, “What banking risk domain is
affected?” The external-driver overlay answers, “What kind of change is
applying pressure?” This project-native taxonomy is separate from the optional
MITRE ATLAS external-framework overlay.

The project-native driver labels are:

| Driver | Scope |
|---|---|
| `Technology` | New or changing technical capabilities, architectures, automation, and infrastructure |
| `Institutional change` | Changes to laws, regulation, standards, market structures, organizational rules, and accepted practices |
| `Demographic change` | Population size, age, location, migration, skills, household structure, and workforce composition |
| `Environment` | Climate, weather, energy, water, pollution, natural hazards, and other physical-system changes |
| `Societal values` | Changes in expectations, behavior, trust, legitimacy, adoption, and attitudes |
| `Cross-driver convergence` | Several co-equal external drivers whose interaction matters more than any single driver |

`external_drivers` is multi-valued and does not replace `risk_category`,
`event_driver_class`, or `primary_cause_class`.

## Signal lifecycle

Emerging risks can carry a maturity and momentum assessment:

```text
weak -> emerging -> accelerating -> established -> materialized
   \          \            \              \
    +----------+------------+---------------> fading
```

Allowed maturity values are `weak`, `emerging`, `accelerating`, `established`,
`materialized`, and `fading`. Allowed momentum values are `accelerating`,
`stable`, and `slowing`.

Evidence strength is a conservative label derived from the number of distinct
cited references:

- `unrated`: no supporting reference is available;
- `limited`: one supporting reference;
- `moderate`: two supporting references; and
- `strong`: three or more supporting references.

The label describes citation breadth, not truth, source quality, independence,
probability, or confidence. A reviewer still owns source assessment.

## Transmission path

Generated scenarios make the route from signal to potential disruption
explicit:

```text
external signal
    -> affected banking capability or dependency
    -> transmission mechanism
    -> control or service under pressure
    -> first-order impact
    -> second-order effects
```

The structured fields are:

- `affected_capabilities`
- `transmission_mechanism`
- `second_order_effects`
- `cross_industry_analogues`
- `challenged_assumptions`

These fields help reviewers distinguish a plausible banking mechanism from an
interesting but irrelevant general trend.

## Signposts and retrospective review

Every generated scenario can identify:

- `strengthening_signposts`: observations that would make the scenario more
  relevant;
- `weakening_signposts`: observations that would reduce its relevance; and
- `option_expiry_signals`: observations suggesting that a useful response is
  becoming harder or more expensive.

Maintenance can compare newly approved evidence summaries with those signposts
and record a transparent assessment:

- `strengthened`
- `unchanged`
- `weakened`
- `materialized`
- `superseded`

Before evaluation, the stored status is `not_evaluated`.

The assessment records matching evidence references and an explanation. It
does not change source review status, declare causation, or claim that the
scenario was correct. `materialized` and `superseded` require especially direct
approved evidence and still remain assessments, not automatic lifecycle
actions. Model-assisted assessments carry a separate
`signpost_review_status: pending` marker until a human reviews them. Retirement
and merge actions remain separately gated by the existing operator control.
Reviewers can list and decide pending interpretations with:

```bash
python scripts/review_records.py list --kind signpost
python scripts/review_records.py approve-signpost SCENARIO_ID
python scripts/review_records.py reject-signpost SCENARIO_ID \
  --reason "Assessment overstates the cited evidence"
```

Approval or rejection changes only the review marker and retains the proposed
interpretation for audit; it never changes the scenario lifecycle.

Only active scenarios are evaluated; legacy rows with a null lifecycle status
are treated as active. Draft and retired scenarios are never placed into the
signpost-review queue.

Every evaluation is appended to the immutable `signpost_assessments` history
table. The scenario columns are only the latest dashboard projection:
`signpost_evidence_refs` contains the exact references cited by that current
assessment, while `signpost_checked_evidence_refs` remains the cumulative
checkpoint of every approved record considered. This prevents repeat
processing without mixing citations from different assessments. Each history
row also has a durable `origin`; migration-created rows are marked
`legacy_projection_backfill`. Older history whose original source can no
longer be reconstructed is marked `legacy_unknown`; neither provenance label
can be overwritten by a later review decision.

Approval always targets the latest pending history row. Before accepting it,
the review workflow confirms that the projection still matches the immutable
snapshot, exact references are a subset of the considered packet, and every
reference still resolves to an approved evidence record. The history decision
and projection marker are committed atomically. If a maintenance action retires
or merges a scenario in the same cycle, any pending assessment is superseded
atomically and cannot remain in or re-enter the review queue.

## Preparedness and opportunity

Scenarios retain preparation strategies and can additionally separate:

- `defensive_options`: ways to reduce exposure or contain loss;
- `resilience_opportunities`: ways to improve recovery, adaptability, or
  observability; and
- `strategic_options`: reversible choices that preserve room to act as the
  situation changes.

This is operational-resilience analysis, not investment or product advice.

## Atlas Brief

The dashboard can render each scenario as an Atlas Brief for a short team
discussion. A brief brings together:

- what may be changing;
- why the scenario matters to banking;
- affected capabilities and the transmission mechanism;
- assumptions being challenged;
- strengthening and weakening signposts;
- defensive, resilience, and strategic options; and
- scenario-specific discussion prompts.

The brief is a presentation of existing structured research. It does not
silently add evidence or turn model-generated text into fact.

## Innovation radar visualization

The dashboard radar uses:

- radial ring: signal maturity;
- angular segment: external driver;
- color: Contingency Atlas banking risk category; and
- marker size: evidence strength or related scenario urgency.

Legacy records without these fields are shown with conservative defaults rather
than rewritten.

## Method note

The separation between broad external drivers and domain-specific consequences
is informed by common horizon-scanning practice, including the publicly
described future-scenarios and strategic-imagination work of
[TomorrowToday Global](https://tomorrowtodayglobal.com/imaginethefuture/).
Contingency Atlas uses independently authored labels, schemas, prompts, code,
and synthetic examples. It does not incorporate TomorrowToday scenarios,
toolkit content, or proprietary material, and no affiliation or endorsement is
implied.
