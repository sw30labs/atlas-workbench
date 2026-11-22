"""The installed ontology path follows the distribution name."""

import sysconfig
from pathlib import Path

from workbench_atlas.guard_hook import GUARD_SCRIPT, PackageNotFoundError


def test_source_checkout_guard_script_is_a_file():
    assert GUARD_SCRIPT.is_file()


def test_installed_ontology_dir_uses_atlas_workbench(tmp_path, monkeypatch):
    import workbench_atlas.guard_hook as hook

    guard = tmp_path / "share" / "workbench-atlas" / "ontology" / "guard.py"
    guard.parent.mkdir(parents=True)
    guard.write_text("# synthetic guard\n", encoding="utf-8")

    class _Entry:
        def as_posix(self):
            return "package-1.data/data/share/workbench-atlas/ontology/guard.py"

        def locate(self):
            return str(guard)

    seen: list[str] = []

    def fake_files(name):
        seen.append(name)
        if name != "atlas-workbench":
            raise PackageNotFoundError(name)
        return [_Entry()]

    monkeypatch.setattr(hook, "files", fake_files)
    found = hook._installed_ontology_dir()
    assert found == guard.resolve().parent
    assert seen == ["atlas-workbench"]


def test_installed_ontology_dir_falls_back_to_sysconfig(monkeypatch):
    import workbench_atlas.guard_hook as hook

    def fake_files(name):
        raise PackageNotFoundError(name)

    monkeypatch.setattr(hook, "files", fake_files)
    found = hook._installed_ontology_dir()
    expected = (
        Path(sysconfig.get_path("data")) / "share" / "workbench-atlas" / "ontology"
    )
    assert found == expected
