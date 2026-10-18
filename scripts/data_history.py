#!/usr/bin/env python3
"""Version the local data files in a git repository separate from the code.

ADR-012 keeps runtime artifacts out of this repository, and that stays right:
`PUBLIC_RELEASE_CHECKLIST.md` gates release on the *complete git history*, so a
a governed register committed here would be permanent and require scrubbing
by rewriting history. But the register also holds human review decisions, which
no re-run reproduces — the one runtime artifact whose recovery story is not
"re-run the pipeline".

So the data gets its own history, in its own git directory, while the files stay
exactly where the application expects them. This is the bare-repo pattern: a
separate `--git-dir` pointed at the same working tree, tracking only the paths
named here. The code repository never sees it, and it never sees the code.

    python scripts/data_history.py init      # once
    python scripts/data_history.py commit -m "after monitoring cycle"
    python scripts/data_history.py log
    python scripts/data_history.py git -- diff HEAD~1 -- emerging_risks.json

The git directory defaults to ~/.contingency-atlas-data.git and is overridden
with ATLAS_DATA_GIT_DIR. Keep it outside the working tree so `generated/`
cleanups cannot delete your history.

SQLite stores are committed as `.sql` text dumps under `generated/data-history/`
rather than as binaries: the review decisions inside them are what matter, and a
dump diffs readably where a database file does not.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Text files versioned verbatim, relative to the working tree.
TRACKED_FILES = (
    "emerging_risks.json",
    "compound_scenarios.json",
    "scenario_export.json",
)

# SQLite stores, dumped to text before each commit.
TRACKED_DATABASES = (
    "atlas.db",
    "event_evidence.db",
)

DUMP_DIR = Path("generated") / "data-history"


def git_dir() -> Path:
    override = os.environ.get("ATLAS_DATA_GIT_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".contingency-atlas-data.git"


def _git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", f"--git-dir={git_dir()}", f"--work-tree={ROOT}", *args],
        cwd=ROOT,
        capture_output=True,
        check=check,
    )


def _initialised() -> bool:
    return (git_dir() / "HEAD").exists()


def cmd_init(_: argparse.Namespace) -> int:
    target = git_dir()
    if _initialised():
        print(f"already initialised: {target}")
        return 0
    target.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "--bare", "--quiet", str(target)], check=True)
    # Without this, `status` would list the entire source tree as untracked.
    _git("config", "status.showUntrackedFiles", "no")
    _git("config", "core.worktree", str(ROOT))
    print(f"initialised {target}")
    print(f"working tree: {ROOT}")
    print("\nnext: python scripts/data_history.py commit -m 'baseline'")
    return 0


def write_dumps() -> list[str]:
    """Dump each SQLite store to deterministic SQL text. Returns paths."""
    out_dir = ROOT / DUMP_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for name in TRACKED_DATABASES:
        source = ROOT / name
        if not source.is_file():
            continue
        target = out_dir / f"{name}.sql"
        connection = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
        try:
            with open(target, "w", encoding="utf-8") as handle:
                for line in connection.iterdump():
                    handle.write(f"{line}\n")
        finally:
            connection.close()
        written.append(str(target.relative_to(ROOT)))
    return written


def cmd_commit(args: argparse.Namespace) -> int:
    if not _initialised():
        print("not initialised — run: python scripts/data_history.py init", file=sys.stderr)
        return 1

    paths = [name for name in TRACKED_FILES if (ROOT / name).is_file()]
    paths.extend(write_dumps())
    if not paths:
        print("no data files present; nothing to version")
        return 0

    # -f is required and correct: those paths are ignored by the CODE repo's
    # .gitignore, which git still reads because the working tree is shared.
    # Tracking exactly what that file excludes is this repository's purpose.
    _git("add", "-f", "--", *paths)
    staged = _git("diff", "--cached", "--name-only").stdout.decode().split()
    if not staged:
        print("no changes since the last commit")
        return 0

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    message = args.message or f"data snapshot {stamp}"
    _git("commit", "--quiet", "-m", message)
    revision = _git("rev-parse", "--short", "HEAD").stdout.decode().strip()
    print(f"[{revision}] {message}")
    for path in staged:
        print(f"  {path}")
    return 0


def cmd_log(args: argparse.Namespace) -> int:
    if not _initialised():
        print("not initialised — run: python scripts/data_history.py init", file=sys.stderr)
        return 1
    result = _git(
        "log", f"-{args.limit}", "--stat", "--format=%C(auto)%h %ad %s", "--date=short",
        check=False,
    )
    sys.stdout.write(result.stdout.decode())
    return result.returncode


def cmd_git(args: argparse.Namespace) -> int:
    """Escape hatch: run any git command against the data history."""
    if not _initialised():
        print("not initialised — run: python scripts/data_history.py init", file=sys.stderr)
        return 1
    # argparse.REMAINDER keeps the "--" that separates our options from git's,
    # and git would choke on it as a first argument.
    passthrough = list(args.args)
    if passthrough and passthrough[0] == "--":
        passthrough = passthrough[1:]
    if not passthrough:
        print("usage: data_history.py git -- <git args>", file=sys.stderr)
        return 2
    result = _git(*passthrough, check=False)
    sys.stdout.write(result.stdout.decode())
    sys.stderr.write(result.stderr.decode())
    return result.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create the data git directory").set_defaults(
        func=cmd_init
    )

    commit = sub.add_parser("commit", help="commit the current data state")
    commit.add_argument("-m", "--message", default="")
    commit.set_defaults(func=cmd_commit)

    log = sub.add_parser("log", help="show the data history")
    log.add_argument("-n", "--limit", type=int, default=10)
    log.set_defaults(func=cmd_log)

    passthrough = sub.add_parser("git", help="run a git command against the history")
    passthrough.add_argument("args", nargs=argparse.REMAINDER)
    passthrough.set_defaults(func=cmd_git)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
