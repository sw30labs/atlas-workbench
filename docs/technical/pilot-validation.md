# Enterprise pilot and source-quality instruments

These instruments support a prospective decision about usefulness. They do not
establish that Atlas discovers enterprise blind spots, supplies calibrated
operational probabilities, or is ready for deployment. No pilot results are
claimed in this document.

## Research-quality diagnostics

`evidence_quality(settings)` reads the public evidence, risk and scenario
stores without migrations, network calls or organization-context access. The
dashboard can expose it through a dedicated diagnostics endpoint. Optional
arguments are `as_of`, `declared_regions` and `stale_days` (default 180).

The response includes:

- Publisher-label and source-host concentration, grouped record IDs, missing
  metadata, largest share and a descriptive concentration index.
- Repeated `content_fingerprint` and canonical-URL groups for human review.
- Publication, retrieval and event dates as separate concepts, with missing,
  invalid, future and older-than-threshold counts.
- Approved records with old or missing review dates, and risk/scenario
  references that are missing, unapproved or malformed.
- Recorded regions compared with explicitly declared region labels. Without a
  declared scope, missing-region coverage remains `not_declared`.

**Source independence remains `not_validated`.** A new publisher label, host or
URL does not mean a new independent incident, independent publisher ownership,
or independent corroboration. Three syndicated reports of one incident must
not become three operational loss events. Subdomains are distinct host labels
in this descriptive view; no corporate ownership or public-suffix inference is
performed. Common-event/source-family resolution requires an explicit review.

The existing `evidence_strength` field is a citation-count classification. Its
human-facing name is **Citation breadth**. Diagnostics preserve the legacy
field and expose a `citation_breadth` alias. A stored value of `strong` means
broad citation coverage under the existing rule, not reliable causal evidence
or independent confidence. This naming change does not alter pipeline scores
or mutate existing records.

`source_date` is publication date and `retrieved_at` is acquisition date. The
current evidence schema does not record a separate event date; diagnostics
report that missing dimension rather than infer it from publication. A fresh
retrieval must not make an old source look newly observed. The configurable age
threshold suggests review; historical evidence does not automatically become
false when it ages.

An unavailable evidence database is reported as unavailable. Its references
are `not_checked_evidence_unavailable`, rather than falsely asserted to be
broken. Source URL reachability is also `not_checked`; the diagnostics do not
open sources or send record data to an external service.

## Pilot design before measurement

1. **Define the intended decision.** Choose named critical services and specific
   decision makers. Record what successful discovery would change: a control
   test, dependency review, contingency exercise or remediation assignment.
2. **Freeze strong comparators and time cutoffs.** Use the organization's real
   risk register and normal analyst workflow as the baseline. Preserve source
   and register snapshots available at each cutoff. Give both arms comparable
   information access, analyst time and brief format. Do not compare Atlas only
   with randomly generated titles or an artificially weak baseline.
3. **Choose cases independently of model outputs.** Include known incidents,
   plausible near misses and negative cases where no useful new exposure is
   expected. Include shared-provider and cross-service dependencies. Avoid
   counting several reports of one underlying incident as independent cases.
4. **Record exposure scope.** Map candidate claims to a declared service,
   dependency/control assumption and observable consequence. Keep private
   organization material in the opt-in context workspace and controlled pilot
   files; do not feed it into the public research pipeline by default.
5. **Blind the assessment.** Remove arm-identifying branding and standardize
   length and formatting. Randomize presentation order. Use at least two
   independent qualified reviewers. Keep reviewer identity and qualification
   mapping under operator control; the instrument stores stable reviewer IDs.
6. **Collect disagreements, rejection reasons and effort.** Capture every
   preselected case and reviewed candidate, including failures, duplicates and
   missing assessments. Do not drop awkward cases or tune acceptance thresholds
   after observing the held-out results. Material changes start a new versioned
   pilot rather than reusing the same holdout as an independent test.
7. **Follow decisions through.** Record whether an owner accepts a finding,
   performs a test and closes an action with evidence. A generated scenario
   count is not a business outcome.

Use enough independent cases for the uncertainty the decision requires. A
small pilot can reveal workflow problems and useful counterexamples; it does
not establish statistical reliability simply because many candidates or rater
votes were collected. Pre-register sampling and uncertainty analysis separately
if a formal claim will be made.

## Reviewer rubric

Review each candidate independently:

| Field | Positive assessment means |
|---|---|
| useful | The claim would materially improve investigation or preparation for the scoped service. |
| novel | The specific exposure or challenged assumption was absent from the frozen baseline, not merely rewritten. |
| actionable | A named owner could undertake a specific bounded investigation, test or mitigation. |
| duplicate | The candidate repeats an already represented exposure without a decision-relevant difference. |

