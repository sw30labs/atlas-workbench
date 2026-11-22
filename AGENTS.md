# Repository Guidelines

## Project Structure & Module Organization

`workbench_atlas/` is the Python package. Pipeline stages live in `phases/`, deterministic analysis in `scoring/`, MITRE ATLAS support in `frameworks/`, and the dashboard in `web/`. Tests are in `tests/`; shared fixtures belong in `tests/conftest.py`. The fail-closed ontology guard lives in `ontology/`. Use `scripts/` for verification and maintenance, `examples/` for fictional fixtures, and `docs/` for architecture and governance. Do not commit runtime databases, logs, exports, or files under `generated/`.

## Build, Test, and Development Commands

Use Python 3.12 or 3.13:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev,ontology]'
```

- `python -m pytest` runs the full suite; pass a path such as `tests/test_pipeline.py` for a focused run.
- `python -m ruff check --select F821,F811,F402,E9,F841 workbench_atlas scripts tests` runs the CI lint gate.
- `python scripts/verify_all.py` runs offline integration checks.
- `python scripts/public_release_gate.py --history` checks the clean-room release boundary.
- `python -m workbench_atlas.web` starts the loopback dashboard.

## Coding Style & Naming Conventions

Follow PEP 8 with four-space indentation. Use `snake_case` for modules, functions, and variables; `PascalCase` for classes; and `UPPER_CASE` for constants. Prefer typed, testable units. Keep scoring deterministic, fail safely when inputs or provenance are missing, and preserve explicit operator gates for destructive actions. Ruff is the configured linter; no repository-wide autoformatter is configured.

## Testing Guidelines

Pytest discovers `test_*.py` files and `test_*` functions under `tests/`. Add or update tests for every behavior change, using temporary or synthetic fixtures rather than live runtime stores. Tests should remain offline; optional real-model probes may skip. There is no numeric coverage threshold, but the full suite and relevant verification scripts must pass.

## Commit & Pull Request Guidelines

Recent history follows Conventional Commit-style subjects, for example `feat(scoring): add interval validation` or `security: harden provenance checks`. Keep commits and branches focused. Open an issue before substantial feature, schema, taxonomy, or data changes. Pull requests should describe intent, risks, validation, and provenance; link the issue, update documentation, and include screenshots for dashboard changes. Confirm that tests pass and inspect generated or binary artifacts separately.

## Security & Data Contributions

Keep credentials in environment variables; `workbench-atlas.yaml` is non-secret configuration only. Follow `DATA_PROVENANCE.md` for data-bearing changes, clearly label synthetic material, and never commit employer, client, subscription, personal, or confidential data. Report vulnerabilities through the private process in `SECURITY.md`.
