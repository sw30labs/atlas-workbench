# Data Model

## 0. Principles

1. **Facts vs judgments vs narratives** are separate tables/fields with provenance.
2. **Scores are derived artifacts** of `(state, algorithm_version, seed)`, not identity.
3. **Plane tags** on every row that crosses a compute boundary.
4. **Canonical JSON** for hashing: sorted keys, no insignificant whitespace, naive datetimes as ISO-8601 Z.
5. Compatible with current Atlas concepts (`issue`, drivers, maturity) but extended for compound search.

---

## 1. Entity overview

```text
RiskAtom ──┐
           ├── CompoundScenario ── ScoreRealization ── TelemetryEvent
ExternalFeature ──┘         │
IncidentFeature (agg) ──────┤
ChannelGraph ───────────────┤
                            ├── Checkpoint ── Branch ── MergeRecord
                            └── PromotionRecord
```

---

## 2. Core schemas

### 2.1 RiskAtom

| Field | Type | Notes |
|-------|------|-------|
| `risk_id` | string PK | Stable slug / UUID |
| `title` | string | |
| `description` | text | Usually `public`; otherwise keep local |
| `scoring_cell_code` | string | Optional project-native code for synthetic scoring-cell grouping. |
| `atlas_category` | string | Existing Atlas category optional |
| `channels` | string[] | Causal channels for CBGE |
| `external_drivers` | string[] | Atlas radar drivers |
| `signal_maturity` | enum | weak…fading |
| `momentum` | enum | accelerating/stable/slowing |
| `trend` | enum | rising/stable/falling |
| `evidence_strength` | enum | |
| `status` | enum | monitoring/deprecated/… |
| `review_status` | enum | pending/approved/… |
| `plane` | enum | `public` / `aggregate` / `local_detail` |
| `first_seen` | date | |
| `last_reviewed` | date | |
| `source_type` | string | public_reporting/synthetic |
| `provenance` | json | |
| `embedding_ref` | string? | retrieval id, optional |

### 2.2 ChannelGraph

| Field | Type | Notes |
|-------|------|-------|
| `graph_id` | string | |
| `version` | int | |
| `nodes` | risk_id[] | |
| `edges` | {src, dst, reason, weight?, source}[] | reason: shared_channel/expert/empirical |
| `graph_hash` | string | sha256 canonical edges+nodes |
| `created_at` | datetime | |

### 2.3 IncidentFeature (synthetic scoring aggregates)

**Never store local-detail prose in the feature table used for egress.**

| Field | Type | Notes |
|-------|------|-------|
| `cell_id` | string | Stable identifier for a scoring-cell/segment group |
| `scoring_cell_code` | string | Project-native synthetic cell code |
| `segment_code` | string | Neutral fictional segment used by the demo |
| `period_start` | date | |
| `period_end` | date | |
| `count` | int | |
| `severity_sum` | float? | optional; policy may allow |
| `severity_quantiles` | json? | e.g. {0.5,0.9,0.95} |
| `n_severity` | int | |
| `plane` | `aggregate` | |
| `source_system` | string | |
| `as_of` | datetime | |

Synthetic event rows remain local demo artifacts, have no egress schema, and
are not joined into API payloads.

### 2.4 ExternalFeature

| Field | Type | Notes |
|-------|------|-------|
| `feature_id` | string | |
| `name` | string | pre-registered |
| `family` | string | geo/macro/cyber/reg |
| `period` | date/bucket | |
| `value` | float/bool | |
| `source_refs` | string[] | public |
| `registry_status` | enum | candidate/accepted/rejected |
| `fdr_q_value` | float? | if tested |
| `holdout_pass` | bool? | |

### 2.5 CompoundScenario

| Field | Type | Notes |
|-------|------|-------|
| `scenario_id` | string PK | |
| `risk_ids` | string[] sorted | identity core |
| `horizon` | string | e.g. `1y` |
| `compound_def` | enum | `all_must_fire` / `k_of_n` |
| `k` | int? | if k_of_n |
| `channels_involved` | string[] | denormalized |
| `transmission_id` | string? | distinct mechanism variant |
| `scope` | json | Segment/region scope assumptions |
| `assumptions` | json | structured |
| `assumption_hash` | string | |
| `state_hash` | string | see §4 |
| `narrative` | text? | judgment/narrative; plane-tagged |
| `narrative_plane` | enum | |
| `status` | enum | candidate/promoted/archived/rejected |
| `branch_id` | string | |
| `parent_ids` | string[] | lineage |
| `root_id` | string | |
| `exploration` | bool | wildcard origin |
| `created_at` | datetime | |
| `algorithm_version` | string | expected scorer |

### 2.6 ScoreRealization

| Field | Type | Notes |
|-------|------|-------|
| `realization_id` | string PK | |
| `scenario_id` | string FK | |
| `state_hash` | string | must match scenario |
| `algorithm_version` | string | |
| `model_config_hash` | string | |
| `rng_seed` | int/string | |
| `p_hat` | float | |
| `p_interval` | [float, float] | |
| `loss_quantiles` | json | |
| `loss_mean` | float | |
| `components` | json | full audit blob (algorithm-spec §9) |
| `uncertainty_class` | enum | |
| `disagreement` | float | |
| `novelty` | float | |
| `evidence_support` | float | |
| `coherence` | float | |
| `U_score` | float? | loop utility |
| `created_at` | datetime | |

