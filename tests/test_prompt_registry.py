"""Dynamic prompt-registry release checks."""

from workbench_atlas.prompt_registry import (
    PromptRecord,
    discover_prompt_names,
    discover_prompts,
    inspect_prompt_registry,
)


def test_discovered_prompt_constants_are_versioned_and_clean():
    records = discover_prompts()
    assert records
    assert len(records) >= 20
    assert not inspect_prompt_registry(records)


def test_prompt_name_discovery_is_not_a_closed_inventory(tmp_path):
    package = tmp_path / "sample_package"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "new_lane.py").write_text(
        "NEW_SYSTEM_PROMPT = " + repr("new prompt " * 20) + "\n",
        encoding="utf-8",
    )

    assert (
        "sample_package.new_lane",
        "NEW_SYSTEM_PROMPT",
    ) in discover_prompt_names(package)


def test_short_prompt_constants_are_versioned_and_scanned(tmp_path, monkeypatch):
    package = tmp_path / "short_prompt_package"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    planted = "".join(("cm", "db", "_info_key"))
    (package / "lane.py").write_text(
        f"SHORT_PROMPT = {planted!r}\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    records = discover_prompts(package)

    assert len(records) == 1
    assert records[0].texts == (planted,)
    failures = inspect_prompt_registry(records)
    assert any("version is missing" in item for item in failures)
    assert any("inventory alias" in item for item in failures)


def test_registry_rejects_missing_version_and_fragmented_residue():
    planted = "".join(("cm", "db", "_info_key"))
    record = PromptRecord(
        module="sample.prompt",
        name="SYSTEM_PROMPT",
        texts=(("safe prompt text " * 10) + planted,),
        version=None,
    )

    failures = inspect_prompt_registry([record])

    assert any("version is missing" in item for item in failures)
    assert any("inventory alias" in item for item in failures)
