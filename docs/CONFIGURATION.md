# Configuration

Precedence, highest first:

```text
explicit Python arguments
  → environment variables
    → config.yaml
      → built-in defaults
```

Unknown keys and wrong types are rejected loudly: a config typo is worse than
no config file.

---

## Where data lives

`ATLAS_HOME` (or `--home DIR`) puts every runtime store in one directory:

```text
$ATLAS_HOME/
├── atlas.db                  scenarios, findings, review history
├── event_evidence.db         evidence register
├── emerging_risks.json       risk register
├── scenario_export.json
├── organization-context.db   created only when the organization layer is enabled
├── config.yaml               optional; read automatically when present
├── exports/
├── logs/
└── telemetry/
```

Default: `~/.atlas`.

Single-store overrides still win over `ATLAS_HOME`, so existing deployments
keep their layout:

| Variable | Store |
|---|---|
| `ATLAS_DB` | scenario database |
| `ATLAS_EVIDENCE_DB` | evidence database |
| `ATLAS_EMERGING_RISKS` | risk register JSON |
| `ATLAS_SCENARIO_EXPORT` | scenario export JSON |
| `ATLAS_CONTEXT_DB` | organization-context database |
| `ATLAS_WORKSPACE` | workspace root |

### Config file resolution

1. `ATLAS_CONFIG` if set.
2. `$ATLAS_HOME/config.yaml` if it exists.
3. `contingency-atlas.yaml` in a source checkout.

---

## Model endpoints

Model access is optional. Without it, intake, review, deterministic scoring,
exports, and the dashboard all work.

### OMLX (local MLX server, default)

| Variable | Default |
|---|---|
| `ATLAS_LLM_PROVIDER` | `omlx` |
| `OMLX_BASE_URL` | `http://127.0.0.1:8000/v1` |
| `OMLX_API_KEY` | `test` |
| `OMLX_MODEL` | `DeepSeek-V4-Flash-0731-MXFP4-MLX` |
| `OMLX_MODEL_JUDGE` | same as `OMLX_MODEL` |
| `OMLX_MODEL_FINAL_JUDGE` | empty (independent final judge disabled) |
| `OMLX_TIMEOUT` | `600` seconds |
| `OMLX_USE_RESPONSES_API` | `0` |

### DGX / vLLM

Select with `ATLAS_LLM_PROVIDER=dgx`. `DGX_*` variables take precedence; the
standard `VLLM_*` names are accepted as aliases.

| Variable | Alias | Default |
|---|---|---|
| `DGX_BASE_URL` | `VLLM_BASE_URL` | `http://127.0.0.1:8000/v1` |
| `DGX_API_KEY` | `VLLM_API_KEY` | `test` |
| `DGX_MODEL` | `VLLM_MODEL` | `Qwen/Qwen3.6-27B-FP8` |
| `DGX_MODEL_JUDGE` | — | same as `DGX_MODEL` |
| `DGX_MODEL_FINAL_JUDGE` | — | empty |
| `DGX_TIMEOUT` | `VLLM_TIMEOUT_S` | `600` seconds |
| `DGX_MAX_TOKENS` | `VLLM_MAX_TOKENS` | `4096` |
| `DGX_ENABLE_THINKING` | `VLLM_ENABLE_THINKING` | `0` |

Provider aliases: `mlx` → `omlx`, `vllm` → `dgx`.

**Secrets never belong in `config.yaml`.** Use the environment.

---

## `config.yaml`

```yaml
provider: omlx          # omlx | dgx (aliases: mlx | vllm)

omlx:
  base_url: http://127.0.0.1:8000/v1
  model: DeepSeek-V4-Flash-0731-MXFP4-MLX
  model_judge: DeepSeek-V4-Flash-0731-MXFP4-MLX
  model_final_judge: ""
  timeout: 600
  max_tokens: 16384
  use_responses_api: false

dgx:
  base_url: http://127.0.0.1:8000/v1
  model: Qwen/Qwen3.6-27B-FP8
  model_judge: Qwen/Qwen3.6-27B-FP8
  model_final_judge: ""
  timeout: 600
  max_tokens: 16384
  enable_thinking: false

organization_context:
  enabled: false
  db_path: organization-context.db
```

`atlas init` writes this file into the data directory.

---

## Pipeline and safety switches

| Variable | Default | Effect |
|---|---|---|
| `ATLAS_AUTO_RETIRE` | `0` | Allow Phase 6 to merge/retire scenarios. Merge retires the merged-away scenario, so this is deliberately off. The dashboard cannot override it. |
| `ATLAS_GUARD` | `on` | Fail-closed ontology validation on machine-producer write seams. `off` disables it; intended for tests and demo-only sessions. |
| `ATLAS_DRIFTLAB_ENABLED` | `0` | Optional runtime governance tracing and baseline comparison. |
| `DRIFTLAB_DATA_DIR` | `~/.driftlab` | Trace output directory (sensitive; never commit). |
| `ATLAS_CONTEXT_ENABLED` | `0` | Opt in to the private organization workspace. |
| `ATLAS_TELEMETRY` | unset | Point the costs view at one specific telemetry log (used by tests). |

Judge behaviour is configured through `Settings` (or YAML where exposed):
`max_judge_iterations` (default `3`) and `judge_score_threshold`
(default `0.75`).

---

## Verification

```bash
atlas doctor            # installation and endpoint checks
atlas config            # effective values, secrets redacted
python scripts/verify_all.py
python -m pytest -q
```
