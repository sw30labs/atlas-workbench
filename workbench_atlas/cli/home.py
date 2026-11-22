"""One data directory for every runtime store.

``ATLAS_HOME`` (or ``--home``) keeps the SQLite databases, the risk register
and the exports in a single, discoverable place instead of scattering them
across the current working directory.  Individual ``ATLAS_DB`` /
``ATLAS_EVIDENCE_DB`` / ... variables still win, so existing deployments keep
working unchanged.
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_HOME = "ATLAS_HOME"
DEFAULT_HOME = "~/.atlas"

# Settings field -> filename inside the home directory.
STORE_FILES: dict[str, str] = {
    "scenario_db_path": "atlas.db",
    "evidence_db_path": "event_evidence.db",
    "emerging_risks_path": "emerging_risks.json",
    "scenario_export_path": "scenario_export.json",
    "context_db_path": "organization-context.db",
}

SUBDIRS = ("exports", "logs", "telemetry")


def resolve_home(explicit: str | Path | None = None) -> Path:
    """Resolve the home directory without mutating the environment."""
    raw = explicit or os.environ.get(ENV_HOME) or DEFAULT_HOME
    return Path(raw).expanduser().resolve()


def activate(explicit: str | Path | None = None) -> Path:
    """Set ``ATLAS_HOME`` for this process and return the resolved path.

    Must run before ``Settings()`` is constructed: settings read the
    environment once, at construction time.
    """
    home = resolve_home(explicit)
    os.environ[ENV_HOME] = str(home)
    return home


def store_paths(home: Path) -> dict[str, Path]:
    return {field: home / name for field, name in STORE_FILES.items()}


def ensure_layout(home: Path) -> dict[str, Path]:
    """Create the home directory and its subdirectories."""
    home.mkdir(parents=True, exist_ok=True)
    for name in SUBDIRS:
        (home / name).mkdir(exist_ok=True)
    return store_paths(home)


def is_initialized(home: Path) -> bool:
    return (home / STORE_FILES["scenario_db_path"]).is_file() and (
        home / STORE_FILES["evidence_db_path"]
    ).is_file()
