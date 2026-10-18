# Revision log — WHITEPAPER v1 → v2

**Scope:** paper amendments responding to
[`WHITEPAPER_REVIEW.md`](WHITEPAPER_REVIEW.md) (2026-08-03).
v1 is preserved unchanged as the reviewed artifact
([`WHITEPAPER_v1.md`](WHITEPAPER_v1.md)); v2 is the amended edition.
Code and harness changes recommended by the review are **out of scope for a
paper revision** and are tracked in
[`HYBRID_SCORING_PROPOSAL.md`](HYBRID_SCORING_PROPOSAL.md); v2 cites them as
scheduled work with proposal section anchors, and discloses every underlying
defect in prose.

**Method (per the working mode for deep-review deliverables):** every number
carried into v2 was re-verified against the repo before writing — artifacts
re-read, formulas re-derived, and the harness re-run fresh (`atlas-eval
demo`, `atlas-eval calibrate` on R0, and a newly generated R0′ dataset +
calibration). Where re-verification *refined* a review claim, the refinement
is noted below and v2 states the refined version. An adversarial critic pass
was then run over the v2 draft (see "Post-draft critic pass" at the end).

## Verification deltas vs. the review (2 refinements, 0 contradictions)

1. **R0′ calibration (D1/D10):** the review reported the R0′ NLL win
   (105.377 vs 135.805) and noted no artifact existed. The artifact was
   generated for this revision
   (`generated/redesign/phase3/calibration-r0prime/`) and shows **more**
   than the review recorded: the strict rule passes in full on R0′ — NLL
   105.3765 vs 135.805, Brier 0.1708 vs 0.1766, coverage 0.9167 in band,
   NB family selected 15/15. v1's hedge "coverage still documented as
   over-covering" was therefore also wrong (in the favorable direction);
   v2 §6.3 states the full result.
2. **Task C tie-break (E6.4):** verified precisely: the top-30 cut falls
   inside a five-way lift tie (ranks 30–34, lift 4.0), resolved by
   co-occurrence count and then id lexicography; the resolution moves at
   most ±1 planted pair, and the pass (23 vs 21) does not depend on it.
   v2 §6.2 states this refined version rather than the review's looser
   phrasing.
3. **Task C count-equivalence (E6.3; caught by the critic pass, then
   re-verified):** the review's "ranking by raw co-occurrence count
   reproduces precision 0.767 exactly" holds only under the same
   lexicographic tie-break — only 21 pairs have count ≥ 2, so nine of the
   thirty slots go to count-1 pairs chosen from 33 by tie-break alone, and
   count-ranking spans ≈ 0.63–0.80 across conventions (sometimes beating
   the lift ranking). The direction (lift normalization adds nothing on
   R0) is unchanged and arguably strengthened; v2 §6.2 item 4 states the
   tie-contingent version.

All other checked figures reproduced exactly: Task B table (4-dp), lift
0.8434, B3/B2 = +5.2% AP, Task C 23/30 and 10/11, base rates 30/105 = 0.286
and 26/54 = 0.481, count-ranking = lift-ranking at 0.7667 under matched
lexicographic ties (tie-contingent — see delta 3), R0 calibration
(76.3384/72.4738; 0.186341/0.185418; 0.9667; PIT 0.2333), R1 (78 pairs, 4
positives, lift 0.0, B3 ≡ B1 at AP 0.0352 / AUROC 0.0878), realizations
(105 rows; 15 interval violations; worst 3.06e-4 vs [0, 4.7e-11]; 105×
`evt: null`; 56 with ≥ 200 hits; ~1.5 KB each), config hashes (fresh
6219ecd3…; minus hardening keys = shipped 0e76a450…), egress log (46 rows,
6/40), ext-features registry statuses, and the code-level claims (config
hash fields, realization id, horizon `//4` hashing, unclipped interval
draws, `include_empirical=False` + eventless graph build in the join,
trimmed persistence, `holdout_survival: True` hardcode, `loss or 1.0`,
hardcoded graduation stamps, phase-4 fallback 0.5, A3 rule
`cv_evt ≤ 1.05·cv_emp` over 3 seeds × ≤ 5 pairs, EVT `< 20 excesses`
silent skip, B2 1e-6 tie-break, stale strings in `evaluate.py` and the
scorer docstring).

