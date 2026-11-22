#!/usr/bin/env python3
"""Create a release-gated public tree without carrying private Git history."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from public_release_gate import ROOT

PUBLIC_AUTHOR = "Atlas Workbench contributors"
PUBLIC_EMAIL = "contributors" + chr(64) + "workbench-atlas.invalid"
_REPOSITORY_ENVIRONMENT = {
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_DIR",
    "GIT_GRAFT_FILE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_REPLACE_REF_BASE",
    "GIT_SHALLOW_FILE",
    "GIT_WORK_TREE",
}


def _clean_environment() -> dict[str, str]:
    """Return an environment that cannot redirect Git to another repository."""
    env = os.environ.copy()
    for variable in _REPOSITORY_ENVIRONMENT:
        env.pop(variable, None)
    return env


def _git(
    *args: str,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    root = ROOT if cwd is None else cwd
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        text=True,
        capture_output=True,
        env=_clean_environment() if env is None else env,
    )


def _run_public_gate(
    root: Path,
    *args: str | Path,
    env: dict[str, str] | None = None,
) -> None:
    """Run the gate from ``root`` and preserve its useful failure output."""
    result = subprocess.run(
        [
            sys.executable,
            str(root / "scripts" / "public_release_gate.py"),
            *(str(arg) for arg in args),
        ],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
        env=_clean_environment() if env is None else env,
    )
    if result.returncode:
        if result.stdout:
            print(result.stdout, end="", file=sys.stderr)
        if result.stderr:
            print(result.stderr, end="", file=sys.stderr)
        raise RuntimeError(f"public release gate failed for {root}")


def export_public_tree(destination: Path, *, initialize_git: bool = False) -> Path:
    """Export the current candidate tree through a temporary Git index.

    Tracked modifications, deletions, and untracked-but-not-ignored files are
    staged only in the temporary index. Ignored runtime material and the
    source repository's history never enter the output.
    """
    requested_destination = destination.expanduser().absolute()
    if os.path.lexists(requested_destination):
        raise ValueError(f"destination already exists: {requested_destination}")
    destination = requested_destination.resolve()
    source_root = ROOT.resolve()
    if os.path.lexists(destination):
        raise ValueError(f"destination already exists: {destination}")
    if destination == source_root or source_root in destination.parents:
        raise ValueError("destination must be outside the source repository")

    # This includes byte/path checks plus fixture and prompt validation. Run it
    # before creating anything at the destination side of the operation.
    _run_public_gate(source_root)

    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(
        tempfile.mkdtemp(
            prefix=f".{destination.name}.building-",
            dir=destination.parent,
        )
    )
    index_directory = tempfile.TemporaryDirectory(
        prefix="workbench-atlas-public-index-"
    )
    index_path = Path(index_directory.name) / "index"
    git_env = _clean_environment()
    git_env["GIT_INDEX_FILE"] = str(index_path)

    try:
        _git("read-tree", "HEAD", cwd=source_root, env=git_env)
        _git("add", "--all", cwd=source_root, env=git_env)
        _git(
            "checkout-index",
            "--all",
            f"--prefix={stage}{os.sep}",
            cwd=source_root,
            env=git_env,
        )

        # The source copy of the gate can compare the staged tree against the
        # alternate index without ever swapping the process-wide index.
        _run_public_gate(source_root, "--staging", stage, env=git_env)

        public_git_env = _clean_environment()
        public_git_env["GIT_CONFIG_NOSYSTEM"] = "1"
        public_git_env["GIT_CONFIG_GLOBAL"] = os.devnull
        _git(
            "init",
            "--template=",
            "--initial-branch=main",
            cwd=stage,
            env=public_git_env,
        )
        _git("config", "user.name", PUBLIC_AUTHOR, cwd=stage, env=public_git_env)
        _git("config", "user.email", PUBLIC_EMAIL, cwd=stage, env=public_git_env)
        # The staging gate approved every copied file. Force-add makes the new
        # root faithfully include any source-tracked path covered by an ignore
        # rule rather than silently dropping it in the fresh repository.
        _git("add", "--all", "--force", cwd=stage, env=public_git_env)

        # Re-run the complete gate from the exported codebase. Unlike the
        # source-side staging scan, its semantic validators load the exact
        # copied fixtures and prompt modules.
        _run_public_gate(stage, env=public_git_env)

        if initialize_git:
            commit_env = public_git_env.copy()
            commit_env.update(
                {
                    "GIT_AUTHOR_NAME": PUBLIC_AUTHOR,
                    "GIT_AUTHOR_EMAIL": PUBLIC_EMAIL,
                    "GIT_COMMITTER_NAME": PUBLIC_AUTHOR,
                    "GIT_COMMITTER_EMAIL": PUBLIC_EMAIL,
                }
            )
            _git(
                "-c",
                "commit.gpgSign=false",
                "commit",
                "-m",
                "chore: establish public source root",
                cwd=stage,
                env=commit_env,
            )
            _run_public_gate(stage, "--history", env=public_git_env)
            commit_count = _git(
                "rev-list",
                "--count",
                "HEAD",
                cwd=stage,
                env=public_git_env,
            ).stdout.strip()
            if commit_count != "1":
                raise RuntimeError("public export must contain exactly one commit")
            root_row = _git(
                "rev-list",
                "--parents",
                "-n",
                "1",
                "HEAD",
                cwd=stage,
                env=public_git_env,
            ).stdout.split()
            if len(root_row) != 1:
                raise RuntimeError("public export commit must be a root commit")
            if _git(
                "status",
                "--porcelain",
                "--untracked-files=all",
                cwd=stage,
                env=public_git_env,
            ).stdout:
                raise RuntimeError("public export repository is not clean")
        else:
            shutil.rmtree(stage / ".git")

        # Do not replace a path that appeared while the candidate was built.
        if os.path.lexists(destination):
            raise FileExistsError(f"destination appeared during export: {destination}")
        stage.rename(destination)
        return destination
    finally:
        index_directory.cleanup()
        if stage.exists():
            shutil.rmtree(stage)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "export the release candidate through an isolated Git index; "
            "the source repository index and history are unchanged"
        )
    )
    parser.add_argument("destination", type=Path)
    parser.add_argument(
        "--initialize-git",
        action="store_true",
        help="create and verify a one-commit public lineage in the export",
    )
    args = parser.parse_args()
    try:
        output = export_public_tree(
            args.destination,
            initialize_git=args.initialize_git,
        )
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"public export failed: {exc}", file=sys.stderr)
        return 1
    print(f"public export ready: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
