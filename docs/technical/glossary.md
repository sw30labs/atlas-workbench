# Glossary

**Atom** — one emerging risk (`RiskAtom`): the unit risks are composed
from.

**Compound scenario** — a hypothesis that ≥ 2 atoms co-materialize
within the horizon, with an auditable score vector. A separate object
class from library scenarios and from catalog risks (ADR-004).

**Channel** — a shared causal dependency class (`identity, cloud,
payments, model, credit-chain, physical, energy, legal, third-party,
geopolitical, data`) assigned by deterministic keyword rules; the
blocking structure for candidate generation.

**Holdout survival** — a compound's risk pair co-occurred at least once
in the quarters *after* the scoring window; the deterministic graduation
requirement.

**State hash** — structural identity of a scenario/pair (sorted risk
ids, horizon, definition, graph/registry hashes, algorithm major).
Scores are never hashed (they are derived).

**Realization** — one concrete scoring of a state: `(state_hash, seed,
algorithm_version)` → p̂, interval, quantiles, components.

**Uncertainty class** — `statistical` (all cells data-rich and
dependence from data), `mixed`, or `judgment_dominant` (unused in
production today).

**Evidence lanes (artifact ingestion)** — `literal` (URL present in the
artifact), `llm` (LLM-resolved URL, amber — verify), `unresolved`
(reference without a URL; needs one or gets dropped).

**Rubric check** — completeness verdict from evidence-forced criteria
(missed-sources enumeration, attribution fidelity with quoted spans,
body fidelity, load-bearing facts, deterministic URL validity). Not a
confidence score.

**Associational feature** — an external feature that passed the screen
(pre-registration, FDR, holdout, adversarial null). A modifier
candidate, never causal evidence, never auto-applied.

**R0 / R0′ / R1** — evaluation regimes: R0 synthetic with planted
channels (R0′ = Poisson-count variant), and R1 public/demo store. The
separate `synthetic-cell-demo` exercises grouped-event aggregation and
egress behavior without representing an organizational data source.

**Validated ≠ true** — a scenario that passed promotion gates is
"validated"; that means data support, coherence, novelty, and
calibration checks passed — not that the future will agree.

**Review queue** — the dashboard card where pending evidence and risks
await an attested human decision; the browser front end of the same
audited functions the review CLI uses.

**MITRE ATLAS overlay** — an optional, versioned projection of an
operator-imported MITRE ATLAS catalog plus human-reviewed
scenario-to-technique applicability mappings. It is display-only and never a
scoring input (ADR-016).

**Framework mapping** — a review decision that an external technique is
useful context for a scenario. In the minimum integration, applicability is
always `hypothesized`; it is not observed adversary attribution, and related
framework case studies are not evidence for the scenario.

**Stale framework mapping** — an approved mapping whose stored scenario hash
or monotonic scenario revision no longer matches the current meaning-bearing
scenario state. It remains visible as audit context but is excluded from
current coverage until a new proposal is reviewed. Restoring identical text or
deleting and recreating the scenario does not reactivate the old approval.

**Attestation** — the two explicit human claims required for approval:
cited evidence is publicly accessible, and access/reuse/redistribution
rights were checked. Never machine-fillable (ADR-013).

**Validation report** — the non-raising checklist mirror of the
approval validators, rendered as traffic lights (plus per-ref
resolution for risks) *before* a decision is attempted.

**Advisory pre-analysis** — a judge-model read of one pending record
(recommendation, reasons, concerns, `human_must_verify`), cached by
content hash, rendered as clearly-labeled advice that pre-selects
nothing. For channel proposals the output is explanation-only
(`explanation` + `weaknesses`, no recommendation) — the decision there
is pure human judgment (ADR-014).

**Agreement audit** — the stored pairing of each suggestion with the
eventual human decision; measures drift toward rubber-stamping.

**Deep research** — the bounded plan → search → triage → fetch →
assess → coverage-judge loop that proposes evidence for a blocked
record. Verdicts: `enough`, `no_evidence_found` (first-class),
`budget_exhausted`. A gated variant serves channel proposals: it
requires a fresh advisory explanation first (its weaknesses are the
red flags), files no candidate evidence, and ends by drafting the
reviewer's written reason.

**Candidate evidence** — a deep-research proposal filed as an ordinary
`pending` evidence-event row (`EVT-DR-*`, provenance `origin: deep_research`);
becomes citable only through attested human approval.

**Adopt-refs** — the register-locked, provenance-stamped attachment of
*approved* evidence ids to a *pending* risk's `evidence_refs`; the
governed alternative to hand-editing the register.

**Register lock** — the `flock` sidecar (`emerging_risks.json.lock`)
serializing the register's three writers (review, Phase 2 monitoring,
adopt-refs) across processes.

**pair_id** — stable compound identity: hash of the sorted risk_id set
and nothing else. Survives graph-version churn, slug renames of other
atoms, and estimator swaps; the continuity key analogues, the ledger
and cross-version reporting ride on (A7).

**data_hash** — hash of the fitted signal series + train bounds, part of
realization identity: the same state scored on different data is a
different realization.

**Realization history** — the append-only `score_realizations` table
(no-update/no-delete triggers) under the `compound_scenarios` current
view; estimator/config/data changes are recorded as new rows, never
overwritten. GUI: the per-compound "paper trail".

**Task B-cal** — compound *level* calibration against closed-form
generative truth on R0, stratified by E[N_S|independence] × rarity ×
support (incl. the supported-rare stratum). The promotion instrument
for any dependence challenger (evaluation-plan §3.7).

**Task L** — loss-side evaluation: severity parameter recovery and
holdout log-likelihood fitted vs pooled vs truth (evaluation-plan §3.8).

**Mixture challenger** — the A1 spike-and-slab dependence estimator
(`dependence_mode: "mixture"`): independence spike + diffuse coupling
slab combined by Beta-Binomial marginal likelihoods. Holds zero-support
rare pairs at ~independence while responding decisively to observed
support. Challenger only — never primary until the B-cal gate favors it.

**Judgment ledger** — append-only, hash-chained log
(`generated/judgment/ledger.jsonl`) of judgment claims (analogues,
elicitations) with human decisions and resolutions; calibration is
computed only for resolvable claim types (P7).

**Frozen forecast** — the deterministic realization (p̂, interval,
hashes, train bounds) pinned to a ledger claim at elicitation time;
claims are resolvable only against a forecast frozen *before* the
resolving window.

**Shadow pass** — the join's parallel scoring of an enriched graph
(approved channel proposals) reported in the artifact's `shadow` block
only; production tables and graduation are untouched by construction.

**Merge proposal** — a review-queue item proposing that two register
records are the same underlying risk; approval (single reviewer +
written reason, ADR-014) applies the merge under the register lock.

**Channel proposal** — a review-queue item from the OMLX channel audit
(P1) proposing a channel-graph enrichment (a tag, an edge, or a new
channel); citation URL-clamped, decided by a single reviewer + written
reason (ADR-014). Approval flips the proposal row only — it feeds the
join's shadow pass, never the production graph.

**Refutation memo** — the best case *against* a compound's
co-materialization, shown beside the claim in review; display-only,
gates nothing (P6).
