# Architecture Decision Records

| # | Title | Status |
|---|-------|--------|
| [ADR-001](001-deterministic-scoring-core.md) | Deterministic scoring core; no LLM in the score path | accepted |
| [ADR-002](002-egress-gateway.md) | Data boundary with an egress gateway enforcement point | accepted |
| [ADR-003](003-batch-over-multiverse.md) | Batch compound scoring; multiverse loop removed | accepted |
| [ADR-004](004-compounds-separate-class.md) | Compound scenarios as a separate object class | accepted |
| [ADR-005](005-automatic-graduation.md) | Automatic graduation with deterministic eligibility and LLM prose | accepted |
| [ADR-006](006-review-first-ingestion.md) | Review-first artifact ingestion with rubric completeness check | accepted |
| [ADR-007](007-chokepoint-telemetry.md) | LLM telemetry at the client choke point | accepted |
| [ADR-008](008-stdlib-web.md) | Stdlib-only web dashboard, no framework | accepted |
| [ADR-009](009-sqlite-jsonl-storage.md) | SQLite + JSON/JSONL storage; no DuckDB yet | accepted |
| [ADR-010](010-stdlib-statistics.md) | Pure-stdlib statistics; no numpy/scipy | accepted |
| [ADR-011](011-no-validation-gui.md) | Validation as one-off gate; no validation GUI page | accepted |
| [ADR-012](012-gitignored-runtime-artifacts.md) | Runtime artifacts gitignored and regenerated | accepted |
| [ADR-013](013-advisory-review-assistance.md) | Advisory-only machine assistance at the review boundary | accepted |
| [ADR-014](014-register-merge-and-proposal-item-types.md) | Register merge and proposal item types | accepted |
| [ADR-015](015-human-decision-durability.md) | Human decisions versioned outside the code repository | accepted |
| [ADR-016](016-mitre-atlas-reviewed-overlay.md) | MITRE ATLAS as a versioned, reviewed, non-scoring overlay | accepted |
| [ADR-017](017-unified-draft-first-research-intake.md) | Unified draft-first research intake and approved-only scenario planning | accepted |
| [ADR-018](018-perfect-storms-workshop-no-compound-nodes.md) | Hub-fire is first-hop AND-gates; Perfect Storms stay workshop; compounds are never nodes | proposed |
| [ADR-019](019-regulatory-regime-mismatch.md) | Regulatory regime mismatch is intake, signposts, and workshop display — not a score | proposed |

Format: context → decision → consequences → alternatives considered.
Dates are decision dates, not authoring dates.
