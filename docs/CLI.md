# `atlas` command reference

One entry point for setup, data, analysis, review, and export. Run
`atlas --help` for the summary, `atlas <command> --help` for details.

```text
atlas [--home DIR] [--json] command [options]
```

Global options:

| Option | Meaning |
|---|---|
| `--home DIR` | Data directory. Default: `$ATLAS_HOME`, else `~/.atlas`. |
| `--json` | Machine-readable output where the command supports it. |
| `--version` | Print the version and exit. |

Every command also accepts `--home` and `--json` after the command name, so
`atlas status --home ./data` and `atlas --home ./data status` are equivalent.

Exit codes: `0` success, `1` a check failed or an action could not be
completed, `2` a review decision was rejected by validation, `130` interrupted.

---

## `atlas up`

First-run experience. Creates the data directory, writes `config.yaml` if
missing, loads the demo corpus when no data exists, starts the dashboard, and
opens a browser.

```bash
atlas up
atlas up --port 9000 --no-browser
```

| Flag | Default | Meaning |
|---|---|---|
| `--port` | `8080` | Dashboard port (loopback only). |
| `--host` | `127.0.0.1` | Bind address; must be loopback. |
| `--no-browser` | off | Do not open a browser. |

Existing data is never replaced by `up`.

---

## `atlas init`

Creates the data directory layout and a commented `config.yaml`.

```bash
atlas init
atlas init --home ./project-data --force   # overwrite an existing config
```

`--force` only rewrites `config.yaml`; it does not touch stores.

---

## `atlas demo`

Loads the conspicuously fictional corpus from `examples/` (or the packaged
copy) into the data directory. It refuses to seed into the source checkout
root unless `--force` is passed.

```bash
atlas demo
atlas demo --reset          # replace the demo databases first
atlas demo --serve          # load, then start the dashboard
atlas demo --json           # machine-readable summary
```

The corpus is synthetic: no real institution, event, or loss is depicted.

---

## `atlas serve`

Starts the loopback dashboard. If no data exists, the demo corpus is loaded
first so the dashboard is never empty on a first run.

```bash
atlas serve
atlas serve --port 9000
atlas open                  # open the default URL without starting a server
```

The backend binds to loopback only. A shared deployment requires the separate
identity-proxy contract in
[docs/reference/shared-deployment.md](reference/shared-deployment.md).

---

## `atlas run`

Runs the eight-phase pipeline over the approved corpus.

```bash
atlas run                                  # deterministic, offline, 3 scenarios
atlas run --with-llm --scenarios 5
atlas run --file research.md --url https://example.org/source
```

| Flag | Meaning |
|---|---|
| `--with-llm` | Enable model-backed phases. Without it, no model is called and no cost is incurred. |
| `--scenarios N` | Scenarios to generate when generation runs (default `3`). |
| `--text` / `--file` | Research text for this run. |
| `--url` | Public source URL supporting the research packet (repeatable). |

New material enters as `pending`. Generation and pattern analysis consume only
approved evidence and risks, so a run with unreviewed input will not use it.

---

## `atlas status`

Library counts, review queue, last recorded run, and disk usage.

```bash
atlas status
atlas status --json
```

---

## `atlas doctor`

Checks Python version, data-directory writability, the ontology guard bundle
and its dependencies, SQLite integrity, demo fixtures, the model endpoint, and
the dashboard port.

```bash
atlas doctor
atlas doctor --no-network     # skip the model endpoint probe
atlas doctor --json
```

`WARN` means a degraded but usable configuration (for example, no model
endpoint). `FAIL` sets exit code `1`.

---

## `atlas review`

The same audited decision path used by the dashboard. Lists pending evidence,
risks, signposts, and merge proposals, and records decisions with reviewer
attestations.

```bash
atlas review list
atlas review list --kind risk --status pending
atlas review approve-event EVT-123 --confirm-public-source --confirm-rights
atlas review reject-event EVT-123 --reason "Source does not support the claim"
atlas review approve-signpost ATLAS-2026-001 --note "Checked against cited evidence"
atlas review approve-merge PROP-1 --reason "Same underlying control failure"
atlas review rate-memo MEMO-1 --rating 0.8
```

The two confirmation flags are reviewer attestations, not automated factual or
rights determinations. Risk approval additionally requires every reference to
resolve to a fully approved public evidence event.

Run `atlas review --help` for the complete list.

---

## `atlas export`

Exports a frozen snapshot with provenance and checksums. Refuses to overwrite
an existing file.

```bash
atlas export --format pdf   --scenario-id ATLAS-2026-001 --output brief.pdf
atlas export --format xlsx  --status active --output portfolio.xlsx
atlas export --format json  --output snapshot.json
```

Run `atlas export --help` for formats and selectors.

---

## `atlas config`

Shows the effective configuration and where each store resolves. API keys are
never printed.

```bash
atlas config
atlas config --json
```

---

## `atlas clean`

Deletes the local stores and generated subdirectories in the data directory.
Refuses to operate on `/` or your home directory. Requires `--yes`.

```bash
atlas clean          # prints what would be deleted
atlas clean --yes    # deletes
```

Research data is not recoverable. Copy the directory first if it matters.
