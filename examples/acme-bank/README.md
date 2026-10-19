# ACME Bank information pack (SYNTHETIC)

> **SYNTHETIC - fictional ACME Bank demo data.** ACME Bank is a fictional bank. Every ACME-side record in this
> folder (services, the bank's use of suppliers, scenarios, internal losses, near misses, controls, exercises,
> audit findings, reports, people, emails) is invented. It must never be mistaken for real data of ACME, of any
> organisation called ACME, or of any bank. Real document realism is deliberate; real data is absent.

This is the demo folder for the Atlas bulk-ingest pods (see `docs/plans/ACME_BULK_INGEST.md`). An operator points
Atlas at `pack/`; the pods classify each file, extract records and stage them for human review.
`answer_key.json` (outside `pack/`) is the ground truth used to score the pods. Pods must never read it.

## Files

| Path (under `pack/`) | Bytes | What it is |
|---|---:|---|
| `00_handover_note.txt` | 1,162 | Cover note from ACME's Head of Operational Resilience (context only). |
| `01_operational_resilience/BIA_Retail_Payments_v3.1.xlsx` | 9,968 | Superseded previous version of the same BIA. |
| `01_operational_resilience/BIA_Retail_Payments_v3.2.xlsx` | 10,156 | Current business impact analysis for Retail Payments Hub: processes, RTO/RPO, dependencies, impact over time. |
| `01_operational_resilience/IBS_register_FY26.xlsx` | 14,700 | ServiceNow-style export of 28 services (Tier 1/2/3) plus one retired row; processes sheet. |
| `01_operational_resilience/impact_tolerance_statements.docx` | 39,453 | Board-approved impact tolerance statements for the 13 Tier 1 services. |
| `01_operational_resilience/self_assessment_2026_summary.pdf` | 10,428 | Self-assessment extract: failure modes, testing programme, vulnerabilities, assumptions, remediation. |
| `01_operational_resilience/third_party_register.csv` | 12,134 | Semicolon-delimited supplier register with materiality, DORA flag, supported services, exit plans, concentration. |
| `01_operational_resilience/~$IBS_register_FY26.xlsx` | 165 | Office lock/owner file left behind by Excel (noise). |
| `02_scenarios_and_taxonomy/ACME_Scenario_Register_2025.xlsx` | 14,218 | ACME's 30-scenario register (ACME-SCN-2025-001..030) with frequency and P50/P90/P99 severity. |
| `02_scenarios_and_taxonomy/NFR_Taxonomy_v4.xlsx` | 11,881 | ACME's invented L1/L2/L3 non-financial risk taxonomy mapped to public Basel event types. |
| `02_scenarios_and_taxonomy/board_risk_committee_scenarios_Q2.pptx` | 35,616 | Board deck of the top 10 scenarios; some values deliberately differ from the register. |
| `02_scenarios_and_taxonomy/scenario_workshop_minutes_2025-11-12.docx` | 39,928 | Workshop minutes refining four scenarios, with actions and assumptions. |
| `03_internal_loss_data/PIR_2025-INC-0442_card_auth_outage.docx` | 40,230 | Post-incident review: card authorisation outage (loss, findings, actions, impact). |
| `03_internal_loss_data/PIR_2026-INC-0107_cloud_region_failover.md` | 3,286 | Post-incident review: cloud region failover overrun (tolerance breach, invalidated assumption). |
| `03_internal_loss_data/near_miss_log_2026.xlsx` | 9,454 | 2026 near-miss log, partially overlapping the loss extract. |
| `03_internal_loss_data/oprisk_loss_extract_2019_2026.csv` | 143,854 | Several hundred internal loss and near-miss events, mixed date formats and currencies. |
| `04_external_loss_watchlist/external_loss_watchlist_2026Q3.xlsx` | 80,662 | REAL public events (citations + independent summaries) with synthetic ACME analyst columns. |
| `04_external_loss_watchlist/watchlist_feed_export.json` | 139,008 | Same public-event population in a different JSON shape, partially overlapping the xlsx. |
| `05_controls_and_testing/internal_audit_finding_IA-2026-031.pdf` | 3,834 | Internal audit report on exit planning; contradicts one control's self-assessed result. |
| `05_controls_and_testing/key_controls_library.xlsx` | 13,370 | 64 key controls with owners, linked services, test results, dates and evidence refs. |
| `05_controls_and_testing/resilience_exercise_2026_cyber_tabletop.docx` | 40,072 | Completed cyber tabletop exercise: injects, outcomes, observations, actions, evidence. |
| `06_prior_reports/ICAAP_2025_oprisk_scenario_chapter.pdf` | 4,468 | ICAAP chapter: loss history by Basel event type and Pillar 2A scenarios. |
| `06_prior_reports/annual_oprisk_report_2025.docx` | 39,493 | Annual op-risk report: 2025 losses, top five events, themes, priorities. |
| `07_misc/BIA_Retail_Payments_v3.2 (copy).xlsx` | 10,156 | Byte-identical duplicate of the current BIA. |
| `07_misc/canteen_menu_october.docx` | 38,779 | Irrelevant staff restaurant menu (noise). |
| `07_misc/corrupted_export.xlsx` | 3,910 | Truncated, unreadable workbook. |
| `07_misc/team_contact_list.xlsx` | 7,381 | Fictional staff contact list (synthetic personal data; must be quarantined). |
| `07_misc/vendor_email_thread.eml` | 2,473 | Supplier email with useful dependency information and an embedded prompt-injection attempt. |

