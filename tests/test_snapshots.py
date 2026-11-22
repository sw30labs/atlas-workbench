"""Pre-write snapshots of the register (the file re-running cannot rebuild)."""

from __future__ import annotations

import json

import pytest

from workbench_atlas.phases.phase2_risks import save_emerging_risks
from workbench_atlas.snapshots import (
    snapshot_before_write,
    snapshot_dir_for,
)


def _risk(status: str) -> list[dict]:
    return [{
        "issue": "Synthetic risk",
        "review_status": status,
        "evidence_refs": ["EVT-SYNTHETIC-0001"],
    }]


def test_destructive_rewrite_preserves_the_previous_decision(tmp_path):
    """The 2026-08-04 failure in miniature: a cycle rewrites an approved
    record to pending, and the approval must survive somewhere."""
    register = tmp_path / "emerging_risks.json"
    save_emerging_risks(register, _risk("approved"))
    save_emerging_risks(register, _risk("pending"))

    snapshots = sorted(snapshot_dir_for(register).glob("*.json"))
    assert len(snapshots) == 1
    assert json.loads(snapshots[-1].read_text())[0]["review_status"] == "approved"
    assert json.loads(register.read_text())[0]["review_status"] == "pending"


def test_first_write_has_nothing_to_preserve(tmp_path):
    register = tmp_path / "emerging_risks.json"
    save_emerging_risks(register, _risk("pending"))
    directory = snapshot_dir_for(register)
    assert not directory.exists() or not list(directory.glob("*.json"))


def test_unchanged_rewrites_do_not_accumulate(tmp_path):
    """An hourly pipeline that changes nothing must not fill the disk."""
    register = tmp_path / "emerging_risks.json"
    for _ in range(5):
        save_emerging_risks(register, _risk("pending"))
    assert len(list(snapshot_dir_for(register).glob("*.json"))) == 1


def test_snapshots_are_pruned_to_the_cap(tmp_path):
    register = tmp_path / "emerging_risks.json"
    root = tmp_path / "snaps"
    for index in range(6):
        register.write_text(json.dumps([{"issue": f"r{index}"}]))
        snapshot_before_write(register, root=root, keep=3)
    kept = sorted(snapshot_dir_for(register, root=root).glob("*.json"))
    assert len(kept) == 3
    # Pruning drops the oldest, so the most recent writes survive.
    assert json.loads(kept[-1].read_text())[0]["issue"] == "r5"


def test_snapshot_failure_never_blocks_the_write(tmp_path, monkeypatch):
    """Refusing to save real work in order to protect a copy of it would be
    the wrong trade, so a snapshot error is logged and swallowed."""
    register = tmp_path / "emerging_risks.json"
    save_emerging_risks(register, _risk("approved"))

    def _explode(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("workbench_atlas.snapshots.shutil.copy2", _explode)
    save_emerging_risks(register, _risk("pending"))
    assert json.loads(register.read_text())[0]["review_status"] == "pending"


def test_missing_source_is_not_an_error(tmp_path):
    assert snapshot_before_write(tmp_path / "absent.json") is None


@pytest.mark.parametrize("keep", [0, 1])
def test_keep_bounds_are_respected(tmp_path, keep):
    register = tmp_path / "emerging_risks.json"
    root = tmp_path / "snaps"
    for index in range(3):
        register.write_text(json.dumps([{"issue": f"r{index}"}]))
        snapshot_before_write(register, root=root, keep=keep)
    assert len(list(snapshot_dir_for(register, root=root).glob("*.json"))) == keep