## Disposition of review findings

| ID | Finding | Severity | Disposition in v2 |
|---|---|---|---|
| D1 | "NLL beats naive on both regimes" false; Brier fail + PIT spike omitted; R0′ artifact missing | must | **Fixed** §6.3: corrected sentence (R0 loses, R0′ passes), Brier + PIT disclosed, R0′ artifact generated and cited (C.2). Explicit "correction of v1's false sentence" paragraph. |
| D2 | Production join persists trimmed rows, not full realizations | must | **Scoped + disclosed** §4, §5.1, §5.5 item 1: §5.1 claim now evaluation-path-only; production gap itemized; full-persist fix cited (proposal §A7). |
| D3 | §3.6 pooling / §3.1 ELD-proxy in present tense; spec ≠ code | should | **Fixed** §3.1, §3.6 (retitled "specified, not implemented"), layer-stack diagram annotated; §3.7 notes `judgment_dominant` unreachable. Subtitle's "hybrid" dropped. |
| D4 | k-of-n "documented sensitivity" not runnable | should | **Fixed** §2: "specified but not implemented; unused `k` slot; a validator cannot run it today." |
| D5 | No stored realization has EVT; stale stub strings in fresh reports + docstring | should | **Disclosed** §3.5 (artifact-status correction), §5.5 item 5, Appendix B caveat 2, C.2/C.3 annotations. String fixes + re-emission stated as scheduled (code-side). |
| D6 | §3.3 interpretation sentence inverts the estimator's limiting behavior | must | **Fixed** §3.3: correct limit (equals independence only at T=0, decays below to κ/(T+κ) ≈ 0.11), with the rare-vs-common distinction and pointer to §7.2. |
| D7 | Nits: noise-feature mechanism; cross-platform bytes; state-hash input list; stdlib caveat; L\|S aggregate definition; abstract attribution | nits | **All fixed**: §6.5 (mechanisms separated), §5.3 + Appendix B (per-platform scope), §5.2 (full input list), §5.3 (PyYAML), §2 (annual-aggregate definition), abstract + §6.2 item 1 (harness attribution). Egress two-rehearsal row counts in §5.4/C.2. |
| D8 | Data coordinate missing from realization identity; history overwritten | should | **Disclosed** §5.5 item 2 with the `data_hash` + append-only fix (proposal §A7). |
| D9 | Shipped GO artifact hash-stale vs today's config surface | should | **Disclosed + used** §6.1 (metrics invariant to the hardening config change — robustness fact softening E3), §5.5 item 5, Appendix B caveat 1 with both hashes. Re-emission left code-side (fresh reports would embed D5's stale strings until those are fixed). |
| D10 | "All in this repository" false — `generated/` gitignored; R0′ artifact absent | must | **Fixed** Appendix C opening rewritten (reproducible ≠ archived; fresh clone has no pack); header table amended; R0′ artifact **generated** (C.2). Committing a frozen pack remains a repo-policy decision, stated as the standing option. |
| D11 | Empirical-edge clause is dead code in production | should | **Disclosed** §4: option never exercised, consequence stated (data cannot nominate candidates); coverage addressed in the proposal via §P1 enrichment — empirical-edge activation itself is unscheduled, and v2 §4 says so (critic finding 3). |
| D12 | interval_draws hash gap; no config in realization id; horizon coarse-hash; slug churn; k≥3 graduation bypass | must | **Disclosed** §5.2 (horizon, slugs), §5.3 (hash coverage; v1's "all hyperparameters" retracted), §5.5 items 3–4 (bypass as "false attestation waiting"), Appendix A hash column. Fixes code-side (§A7; the config-coordinate-in-realization-id item is scheduled nowhere yet — v2 §5.3 says so, critic finding 5). |
| T1 | Rare-pair prior collapse (below-independence in the tail) | must-disclose | **Disclosed at full strength** §3.3 + §7.2 (mechanism, ×0.11 markdown, 0.92 zero-probability at q=0.05, "≈9× below independence", why Task B is blind); abstract sentence; §1 caveat; conclusion retires v1's tail claim. Mixture/common-factor remedy cited (§A1/§A2) with challenger-first promotion gated on E4's task. |
| T2 | 15/105 blobs have p̂ outside its own interval | must | **Disclosed** §3.3 + §7.2 with worst-row numbers and all three mechanisms + marginal-conditioning caveat; fixes cited (§A3). C.2 notes blobs kept as evidence. |
| T3 | Same-quarter-only dependence; lagged cascades invisible | roadmap | **Disclosed** §7.3 with windowed-co-firing sensitivity scheduled (§A4). |
| T4 | Event multiplicity discarded in the loss | roadmap | **Disclosed** §3.4 + §7.4 (compound-Poisson fix, §A5). |
| T5 | Severity blend discontinuity; σ blend; EVT lognormal ceiling; one-sided sensitivity degradation | roadmap | **Disclosed** §3.2 (blemishes), §3.5 (scope-of-claim paragraph: variance-reduction device, ξ<½ validity, one-sided degradation visible in `xi_sensitivity`), §7.4. |
| T6 | k-way scoring ignores pairwise joints | roadmap | **Disclosed** §7.3/§7.4 (pairwise Fréchet clip as cheap fix). |
| T7 | Emerging-risk cold start — weakest exactly on charter | should | **Disclosed** §7.3 as a named limitation ("v1's omission of it was the bug") motivating layers 6–7 (proposal Part B / §P3). |
| T8 | R1 unit pollution formatted as dollars; hardcoded graduation stamps; arbitrary urgency fallback; ecosystem LLM touchpoints | should | **Disclosed** §4 (all three production blemishes with scheduled fixes) + **new Appendix D boundary map** (5 touchpoints). |
| T9 | String-fragile identity masquerades as dependence | minor | **Disclosed** §5.2 + §7.3 (semantic hygiene, §P8). |
| E1 | Generator-forced B1; oracle B2; AUROC inversion unexplained | must | **Fixed** §6.1: all three disclosures (generator mechanism with parameter values; oracle status; B2-relative margin +5.2% AP / P@20 0.90-vs-0.80 / AUROC loss with the top-weighted-metric argument made explicitly); abstract re-anchored to carry both lifts + the AUROC loss; composite baseline named as the missing instrument (§A6). |
| E2 | No CIs; single split; no multi-seed | should | **Disclosed** §6.1 item 3 + §6.6 ("magnitude unresolved" reading imposed until they run; scheduled §A6). |
| E3 | eb_strength tuned on the decision regime; κ unexamined | should | **Disclosed** §3.1 + §6.1 (with the D9 invariance fact as partial mitigation; κ sensitivity scheduled). |
| E4 | No compound-level calibration task — T1's blind spot | should | **Disclosed** §6.6 (first bullet, marked load-bearing; instrument specified incl. rare×supported stratum; challenger promotion gated on it). |
| E5 | A1 is a wiring check; A3 tolerance unstated; B2 tie-break; stale limitations; Task D open | should | **Fixed** §6.4 rewritten as a two-column "v1's framing vs what it shows" table; B2 footnote in §6.1; Task D in §6.6; stale strings in §3.5/Appendix B. |
| E6 | Task C: unpassable pre-registered metric reinterpreted; no base rates; count-equivalence; tie at the cut | must | **Fixed** §6.2 rewritten with all four disclosures (+ the three-views-of-one-statistic note; tie-break stated in verified refined form; jittered re-run scheduled). |
| E7 | L\|S has no evaluation task | must | **Fixed** §2 (demotion at the definition site: "occurrence evaluated, severity unvalidated"), §6.6 (missing instrument + cheap fix via truth pack), abstract silent on severity claims. |
| E8 | Pre-registration conflict on the GO; B3 ≡ B1 on R1 | must | **Fixed** new §6.0 (conflict named, lenient-gate-chosen-ex-post acknowledged, "under the stricter reading Phase 1 did not pass") + §6.7 (GO scoped: what it licenses / does not assert) + §7.1 + abstract sentence. B3 ≡ B1 verified numerically and stated in bold. |
| §5 (paper-craft) | No related work; §3.7 class reachability; marginal_smoothing missing from Appendix A; interval floor outside config | nice/should | **Fixed**: new §1.1 related-methods section + references appendix; §3.7 clause; Appendix A gains the row, the floor row, and an "in config hash" column. |

## Other changes not tied to a single finding

- **Links repaired:** v1's relative links (`redesign/…`, `../contingency_atlas/…`,
  `../DISCLAIMER.md`) resolved to nonexistent paths from `docs/whitepaper/`
  and predate the `docs/redesign` → `docs/redesignv1` rename; v2 links
  resolve (`../redesignv1/…`, `../../…`).