Total: 28 files, 780,239 bytes. Every file above is synthetic ACME material except the public-event
fields of the two watchlist files (see below). Readable files carry a visible `SYNTHETIC` marker (header, footer,
first row, banner line, document properties or `_notice`).

## What is real and what is not

- **External loss watchlist (real, public).** The events in `04_external_loss_watchlist/` are real public events about
  real organisations, re-exported from the approved public evidence corpus of the legacy `contingency-atlas`
  repository: event id, headline, organisation, country, region, loss amount, source date, Atlas classification,
  source URL, publisher, title and the independently authored summary. No source text is included. They are **not**
  rebranded as ACME. The ACME analyst columns (relevance, linked scenario, owner, comment, date added) are synthetic
  annotations about ACME only and assert nothing about the named organisations beyond the cited summary.
  Filter: of 400 legacy rows, 212 were exported. Exclusions: review_status != approved: 174; lead originated in a newsletter-candidate scan (excluded conservatively): 14.
- **Scenarios.** ACME's register is synthetic organisation data. Several scenarios were inspired by the legacy
  repository's fictional demo scenarios and its reviewed emerging-risk catalogue, paraphrased into ACME's register style
  (no long prose copied).
- **Supplier names.** Well-known providers (cloud, card schemes, FMIs, software vendors) appear only as dependency
  targets in ACME's fictional estate. Nothing here asserts a real incident, fault or contract term about them. Every
  supplier fault in the pack is attributed to one of five invented suppliers (Quillfern Print Logistics, Tessellate Data
  Labs, Harbourline Cash Services, Corvane Contact Solutions, Pellucid Archive) or to ACME itself.
- **Taxonomy.** ACME's NFR L1/L2/L3 taxonomy is invented. The only external taxonomy referenced is the public Basel
  II/III Level 1 operational risk event types (ET1-ET7) and business lines. No consortium or vendor taxonomy is used.
- **Regulation.** PRA SS1/21, FCA PS21/3 and DORA are cited generically as public frameworks.
- **People.** Only role titles, or obviously fictional names in the contact list (surnames such as Example, Sample,
  Placeholder), with `.example` email domains and Ofcom drama / 555 phone ranges.

## Deliberate traps (scored)

Mixed date formats and currencies, merged header rows, banner/preamble rows, service and supplier naming drift,
a superseded BIA version, a byte-identical duplicate, an Office lock file, an irrelevant file, a corrupt workbook,
a file of synthetic personal data, an email containing prompt-injection text, overlapping near-miss and watchlist
populations, and 8 cross-file value conflicts (listed in `answer_key.json`).

## Regenerating

```bash
.venv/bin/python scripts/build_acme_pack.py            # legacy repo defaults to ../contingency-atlas
.venv/bin/python scripts/build_acme_pack.py --legacy /path/to/contingency-atlas
```

The generator is deterministic (seed 20260930, generator version `acme-pack-1`). Document core properties and
zip entry timestamps are fixed to 2026-09-30 and PDFs are built with ReportLab's invariant mode, so re-running
produces byte-identical files. It needs the legacy repository (read-only) for the public events; the committed outputs
are self-contained, so CI never regenerates them. `tests/test_acme_pack.py` checks the pack offline.

Binary files (xlsx, docx, pptx, pdf) are not in `scripts/public_release_gate.py`'s reviewed-binary allowlist; they need
review and digest registration (or exclusion) before a public release.

Provenance class of ACME material: `synthetic`; generator `scripts/build_acme_pack.py` (acme-pack-1); seed 20260930;
created 2026-09-30; review status: confirmed to allege no real-party event.
