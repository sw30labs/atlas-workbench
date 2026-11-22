# Atlas Workbench

**Operational-risk scenarios you can actually run, review, and defend.**

Atlas Workbench turns public research into structured, testable
operational-risk scenarios: what could fail, how it would propagate, who owns
the response, and what evidence supports the claim. A scenario is a hypothesis,
not a prediction — the workbench exists to help you challenge it.

This is the usability-focused edition of Atlas Workbench: same engine and
provenance rules, one command to a working dashboard, one place for your data,
and a single `atlas` command instead of a dozen scripts.

## Project boundaries

This is an independent personal research project:

- Atlas Workbench is not affiliated with, sponsored, supplied, approved, or
  endorsed by the author's employer or any other organization. No employer
  code, data, internal reports, client information, systems, methods, or other
  confidential or proprietary material is included.
- The project contains no ORX member, subscription, loss-event, news, scenario,
  taxonomy, or other proprietary data. [ORX](https://orx.org/) is linked only
  as an external industry resource. No affiliation or endorsement is implied.
- Nassim Nicholas Taleb's books *The Black Swan* and *Antifragile* are
  acknowledged as intellectual influences on thinking about uncertainty,
  fragility, and resilience. The software does not reproduce or simulate
  Taleb's persona, voice, likeness, writings, or views.
- Banking is the principal domain so the project can use a bounded, coherent
  public taxonomy and evaluation surface.
- Outputs are hypothetical research artifacts. They are not financial, legal,
  regulatory, investment, security, or risk-management advice.

Read [DISCLAIMER.md](DISCLAIMER.md), [DATA_PROVENANCE.md](DATA_PROVENANCE.md),
and [DATA_LICENSE.md](DATA_LICENSE.md) before using or contributing data.

---

## Start here

```bash
./start.sh
```

That is the whole setup. It creates a virtualenv, installs the package, loads
the conspicuously fictional demo corpus, and opens
**http://127.0.0.1:8080**.

Requirements: Python 3.12 or 3.13. If [`uv`](https://docs.astral.sh/uv/) is on
your `PATH`, `start.sh` will fetch a suitable interpreter for you.

Prefer doing it by hand? `--seed` installs pip into the virtualenv. Without it, `pip` is not there.

```bash
uv venv --python 3.13 --seed .venv
source .venv/bin/activate
pip install -e .
atlas up
```

---

## The six commands you need

| Command | What it does |
|---|---|
| `atlas up` | First run: create the data directory, load the demo, open the dashboard |
| `atlas demo` | Reload the fictional demo corpus (`--reset` to start clean) |
| `atlas run` | Analyse the approved corpus — deterministic and offline by default |
| `atlas status` | Library counts, review queue, last run |
| `atlas doctor` | Check Python, stores, guard bundle, model endpoint, port |
| `atlas serve` | Open the dashboard (auto-loads the demo if you have no data) |

Everything else: `atlas --help`. Full reference: [docs/CLI.md](docs/CLI.md).

## Research and maintenance commands

These stay separate from the six commands above. They are for scoring research, pre-registered evaluation, catalog import, and export.

| Command | What it does |
|---|---|
| `atlas-score` | Deterministic compound scoring |
| `atlas-eval` | Pre-registered evaluation |
| `atlas-framework` | MITRE ATLAS catalog import |
| `atlas-export` | The same exporter as `atlas export` |

```console
$ atlas status
Atlas Workbench status
  data directory  ~/.atlas
  initialized     yes

Library
  collection  total  by status
  evidence    8      approved_synthetic 8
  risks       6      approved_synthetic 6
  scenarios   6      active 6

Review queue
  OK    nothing waiting for review
```

---

## How the work actually flows

```
add research → prepare a draft → select items → review → run analysis → scenarios
```

1. **Add research** in *Analyze → Add Research*. Paste prose, notes, and literal
   URLs into one field; nothing is written until you select items and submit.
2. **Review** is the human boundary. Every pending record shows *why* it can or
   cannot be approved (provenance, synthetic status, reference resolution).
3. **Run** the analysis. Generation uses a bounded judge-and-revise loop; the
   deterministic path needs no model at all.
4. **Read the portfolio** as a map or table, open an Atlas Brief, compare
   scenarios, and export a PDF/Word/Excel/PowerPoint pack with provenance and
   checksums.

The dashboard's **Organization** workspace is a separate, opt-in layer that
connects scenarios to services, owners, controls, and exercise evidence.

---

## Your own data

```bash
atlas init                      # create ~/.atlas + config.yaml
atlas serve                     # add research in the dashboard
atlas run --with-llm            # generate scenarios (needs a model endpoint)
atlas status                    # see what is waiting for review
atlas export --format pdf --scenario-id ATLAS-... --output brief.pdf
```

All runtime state lives in **one directory** (`~/.atlas` by default):

```text
~/.atlas/
├── atlas.db                 # scenarios, findings, review history
├── event_evidence.db        # evidence register
├── emerging_risks.json      # risk register
├── scenario_export.json
├── organization-context.db  # only if you opt in
├── config.yaml              # created by `atlas init`
└── exports/  logs/  telemetry/
```

Change it per-command with `--home`, or once with `export ATLAS_HOME=/path`.
Back it up by copying the directory. Delete it with `atlas clean`.

Individual `ATLAS_DB` / `ATLAS_EVIDENCE_DB` / `ATLAS_EMERGING_RISKS` /
`ATLAS_SCENARIO_EXPORT` variables still override single stores, so existing
deployments keep working unchanged.

---

## Optional: connect a local model

Nothing below is required for intake, review, deterministic scoring, exports,
or the dashboard. Model-backed generation needs an OpenAI-compatible endpoint.

```bash
export ATLAS_LLM_PROVIDER=omlx                 # or dgx for NVIDIA vLLM
export OMLX_BASE_URL=http://127.0.0.1:8000/v1
export OMLX_MODEL=DeepSeek-V4.1-Flash-1017-oQ4e-mtp
export OMLX_MODEL_JUDGE=DeepSeek-V4.1-Flash-1017-oQ4e-mtp
atlas doctor                                   # verifies the endpoint
atlas run --with-llm --scenarios 5
```

Secrets stay in environment variables, never in `config.yaml`. See
[.env.example](.env.example) and [docs/CONFIGURATION.md](docs/CONFIGURATION.md).

---

## Safety and provenance

The workbench keeps the original project's boundaries; it does not relax them
for convenience.

- **Draft before persistence.** Free-form intake is classified and previewed
  without writes. Only selected, valid items enter review, always as pending.
- **Evidence before synthesis.** Public-source records are cited at record
  level; generated narrative never becomes its own source.
- **Humans decide.** Merge and retire actions require the explicit
  `ATLAS_AUTO_RETIRE=1` switch. The dashboard cannot override it.
- **Fail-closed writes.** Machine-producer write seams validate through the
  ontology guard before persisting (`ATLAS_GUARD=off` disables it).
- **Local by default.** SQLite, JSON artifacts, and local inference stay on
  your machine.
- **Not advice.** Outputs are hypothetical research artifacts — not financial,
  legal, regulatory, or risk-management advice. See
  [DISCLAIMER.md](DISCLAIMER.md) and [DATA_PROVENANCE.md](DATA_PROVENANCE.md).

---

## What changed in this edition

| Before | Now |
|---|---|
| Six-step manual setup (`scripts/setup_and_run.sh`) | `./start.sh`, idempotent |
| Runtime stores in the repo root / cwd | One `ATLAS_HOME` directory |
| `python -m workbench_atlas.web` + env vars | `atlas serve` |
| `scripts/review_records.py …` | `atlas review …` (same audited path) |
| `scripts/seed_demo.py --output-dir …` | `atlas demo` |
| `pip install -e '.[dev,ontology]'` | `pip install -e .` (guard deps included) |
| 959-line README as the front door | This page; the original lives in [docs/REFERENCE_README.md](docs/REFERENCE_README.md) |

The engine, database schema, review rules, ontology guard, and scoring code are
unchanged apart from `ATLAS_HOME` support in `config.py`.

---

## Development

```bash
make install     # venv + editable install
make test        # pytest (offline)
make lint        # the CI lint gate
make check       # lint + test
```

```text
workbench_atlas/     engine, phases, scoring, dashboard, and the `atlas` CLI
  cli/                 argument parsing, home resolution, commands
  demo.py              demo-corpus seeder (behind `atlas demo`)
  review_cli.py        review commands (behind `atlas review`)
  web/                 stdlib HTTP dashboard
ontology/              fail-closed write guard
tests/                 unit and integration tests
docs/                  use (CLI, configuration), decisions, reference, and research
examples/              fictional demo fixtures
```

Contributor rules are unchanged: see [CONTRIBUTING.md](CONTRIBUTING.md) and
[AGENTS.md](AGENTS.md).

## Documentation

Start at [docs/README.md](docs/README.md). That map splits use, decisions, and research.

- [docs/CLI.md](docs/CLI.md) — every command and flag
- [docs/CONFIGURATION.md](docs/CONFIGURATION.md) — data paths and settings
- [docs/reference/enterprise-experience.md](docs/reference/enterprise-experience.md) — the workbench tour
- [docs/reference/organization-context.md](docs/reference/organization-context.md) — the organization layer
- [docs/reference/exports.md](docs/reference/exports.md) — export contract
- [docs/reference/TAXONOMY.md](docs/reference/TAXONOMY.md) — the project-native risk taxonomy
- [docs/REFERENCE_README.md](docs/REFERENCE_README.md) — the original full manual

## License

Code is [Apache 2.0](LICENSE). Data and documentation have separate terms in
[DATA_LICENSE.md](DATA_LICENSE.md).