- **Subtitle:** "deterministic hybrid scoring algorithm" → "deterministic
  scoring algorithm" (the hybrid channels are empty in v0.1.0 — D3
  discipline; the companion proposal is precisely the plan to make it
  hybrid).
- **Section numbering preserved** (§1–§8, C.1–C.3) so review references map
  onto v2; additions are subsections (§1.1, §5.5, §6.0, §6.6–6.7,
  §7.1–7.5) and Appendix D.
- **Roadmap decision log:** a dated correction row was appended for the
  "both regimes" NLL overstatement (the review found the same false claim
  at `roadmap.md:161`; the log is append-only, so the correction is a new
  row, not an edit).
- **Generated for this revision:**
  `generated/redesign/phase3/calibration-r0prime/` (R0′ dataset + strict-
  PASS calibration report). No shipped artifact was modified or re-emitted
  (per D9 note above).

## What remains code-side (not addressable by prose)

From the review's must/should list, in proposal order: full-realization
persistence + data_hash + append-only history + hash-surface widening +
k≥3 attestation fix + regime-aware loss suppression + graduation metadata +
urgency exclusion (§A7); mixture-prior challenger (§A1) / common-factor
model (§A2); interval fixes (§A3); windowed co-firing (§A4); count-aware MC
(§A5); Task B-cal + severity calibration + bootstrap/multi-seed/κ
sensitivity + composite baseline (§A6); stale-string fixes + artifact
re-emission (with D5); cross-platform CI job; semantic identity (§P8).

