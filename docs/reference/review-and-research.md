# Review Queue & Deep Research

The Review queue is the human boundary rendered in the browser: every
pending evidence event and emerging risk waits there for an attested
human decision, with deterministic validation shown *before* the
decision and two optional layers of machine assistance — an advisory
pre-analysis and a bounded web-research loop — that can propose but
never decide (ADR-013). Pending channel proposals (ADR-014) wait in the
same queue and get the same two layers, but weakened on purpose: the
pre-analysis explains instead of recommending, and the research loop
files no evidence — the channel decision stays pure human judgment.
This document covers the whole surface: the card, the validation
reports, the decision path, the register lock, the suggestion cache and
agreement audit, the deep-research loop, and the adopt-refs mechanism.

## 1. Why a card at all

The CLI (`scripts/review_records.py`) enforces the review boundary but
reports failures only *after* a decision is attempted, as an exception.
The card runs the same validators in a non-raising "report" mode first,
so a reviewer sees *why* a record can or cannot be approved — per
check, per evidence ref — before deciding. Both front ends call the
same functions in `review.py`; there is exactly one audited decision
path.

## 2. Validation reports (traffic lights)

`review.evidence_validation_report(record)` and
`review.risk_validation_report(record, evidence_db=…)` are non-raising
mirrors of the raising validators (`validate_evidence_for_approval`,
`validate_risk_for_approval`). Every check is evaluated and returned:

```json
{"kind": "risk", "approvable": false,
 "checks": [{"key": "refs_resolve",
             "label": "All refs resolve to approved evidence",
             "status": "fail", "detail": "1 of 4 ref(s) do not resolve"}],
 "refs": [{"ref": "EVT-…", "status": "approved|approved_synthetic|pending|rejected|unattested|invalid|missing",
           "event_id": "…", "headline": "…", "detail": "…"}],
 "blockers": ["…"]}
```

The mirror contract is guarded by tests: for a matrix of records, the
report's `approvable` must equal "the raising validator did not raise"
(`tests/test_web_review.py::test_reports_agree_with_raising_validators`).
Per-ref classification is strictly informational — resolution semantics
for approval are unchanged (a risk ref counts only when the evidence is
`approved`, rights-attested, with a valid review timestamp, and itself
passes evidence validation; `approved_synthetic` evidence does **not**
satisfy risk approval).

## 3. The decision path

`POST /api/review/decide` routes to the same four functions the CLI
uses — `approve_evidence`, `reject_evidence`, `approve_risk`,
`reject_risk` — and maps `ReviewError` to 400 (409 when the register is
locked). The rules are the CLI's rules:

- Approval requires **both** explicit attestations
  (`confirm_public_source`, `confirm_rights`). The card renders them as
  checkboxes that are never pre-ticked; the APPROVE button stays
  disabled until the record is approvable *and* both are checked.
- Rejection requires a reason.
- Approval of a risk canonicalizes `evidence_refs` to event ids and
  stamps the reviewer note into `provenance_note`, exactly as the CLI
  does.

## 4. The register lock

`emerging_risks.json` is mutated read-modify-write by three writers:
the review workflow (CLI and card), phase 2's monitoring cycle, and the
adopt-refs endpoint. `review.register_lock(path)` serializes them with
an `flock` on a sidecar `<register>.lock`, held for the whole
load→save span. Phase 2 holds it across its LLM calls (deliberately:
reviewing while the monitor rewrites the register was the lost-update
hazard); a blocked writer gets a clear "register is locked" error —
409 in the card — rather than silently losing updates. The lock
releases automatically if the holding process dies. The sidecar is
runtime state and gitignored. On platforms without `fcntl` the lock
degrades to a no-op.

## 5. Advisory pre-analysis (`review_assist.py`)

An optional judge-style pre-analysis of each pending record, run as a
background job (`review_analyze` phase — whole queue, or one record
with `kind` + `record_id` + `force`). The prompt receives the record,
the deterministic validation results, and the register titles (for
duplication assessment), is told to treat record text as untrusted
input, and is explicitly told it **cannot** attest public access or
reuse rights — those always appear under `human_must_verify`.

Output per record: `recommendation` (`approve` | `reject` |
`needs_more_evidence`), `confidence`, `summary`, `reasons`, `concerns`,
`human_must_verify`. Rendering rules that keep it advisory: clearly
labeled panel, never pre-selects a decision, never pre-ticks
attestations.

**Cache.** Suggestions are stored in `atlas.db.review_suggestions`,
content-addressed by a SHA-256 over the analyzed fields plus the model
id. A phase-2 rewrite of the record changes the hash and the card marks
the suggestion STALE; re-runs with an unchanged record and model are
served from cache (`force` overrides).

**Agreement audit.** When the human decides, the decision is written
next to the latest suggestion (`human_action`, `decided_at`). The card
footer shows the running agreement ratio with the deliberately blunt
framing: *if you always agree, you have stopped reviewing*. A reviewer
drifting into rubber-stamping becomes measurable rather than
invisible.

