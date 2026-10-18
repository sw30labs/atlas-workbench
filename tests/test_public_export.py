"""Safety checks for the clean-root public export helper."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import export_public_tree as public_export


def _git(root: Path, *args: str) -> str:
    env = os.environ.copy()
    for variable in public_export._REPOSITORY_ENVIRONMENT:
        env.pop(variable, None)
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        text=True,
        capture_output=True,
        env=env,
    )
    return result.stdout


def _source_repository(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    (source / "scripts").mkdir()
    (source / "scripts" / "public_release_gate.py").write_text(
        "raise SystemExit(0)\n",
        encoding="utf-8",
    )
    (source / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    (source / "tracked.txt").write_text("original\n", encoding="utf-8")
    (source / "deleted.txt").write_text("remove me\n", encoding="utf-8")
    (source / "staged.txt").write_text("original\n", encoding="utf-8")
    _git(source, "init", "--initial-branch=main")
    _git(source, "add", "--all")
    _git(
        source,
        "-c",
        "user.name=Fixture",
        "-c",
        f"user.email=fixture{chr(64)}example.invalid",
        "commit",
        "-m",
        "initial",
    )
    return source


def _prepare_candidate(source: Path) -> bytes:
    (source / "tracked.txt").write_text("working tree\n", encoding="utf-8")
    (source / "deleted.txt").unlink()
    (source / "new.txt").write_text("new candidate\n", encoding="utf-8")
    (source / "ignored.txt").write_text("runtime only\n", encoding="utf-8")
    (source / "staged.txt").write_text("user staged\n", encoding="utf-8")
    _git(source, "add", "staged.txt")
    (source / "staged.txt").write_text("candidate latest\n", encoding="utf-8")
    index = source / _git(source, "rev-parse", "--git-path", "index").strip()
    return index.read_bytes()


def test_export_uses_current_candidate_without_mutating_source_index(
    tmp_path,
    monkeypatch,
):
    source = _source_repository(tmp_path)
    index_before = _prepare_candidate(source)
    head_before = _git(source, "rev-parse", "HEAD")
    refs_before = _git(source, "show-ref")
    destination = tmp_path / "public-tree"
    monkeypatch.setattr(public_export, "ROOT", source)
    monkeypatch.setenv("GIT_INDEX_FILE", str(tmp_path / "ambient-index"))
    monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path / "wrong-worktree"))

    result = public_export.export_public_tree(destination)

    assert result == destination
    assert (destination / "tracked.txt").read_text() == "working tree\n"
    assert (destination / "staged.txt").read_text() == "candidate latest\n"
    assert (destination / "new.txt").read_text() == "new candidate\n"
    assert not (destination / "deleted.txt").exists()
    assert not (destination / "ignored.txt").exists()
    assert not (destination / ".git").exists()
    index = source / _git(source, "rev-parse", "--git-path", "index").strip()
    assert index.read_bytes() == index_before
    assert _git(source, "rev-parse", "HEAD") == head_before
    assert _git(source, "show-ref") == refs_before
    assert _git(source, "show", ":staged.txt") == "user staged\n"
    assert (source / "staged.txt").read_text() == "candidate latest\n"


def test_initialized_export_has_one_clean_root_commit(tmp_path, monkeypatch):
    source = _source_repository(tmp_path)
    _prepare_candidate(source)
    destination = tmp_path / "public-repository"
    monkeypatch.setattr(public_export, "ROOT", source)

    public_export.export_public_tree(destination, initialize_git=True)

    assert _git(destination, "rev-list", "--count", "HEAD").strip() == "1"
    root_commit = _git(
        destination,
        "rev-list",
        "--parents",
        "-n",
        "1",
        "HEAD",
    ).split()
    assert len(root_commit) == 1
    assert _git(destination, "status", "--porcelain", "--untracked-files=all") == ""
    assert _git(destination, "remote") == ""
    assert _git(destination, "log", "-1", "--format=%an <%ae>").strip() == (
        f"{public_export.PUBLIC_AUTHOR} <{public_export.PUBLIC_EMAIL}>"
    )


def test_export_rejects_existing_or_source_nested_destination(
    tmp_path,
    monkeypatch,
):
    source = _source_repository(tmp_path)
    existing = tmp_path / "existing"
    existing.mkdir()
    broken_link = tmp_path / "broken-link"
    broken_link.symlink_to(tmp_path / "missing-target", target_is_directory=True)
    monkeypatch.setattr(public_export, "ROOT", source)

    with pytest.raises(ValueError, match="already exists"):
        public_export.export_public_tree(existing)
    with pytest.raises(ValueError, match="already exists"):
        public_export.export_public_tree(broken_link)
    with pytest.raises(ValueError, match="outside the source"):
        public_export.export_public_tree(source / "nested-export")


def test_failed_staging_gate_leaves_no_export_or_build_tree(
    tmp_path,
    monkeypatch,
):
    source = _source_repository(tmp_path)
    destination = tmp_path / "public-tree"
    monkeypatch.setattr(public_export, "ROOT", source)
    calls = 0

    def fail_staging(root, *args, env=None):
        nonlocal calls
        del root, env
        calls += 1
        if args:
            raise RuntimeError("planted staging failure")

    monkeypatch.setattr(public_export, "_run_public_gate", fail_staging)

    with pytest.raises(RuntimeError, match="planted staging failure"):
        public_export.export_public_tree(destination)

    assert calls == 2
    assert not destination.exists()
    assert not list(tmp_path.glob(".public-tree.building-*"))


def test_git_environment_scrubber_removes_repository_redirects(monkeypatch):
    for variable in public_export._REPOSITORY_ENVIRONMENT:
        monkeypatch.setenv(variable, f"planted-{variable.lower()}")
    monkeypatch.setenv("PATH", os.environ["PATH"])

    cleaned = public_export._clean_environment()

    assert not public_export._REPOSITORY_ENVIRONMENT.intersection(cleaned)
    assert cleaned["PATH"] == os.environ["PATH"]