## Formatted edition (added after the critic pass)

A LaTeX/PDF edition of this revision now exists in the tslit-dspy
whitepaper format: `manuscript/contingency_atlas_whitepaper.tex` (+
compiled PDF), `Makefile` (`make all` = figures + latexmk), and
`scripts/generate_figures.py`, which draws four figures from the real
evaluation artifacts (Task B baselines; R0-vs-R0′ PIT histograms; the T1
markdown mechanism; the T2 interval-violation scatter). Content is
identical to `WHITEPAPER_v2.md`, which remains the canonical
content-and-audit-trail source — any future amendment updates both. One
numbering difference: LaTeX numbers §6's subsections from 6.1 (the
pre-registration conflict), so Task B is §6.2 in the PDF vs §6.1 in the
Markdown; all internal cross-references in each edition are
self-consistent. Toolchain note: built with a standard TeX distribution and
matplotlib installed into the project virtual environment.

## Plain-language executive summary (added 2026-08-03, user request)

Both editions gained an unnumbered **Executive Summary (plain language)**
before the abstract (the slot the tslit format already uses), plus a
12-entry "plain-language key to the jargon" table (risk atom,
decomposition, dependence, shrinkage prior, Monte Carlo, tail/EVT,
pre-registration, holdout, calibration, audit blob, research quantity).
Design rules applied: the summary carries the same honesty as the abstract
(oracle-baseline caveat, "no production number touched yet", the rare-pair
defect, unvalidated severity — all restated in lay terms), and analogies
were kept technically correct. The author's book *AI Agents in
Cybersecurity* is cited as companion practitioner reading (bibliography
entry marked "manuscript in preparation" — update when published). PDF is
now 27 pages.