**Channel proposals get an explanation, not a verdict.** For a pending
channel proposal (P1) the same job runs a different prompt whose output
is `explanation` + `weaknesses` and nothing else — no recommendation,
no confidence. The model restates what is being proposed (kind,
channel, the risk atoms involved), explains in plain English why the
machinery surfaced it, and names weaknesses to probe; the decision
stays pure human judgment (ADR-014). Staleness hashing works the same
way (a mechanism or citation rewrite marks the explanation STALE), and
when a deep-research memo exists for the proposal, re-running the
analysis grounds the explanation in the research findings.
Explanation-only rows are excluded from the agreement audit — there is
no machine verdict to agree or disagree with.

The analysis model is `omlx.model_judge` — the same separation as the
scenario QA judge, so a generation model never grades its own intake.

## 6. Deep research (`research.py`)

For records blocked by missing or weak evidence, the card can run a
bounded web-research loop as a background job (`review_research`
phase, one record per run; `kind` is `event`, `risk`, or `channel`).
Verdicts are three-valued by design:

| Verdict | Meaning |
|---------|---------|
| `enough` | coverage judge deems the red flags resolvable with what was found |
| `no_evidence_found` | searching further is unlikely to help — a first-class, decision-relevant outcome |
| `budget_exhausted` | caps hit while the judge still voted "continue" |

**Loop shape** (Ralph-style, all steps LLM-driven with the judge
model, temperature 0):

1. **Plan** — up to 3 queries per iteration from the record's red
   flags (failing checks + pre-analysis concerns), with optional
   operator-supplied seed domains turned into `site:` variants.
2. **Search** — DuckDuckGo's HTML endpoint (no API key). Results are
   parsed, redirect-unwrapped, filtered through `is_public_http_url`,
   and de-duplicated. A bot challenge or empty page degrades to "no
   results", never an exception.
3. **Triage** — the model selects which results to fetch. Only URLs
   the search actually returned are eligible: a model-invented URL is
   silently dropped (tested).
4. **Fetch** — capped page retrieval: ≤ 6 pages per run, ≤ 400 KB per
   page, ≤ 3 redirects with **every hop** re-validated as a public
   HTTP(S) URL, text/HTML content types only, script/style stripped.
   PDFs (by content type or a `.pdf` URL suffix) are the one exception:
   they are downloaded whole under their own 8 MB cap — the xref table
   sits at the end of the file, so truncation breaks parsing — and
   parsed locally with pypdf (≤ 20 pages, ≤ 60k chars of extracted
   text, unextractable pages skipped). Extracted PDF text is untrusted
   data exactly like HTML text; a PDF pypdf cannot parse degrades to a
   recorded fetch error, never an exception.
5. **Assess** — per page: does it genuinely support or refute specific
   red flags (not merely share the topic)? If it is real evidence, the
   model drafts a candidate record with a summary in its own words.
6. **Judge coverage** — per red flag `covered | partial | uncovered`,
   then the verdict. At most 3 iterations.

**Candidates are proposals.** Each accepted draft becomes an ordinary
`pending` row in `event_evidence.db` via the standard `add_evidence_event`
path: `event_id = EVT-DR-<fingerprint[:12]>`, the phase-1 fingerprint
recipe, `provenance_class = public_secondary`,
`source_type = public_research`, and a provenance note beginning
`origin: deep_research` naming the record it was found for. Duplicate
URLs/ids are dropped by the store's dedup. Nothing is ever written
approved; candidates queue for the same attested human review as any
other evidence.

**Channel-proposal mode (gated).** For a pending channel proposal the
loop runs the same steps but under a different contract. It is *gated*
— it refuses to start until a fresh advisory explanation exists,
because the explanation's weaknesses become the red flags (plus a
citation-corroboration flag when the proposal's citation is
unverified). It files **no** candidate evidence — pages are assessed
only against the proposed transmission mechanism. And it ends with one
extra step: the model drafts the reviewer's written reason
(`draft_reason` on the memo), pre-filled into the decision panel for
the human to edit — advisory text, never a submitted decision. The
intended sequence is analyze → research → (optionally) re-analyze so
the explanation is grounded in the findings → human decision.

**Memo.** Each run stores a memo in `atlas.db.review_research`
(verdict, red flags, per-flag coverage, per-page findings, candidate
ids, searches, errors; channel runs add `draft_reason`). The memo also
carries the record's content hash, so batch runs can tell whether the
latest memo still covers the record as it stands.
`/api/review/pending` attaches the latest memo per record with each
candidate's *current* review status resolved, so the card can show
which candidates are approved and adoptable.