Missing values mean not assessed, not false. Useful-and-novel is evaluated as a
combined judgement within each review. Candidate rates use majority review
votes; ties remain unresolved and are reported in the denominator diagnostics.
The instrument does not silently break ties or count rater votes as separate
discovered exposures.

Measure `time_to_brief_minutes` once per case and arm with the same start/end
definition. For example, include evidence retrieval, drafting, checking and
preparing the reviewable brief; record setup and exceptional delays separately.
Repeated rater rows may carry the same case time, but conflicting values are
rejected and duplicate rows do not multiply elapsed time.

## Input contract and execution

The pure function `assess_pilot(records)` accepts a list of reviewer records.
The command below reads a supplied JSON list and writes a report to stdout:

```bash
python -m contingency_atlas.diagnostics pilot --input pilot-observations.json
```

Required record fields are `case_id`, `candidate_id`, `arm` (`baseline` or
`atlas`) and `reviewer_id`. Set `blinded` explicitly to true only when the rating
was blinded. Optional assessments are strict JSON booleans: `useful`, `novel`,
`actionable` and `duplicate`. Omit unassessed fields or use null. Optional
`time_to_brief_minutes` is a finite non-negative number.

This fictional input illustrates the format, not a measured pilot result:

```json
[
  {
    "case_id": "fictional-case-01",
    "candidate_id": "fictional-candidate-01",
    "arm": "atlas",
    "reviewer_id": "fictional-reviewer-a",
    "blinded": true,
    "useful": true,
    "novel": null,
    "actionable": true,
    "duplicate": false,
    "time_to_brief_minutes": 30
  }
]
```

Duplicate arm/case/candidate/reviewer rows are rejected. Arm-level rates report
resolved, missing/tied and total candidate counts. Comparative deltas use
matched cases with declared blinded ratings; unpaired or unblinded data cannot
produce a blinded-comparison result. Cases have equal weight in paired rate
deltas. Time savings use paired case durations. Reviewer agreement is
descriptive pairwise agreement with the number of comparable rating pairs; it
is not a chance-adjusted reliability coefficient or independent sample count.

## Forecast scoring requires later observed outcomes

Ranking and expert preference cannot establish probability calibration.
Optional forecast fields are:

- `forecast_probability`: probability of a precisely defined binary target;
  `target`, `forecast_at`, `outcome` (0 or 1), `outcome_at`, `outcome_source` and
  `outcome_status: "observed"` are needed for a Brier score.
- `prediction_interval`: two numeric outcome bounds, plus `interval_level`,
  `target`, `units`, `observed_value`, `observed_value_source`, `forecast_at`,
  `outcome_at` and `outcome_status: "observed"`, for empirical numeric prediction
  interval coverage.

Forecast timestamps must precede the outcome, and outcomes cannot postdate the
report cutoff. Synthetic, unresolved, unsourced or undated outcomes are excluded
with reasons. Facts repeated across reviewers count once per candidate; conflicting
facts are rejected. Brier scores are grouped by target. Interval coverage is
grouped by target, units and nominal level.

Do **not** put a probability confidence interval into `prediction_interval` and
compare it with a realized 0/1 outcome. That does not measure whether the
interval covers a true probability. The numeric interval instrument measures
prediction intervals for directly observed quantities, such as recovery time.
Observed outcome sources are operator-supplied attestations; this local tool
does not independently verify those documents.

## Pre-register acceptance gates

Select concrete thresholds, responsible approvers, evaluation dates and a
minimum complete-case requirement before opening blinded results. Possible
gate categories are:

| Gate | Pre-registered decision rule should address |
|---|---|
| Incremental discovery | Required paired improvement in useful novel exposures over the real baseline, with a tolerable unresolved fraction. |
| Signal quality | Maximum duplicate rate and minimum actionability under the agreed rubric. |
| Workflow | Required reduction in time to a reviewed brief, including checking effort and integration overhead. |
| Review reliability | Acceptable disagreement, an explicit adjudication process and sufficient independent reviewers/cases. |
| Operational consequence | Required owner acceptance, completed tests or evidence-backed actions by the follow-up deadline. |
| Quantitative claims | Separate, outcome-based calibration criteria; no probability claim may borrow a ranking pass. |
| Governance | Traceable sources and decisions, private-data boundaries, reproducible output and accountable owners. |

There are no universal thresholds implied by this table. A regulated
operational-risk pilot should agree its own materiality and review process.
The report always retains `validation_status: "not_validated"`: it supplies
descriptive measurements, not an automatic approval or a significance test.
An independent recorded decision should explain whether the pre-registered
gates were met and what remains unproven. Failed gates and negative findings
are useful outcomes; they should guide revision or stop an unjustified rollout.