## Post-draft critic pass

Two independent adversarial agents ran over the finished v2 draft: a
**coverage auditor** (review §6 table item-by-item, honesty of each
disclosure, internal cross-references, proposal-anchor accuracy) and a
**fact-checker** (every number, formula, code claim, quote, citation, and
link, recomputed from artifacts and code — including re-running the R0
eval in memory and re-verifying the egress hash chain across all 46 rows).

**Fact-checker: 35/36 checks clean.** One discrepancy, fixed: Appendix C
claimed the working papers and code were "under version control," but at
revision time the `docs/redesign` → `docs/redesignv1` rename and
`docs/whitepaper/` itself are uncommitted, so a checkout of HEAD predates
both — exactly the validator's-checkout standard the appendix invokes.
Appendix C now states the uncommitted status explicitly. (Committing the
rename and the whitepaper directory clears the caveat; that is a repo
action outside this paper revision.)

**Coverage auditor: 14/20 review items fully satisfied; no required
disclosure missing;** ten findings, all resolved:

1. *This section was an empty placeholder while the Method paragraph
   claimed the pass had run* — resolved by this record.
2. *§6.2's "count-ranking reproduces 0.767 exactly" was tie-break
   contingent* (0.63–0.80 across conventions; nine of thirty slots are
   count-1 tie-break picks) — re-verified independently, then §6.2 item 4
   rewritten to the tie-contingent version; logged as verification
   delta 3. The direction (lift normalization inert) is unchanged.
3. *v2 cited proposal §A7/§P1 as scheduling empirical-edge activation;
   the proposal only addresses coverage via §P1 enrichment* — §4 and
   Appendix D row 2 now say activation itself is unscheduled.
4. *Appendix D invented "§A7 adds a lint"* — cell corrected to "review
   flag only; no mechanical check scheduled."
5. *§5.3 claimed the proposal schedules a config coordinate in
   realization identity; §A7 does not* — §5.3 now states that item is
   scheduled nowhere and remains the paper's outstanding ask.
6. *§7.4's "all tracked in the proposal" was false for the
   severity-blend and data-side-tail items* — §7.4 now separates
   proposal-tracked (§A5, §A2) from spec-prescribed-but-unscheduled;
   §3.2's "scheduled" softened to "recorded in §7.4."
7. *The log said v2 cites §A3 for the interval fixes; it didn't, and the
   marginal-conditioning defect was only implied* — §7.2 now cites §A3
   and names the conditioning-on-\(q_i\) understatement as a defect.
8. *Abstract's "reconstruct it exactly" contradicted §5.3's own hash
   gaps* — qualified to "exactly, up to the hash-coverage gaps §5.3
   itemizes."
9. *Appendix B's R0′ snippet omitted the `r0prime_truth.json` write that
   C.2 promises* — line added to the snippet.
10. *Phantom subsection numbers (§5.5.2, §6.1.3, §6.2.1)* — converted to
    "§X item N" style in v2 and throughout this log.

Open items the critic pass established are **scheduled nowhere** (v2 now
says so at each site, so they cannot be mistaken for tracked work):
empirical-edge activation (D11), a config coordinate in `realization_id`
(D12), the severity-blend and data-side family-selection items (T5), and a
mechanical prose-number check for graduated scenarios (T8/Appendix D).