### 2.7 Checkpoint

| Field | Type | Notes |
|-------|------|-------|
| `checkpoint_id` | string PK | |
| `branch_id` | string | |
| `parent_checkpoint_id` | string? | |
| `created_at` | datetime | |
| `run_id` | string | |
| `config_hash` | string | |
| `graph_hash` | string | |
| `archive_scenario_ids` | string[] | |
| `rng_state` | string/json | |
| `assumptions` | json | branch-level |
| `feature_registry_hash` | string | |
| `code_git_sha` | string | |
| `plane_manifest` | json | |
| `state_hash` | string | checkpoint-level hash |
| `telemetry_cursor` | string | |

### 2.8 Branch

| Field | Type | Notes |
|-------|------|-------|
| `branch_id` | string PK | |
| `name` | string | human label |
| `color` | string | viz |
| `head_checkpoint_id` | string | |
| `status` | enum | active/abandoned/merged |
| `parent_branch_id` | string? | |
| `created_from_checkpoint` | string | |
| `abandon_reason` | string? | |
| `bandit_stats` | json | pulls, reward |

### 2.9 MergeRecord

| Field | Type | Notes |
|-------|------|-------|
| `merge_id` | string | |
| `parents` | string[] | branch or scenario ids |
| `child_id` | string | |
| `method` | enum | structural/ci_overlap/human |
| `conflict` | bool | |
| `pre_scores` | json | both sides |
| `notes` | string | |

### 2.10 PromotionRecord

| Field | Type | Notes |
|-------|------|-------|
| `promotion_id` | string | |
| `scenario_id` | string | |
| `realization_id` | string | |
| `gates` | json | pass/fail detail |
| `U_score` | float | |
| `archive_rank` | int | |
| `promoted_at` | datetime | |
| `promoted_by` | enum | auto/human |

### 2.11 TelemetryEvent

See `telemetry-spec.md` (mirrored fields). FK optional: `node_id`, `branch_id`, `scenario_id`, `checkpoint_id`.

---

## 3. Enumerations (normative)

```text
plane: public | aggregate | local_detail
uncertainty_class: statistical | mixed | judgment_dominant
scenario_status: candidate | promoted | archived | rejected | superseded
branch_status: active | abandoned | merged
compound_def: all_must_fire | k_of_n
edge_reason: shared_channel | expert | empirical | wildcard
```

---

## 4. State hash design

### 4.1 Scenario structural state hash

```text
state_hash = sha256(canonical_json({
  "risk_ids": sorted(risk_ids),
  "horizon": horizon,
  "compound_def": compound_def,
  "k": k or null,
  "transmission_id": transmission_id or "",
  "scope": canonical(scope),
  "assumption_hash": sha256(canonical_json(assumptions)),
  "graph_hash": graph_hash,
  "feature_registry_hash": feature_registry_hash,
  "algorithm_major": algorithm_version.split(".")[0] + ".x"
}))
```

### 4.2 Checkpoint state hash

```text
checkpoint.state_hash = sha256(canonical_json({
  "branch_id": branch_id,
  "parent_checkpoint_id": parent or "",
  "config_hash": config_hash,
  "graph_hash": graph_hash,
  "archive_ids": sorted(archive_scenario_ids),
  "assumption_hash": sha256(canonical_json(assumptions)),
  "feature_registry_hash": feature_registry_hash,
  "code_git_sha": code_git_sha,
  "plane_manifest_hash": sha256(canonical_json(plane_manifest))
}))
```

### 4.3 Explicitly excluded from hashes

- Timestamps, titles, narrative prose  
- Token counts, costs  
- Score values, MC draws  
- Judge free-text feedback (store separately; optional `feedback_hash` if needed for audit of a *decision*)

### 4.4 Collision / sensitivity

- SHA-256; truncation to 16 hex chars allowed for display only; store full hash.
- Assumption schema must be strict; free-text assumptions break stability — force structured keys.

---

## 5. Canonicalization rules

1. UTF-8, NFC optional but fixed.
2. JSON object keys sorted lexicographically at every level.
3. Floats: use decimal string with agreed precision for hash inputs only (e.g. 8 d.p.) **or** avoid floats in hashed assumptions (prefer enums/rationals).
4. Arrays of ids sorted unless order is semantic (it is not for risk_ids).

---

## 6. Migration from current repo artifacts

| Current | Target |
|---------|--------|
| `emerging_risks.json` | `RiskAtom` rows |
| `atlas.db` scenarios | Partial map to `CompoundScenario` + qualitative legacy scores marked `legacy_qualitative` |
| `event_evidence.db` | Public `ExternalFeature` / evidence refs (`public`) |
| Demo synthetic losses | `IncidentFeature` with `source_system=synthetic` |

Legacy urgency scores **do not** auto-convert to \(\hat p\).

---

## 7. Storage layout **(REC) Phase 1**

```text
data/
  public/
    risks.sqlite
    external.sqlite
    multiverse.sqlite    # branches, checkpoints metadata, scenarios structural
  local/
    scoring_features.duckdb   # aggregate synthetic features
    synthetic_events.duckdb   # local_detail demo rows
    score_components.duckdb
  telemetry/
    events.jsonl | sqlite
```

---

## 8. Open questions

1. UUID vs slug risk_id — **(REC)** stored UUID, slug display.
2. Whether narratives live in multiverse DB or object store — **(REC)** DB Phase 1.