**Batch mode (`review_research_all` phase).** The DEEP-RESEARCH ALL
button runs the loop for every pending record whose pre-analysis is
*fresh* — a stored suggestion matching the record's current content
hash and the current judge model, carrying no error (the same check
the pre-analysis cache uses). Records whose latest memo already covers
the current content are skipped (`force` re-runs them); everything
else is reported as `skipped_no_preanalysis` — this is the
analyze → research sequence enforced for events and risks, which the
single-record phase only advises. Because each record runs the full
bounded loop, the batch is capped at 10 records per run (the rest is
reported as `deferred`), one-at-a-time job serialization still
applies, and per-record failures are collected into `errors` rather
than killing the batch. Still proposal-only: memos plus pending
candidates, never approvals.

## 7. Adopt-refs

`POST /api/review/adopt-refs {record_id, event_ids}` attaches evidence
ids to a pending risk's `evidence_refs` without hand-editing the
register. Deliberately narrow:

- every id must exist and be **fully approved** (anything else is a
  400 naming the offender);
- the target risk must still be `pending`;
- the write happens under the register lock;
- adopted ids are stamped into `provenance_note`
  ("Evidence refs adopted after human approval of the cited events: …");
- idempotent — already-present ids are skipped.

The intended cycle for an evidence-poor record is therefore: deep
research → human review of candidates → adopt approved refs → the
risk's own checks go green → attested approval. The machine finds; the
human admits; the machinery records who did which.

## 7b. Detach-refs

`POST /api/review/detach-refs {record_id, refs}` removes refs from a
pending risk's `evidence_refs` — the mirror of adopt-refs and the only
sanctioned way to *remove* refs. It exists because Phase 2 unions each
monitoring cycle's whole source packet into every re-confirmed risk's
refs, so packets accumulate URLs that bear nothing on the specific
risk — and since every ref must resolve to fully approved evidence for
the risk to pass review, packet junk permanently blocks approval.
Deliberately simple:

- any ref value may be detached (junk refs are precisely the ones that
  resolve to nothing — no existence/approval check, unlike adopt);
- the target risk must still be `pending`;
- the write happens under the register lock;
- removed refs are stamped into `provenance_note`
  ("Evidence refs detached by human review: …");
- the derived `evidence_strength` is recomputed from what remains;
- idempotent — already-absent refs are skipped.

In the dashboard the refs table carries a per-row ✕, plus a bulk
"DETACH N UNRESOLVED" button that targets exactly the refs with no
evidence record (status `missing`) — the ones that can never resolve.

## 8. Dashboard surface

| Element | Behavior |
|---------|----------|
| Nav badge | pending count across all five queue kinds — events, risks, merge proposals, channel proposals, analogues (from `/api/data`) |
| Left list | one row per pending record: kind tag, approvable dot, suggestion chip, blocker count |
| Provenance checks panel | the validation report as traffic lights; per-ref resolution table for risks |
| Advisory panel (violet) | pre-analysis with recommendation chip, reasons, concerns, "only you can attest" callout, RE-ANALYZE; for channel proposals: plain-English explanation + weaknesses to probe, deliberately no recommendation |
| Research panel (cyan) | memo verdict, red-flag coverage, sources examined, candidate statuses, seed-sites input, DEEP-RESEARCH / RESEARCH AGAIN, ADOPT APPROVED REFS; for channel proposals: DEEP-RESEARCH stays locked until a fresh explanation exists, no candidates, and the memo's advisory draft reason is pre-filled into the decision panel |
| Decision panel | two attestation checkboxes (never pre-ticked), note/reason field, APPROVE (disabled until approvable + attested; tooltip names the failing checks), REJECT (needs a reason) |
| Job status line | RUNNING / DONE-with-counts / ERROR-with-message for pre-analysis and research jobs; partial results stream in while a job runs |
| Toolbar | PRE-ANALYZE ALL (every pending record) and DEEP-RESEARCH ALL (only records with a fresh pre-analysis; disabled with a hint otherwise) — both background jobs |

Jobs go through the standard one-at-a-time runner and appear in the
Pipeline job console as well.

## 9. Failure modes worth knowing

| Symptom | Meaning |
|---------|---------|
| Pre-analysis fails on every record instantly | the judge model id is not served by OMLX — check `GET <omlx>/v1/models` against `omlx.model_judge` |
| Research verdict `budget_exhausted` | caps hit mid-hunt; RESEARCH AGAIN continues with a fresh run |
| A fetched page shows "no title or text content" | JavaScript-shell page (e.g. YouTube) — not text-fetchable; ingest the transcript via the Phase 1 artifact path instead |
| Decide returns 409 | register locked (monitoring cycle or another reviewer); retry shortly |
| Suggestion marked STALE | the record changed (phase-2 rewrite) since the analysis — re-analyze |
| DEEP-RESEARCH refuses with "run pre-analysis first" | channel-proposal research is gated — the explanation's weaknesses steer the search, so a fresh advisory explanation must exist first |
| Memo errors name a PDF ("could not parse PDF") | malformed or encrypted PDF beyond pypdf; the run continued with the other pages — ingest the document via the Phase 1 artifact path if it matters |
